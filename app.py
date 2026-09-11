import os
import time
import uuid
import glob
import json
import shutil
import signal
import secrets
import subprocess
import threading
import urllib.parse
from collections import deque
from flask import Flask, request, jsonify, send_file, render_template, redirect

import spotify

app = Flask(__name__)
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Empty AUTH_TOKEN disables auth (local usage); set it when exposing to the internet.
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")
AUTH_COOKIE = "reclip_token"
AUTH_COOKIE_MAX_AGE = 30 * 24 * 3600

FILE_TTL_MINUTES = int(os.environ.get("FILE_TTL_MINUTES", "60"))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
MAX_FILESIZE = os.environ.get("MAX_FILESIZE", "2G")
# Hard ceiling for one download. Deliberately generous: a slow-but-alive
# transfer should be caught by DOWNLOAD_STALL_TIMEOUT, not by this.
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "3600"))
# yt-dlp and aria2c both print progress every few seconds, so going silent is
# the only reliable "this transfer is dead" signal. Wall-clock time cannot tell
# a 300 MB file on a throttled host from a stuck socket, and used to kill both.
DOWNLOAD_STALL_TIMEOUT = int(os.environ.get("DOWNLOAD_STALL_TIMEOUT", "120"))
MAX_PLAYLIST_ITEMS = int(os.environ.get("MAX_PLAYLIST_ITEMS", "50"))
# Optional Netscape-format cookies file for yt-dlp — helps with bot checks on datacenter IPs.
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "")

# aria2c fetches a single file over several connections, which roughly doubles
# throughput against hosts that throttle per connection. Optional: without the
# binary present we fall back to yt-dlp's own downloader.
ARIA2C_CONNECTIONS = int(os.environ.get("ARIA2C_CONNECTIONS", "8"))
# Empty means unlimited. yt-dlp forwards this to aria2c as
# --max-overall-download-limit, so one setting covers both downloaders. Worth
# setting when the box shares its uplink with something latency-sensitive.
DOWNLOAD_RATE_LIMIT = os.environ.get("DOWNLOAD_RATE_LIMIT", "")
USE_ARIA2C = (
    os.environ.get("USE_ARIA2C", "1") == "1" and shutil.which("aria2c") is not None
)

jobs = {}
download_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)


def ytdlp_cmd(*args, no_playlist=True):
    cmd = ["yt-dlp"]
    if no_playlist:
        cmd.append("--no-playlist")
    if YTDLP_COOKIES_FILE:
        cmd += ["--cookies", YTDLP_COOKIES_FILE]
    cmd += list(args)
    return cmd


def parse_ytdlp_json(stdout):
    """Parse yt-dlp JSON output.

    With ``-j`` yt-dlp prints one JSON object per line. Some extractors emit
    multiple videos even with ``--no-playlist``, so stdout holds several objects
    and a plain ``json.loads`` raises "Extra data". Return the first one.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


def valid_media_url(url):
    """Whether a user-supplied string is safe to hand to yt-dlp as a URL.

    yt-dlp parses its own argv, so a string starting with "-" is read as an
    option instead of a URL — and some of its options run commands. Commands
    are built with a "--" terminator too, but this check is the half that does
    not depend on yt-dlp's parser behaving as documented.

    Pinning the scheme also keeps file:// and the other protocols its
    extractors accept from being used to read the container's own filesystem.
    """
    if not url or url.startswith("-"):
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def kill_process_group(proc):
    """Kill yt-dlp and everything it spawned.

    proc.kill() reaches yt-dlp alone. aria2c and ffmpeg are its children, so
    they would outlive it — still transferring, still holding the partial files
    the caller is about to unlink.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def parse_size(text):
    """Parse a yt-dlp style size such as ``2G`` or ``500M`` into bytes.

    Returns 0 for anything unparseable, which callers read as "no limit".
    """
    text = str(text).strip().upper().rstrip("B")
    if not text:
        return 0
    units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    try:
        if text[-1] in units:
            return int(float(text[:-1]) * units[text[-1]])
        return int(float(text))
    except ValueError:
        return 0


MAX_FILESIZE_BYTES = parse_size(MAX_FILESIZE)


def last_error_line(output):
    """Pull the real error out of yt-dlp output with stderr merged into stdout.

    The tail of the stream is usually a progress line, so the last ``ERROR:``
    wins over it.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("ERROR:"):
            return line
    return lines[-1] if lines else "Download failed"


def partial_size(job_id):
    """Bytes actually occupied on disk by a job, partial files included.

    Allocated blocks rather than apparent length. aria2c fetches segments in
    parallel, so with --file-allocation=none the partial file is sparse: its
    apparent size leaps to near the total the moment the last segment opens,
    while the holes between segments take minutes more to fill. Apparent size
    therefore flatlines mid-download and would read as a stall, and it also
    overstates how much disk a job has really consumed.
    """
    total = 0
    for path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
        try:
            info = os.stat(path)
        except OSError:
            continue
        # st_blocks is POSIX-only and always counted in 512-byte units.
        blocks = getattr(info, "st_blocks", None)
        total += blocks * 512 if blocks is not None else info.st_size
    return total


def run_ytdlp(cmd, job_id=None, size_cap=0):
    """Run yt-dlp, killing it only once it genuinely stops making progress.

    ``subprocess.run(timeout=...)`` cannot distinguish a slow transfer from a
    dead one, so large files on throttled hosts were killed mid-download. The
    signal used instead is the partial file growing: aria2c keeps printing
    progress and retry chatter long after a transfer has actually died, so
    output alone marks a stuck download as healthy. Output still counts before
    the first byte lands, while yt-dlp is only resolving the page and no file
    exists yet. Wall-clock time remains as a backstop.

    ``size_cap`` re-implements ``--max-filesize``, which yt-dlp does not enforce
    when an external downloader performs the transfer.

    Returns ``(returncode, output, aborted)`` where ``aborted`` is None or the
    reason this process was killed.
    """
    # start_new_session puts yt-dlp in its own process group so the whole tree
    # can be signalled at once — see kill_process_group().
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
    )
    # Bounded so a long download cannot grow this without limit. Progress lines
    # are short and what matters on failure sits at the end anyway.
    chunks = deque(maxlen=4096)
    last_output = [time.time()]

    def pump():
        fd = proc.stdout.fileno()
        while True:
            try:
                # Read raw rather than by line: aria2c separates progress with
                # carriage returns, and readline() would block until it exits.
                chunk = os.read(fd, 65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            last_output[0] = time.time()
            chunks.append(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    deadline = time.time() + DOWNLOAD_TIMEOUT
    aborted = None
    last_progress = time.time()
    peak_bytes = 0
    while proc.poll() is None:
        time.sleep(0.5)
        now = time.time()
        on_disk = partial_size(job_id) if job_id else 0

        if on_disk > peak_bytes:
            peak_bytes = on_disk
            last_progress = now
        elif peak_bytes == 0:
            # Nothing fetched yet, so yt-dlp is still resolving the page and
            # its chatter is the only sign of life there is.
            last_progress = max(last_progress, last_output[0])

        if size_cap and on_disk > size_cap:
            aborted = f"File exceeds the {MAX_FILESIZE} size limit"
        elif now - last_progress > DOWNLOAD_STALL_TIMEOUT:
            aborted = f"Download stalled — no progress for {DOWNLOAD_STALL_TIMEOUT}s"
        elif now > deadline:
            aborted = f"Download timed out ({DOWNLOAD_TIMEOUT // 60} min limit)"
        if aborted:
            kill_process_group(proc)
            break

    proc.wait()
    reader.join(timeout=2)
    output = b"".join(chunks).decode("utf-8", "replace")
    return proc.returncode, output, aborted


def cleanup_job_files(job_id):
    for file_path in glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*")):
        try:
            os.remove(file_path)
        except OSError:
            pass


if os.environ.get("CLEAN_DOWNLOAD_DIR_ON_START") == "1":
    for file_path in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


def janitor():
    while True:
        time.sleep(60)
        cutoff = time.time() - FILE_TTL_MINUTES * 60
        for path in glob.glob(os.path.join(DOWNLOAD_DIR, "*")):
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
        for job_id, job in list(jobs.items()):
            if job["status"] in ("done", "error") and job.get("finished_at", 0) < cutoff:
                jobs.pop(job_id, None)


threading.Thread(target=janitor, daemon=True).start()


def is_authorized():
    if not AUTH_TOKEN:
        return True
    token = request.cookies.get(AUTH_COOKIE, "")
    return secrets.compare_digest(token, AUTH_TOKEN)


@app.before_request
def require_auth():
    if not AUTH_TOKEN:
        return
    if request.path in ("/healthz", "/login") or request.path.startswith("/static/"):
        return
    if is_authorized():
        return
    if request.path.startswith("/api/"):
        return jsonify({"error": "Unauthorized"}), 401
    return render_template("login.html", error=None), 401


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_TOKEN or is_authorized():
        return redirect("/")
    if request.method == "GET":
        return render_template("login.html", error=None)
    token = request.form.get("token", "")
    if not secrets.compare_digest(token, AUTH_TOKEN):
        return render_template("login.html", error="Wrong access token"), 401
    response = redirect("/")
    response.set_cookie(
        AUTH_COOKIE,
        token,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure,
    )
    return response


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    try:
        with download_slots:
            job["status"] = "downloading"
            do_download(job_id, url, format_choice, format_id)
    finally:
        job["finished_at"] = time.time()


def do_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    # --max-filesize is kept for the native downloader, which honours it. It is
    # silently ignored when aria2c does the transfer, so run_ytdlp() enforces
    # the same ceiling by watching the partial file.
    cmd = ytdlp_cmd("-o", out_template, "--max-filesize", MAX_FILESIZE, "--newline")

    if USE_ARIA2C:
        # http only. yt-dlp dropped aria2c for HLS/DASH after an RCE in fragment
        # handling, so fragmented streams stay on the native downloader.
        # --file-allocation=none stops aria2c preallocating the full size up
        # front, which would make the partial file look complete to the size
        # guard the moment it starts.
        cmd += [
            "--downloader", "http:aria2c",
            "--downloader-args",
            f"aria2c:-x{ARIA2C_CONNECTIONS} -s{ARIA2C_CONNECTIONS} -k1M "
            "--file-allocation=none --summary-interval=5",
        ]

    if DOWNLOAD_RATE_LIMIT:
        cmd += ["--limit-rate", DOWNLOAD_RATE_LIMIT]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd += ["--", url]

    try:
        returncode, output, aborted = run_ytdlp(
            cmd, job_id=job_id, size_cap=MAX_FILESIZE_BYTES
        )
        if aborted:
            job["status"] = "error"
            job["error"] = aborted
            cleanup_job_files(job_id)
            return

        if returncode != 0:
            job["status"] = "error"
            job["error"] = last_error_line(output)
            cleanup_job_files(job_id)
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            if "max-filesize" in output:
                job["error"] = f"File exceeds the {MAX_FILESIZE} size limit"
            else:
                job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:100].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        cleanup_job_files(job_id)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not valid_media_url(url):
        return jsonify({"error": "Only http:// and https:// links are accepted"}), 400

    cmd = ytdlp_cmd("-j", "--", url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = parse_ytdlp_json(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not valid_media_url(url):
        return jsonify({"error": "Only http:// and https:// links are accepted"}), 400

    # --flat-playlist only lists entries, so this stays cheap even for huge
    # playlists. Capped because every returned URL becomes its own /api/info
    # call and its own queued download.
    cmd = ytdlp_cmd(
        "--flat-playlist", "-J", "-I", f"1:{MAX_PLAYLIST_ITEMS}", "--", url,
        no_playlist=False,
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = parse_ytdlp_json(result.stdout)
        entries = info.get("entries") or []
        urls = [e.get("url") for e in entries if e.get("url")]
        return jsonify({"urls": urls, "truncated": len(urls) >= MAX_PLAYLIST_ITEMS})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/spotify", methods=["POST"])
def get_spotify_tracks():
    """Resolve a Spotify link to matched, non-DRM source URLs.

    Metadata only — see spotify.py. Returns the same ``urls`` shape as
    /api/playlist, plus what it could not match, so the UI can say so instead of
    silently dropping tracks.
    """
    data = request.json
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not valid_media_url(url):
        return jsonify({"error": "Only http:// and https:// links are accepted"}), 400

    try:
        return jsonify(spotify.resolve(url, ytdlp_cmd))
    except spotify.SpotifyError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Spotify lookup failed: {e}"}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not valid_media_url(url):
        return jsonify({"error": "Only http:// and https:// links are accepted"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "queued", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

    file_path = job["file"]
    if not os.path.exists(file_path):
        jobs.pop(job_id, None)
        return jsonify({"error": "File expired"}), 404

    return send_file(file_path, as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
