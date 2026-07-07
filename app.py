import os
import time
import uuid
import glob
import json
import secrets
import subprocess
import threading
from flask import Flask, request, jsonify, send_file, render_template, redirect

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
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "300"))
# Optional Netscape-format cookies file for yt-dlp — helps with bot checks on datacenter IPs.
YTDLP_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "")

jobs = {}
download_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)


def ytdlp_cmd(*args):
    cmd = ["yt-dlp", "--no-playlist"]
    if YTDLP_COOKIES_FILE:
        cmd += ["--cookies", YTDLP_COOKIES_FILE]
    cmd += list(args)
    return cmd


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

    cmd = ytdlp_cmd("-o", out_template, "--max-filesize", MAX_FILESIZE)

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            cleanup_job_files(job_id)
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            if "max-filesize" in result.stdout or "max-filesize" in result.stderr:
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
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = f"Download timed out ({DOWNLOAD_TIMEOUT // 60} min limit)"
        cleanup_job_files(job_id)
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

    cmd = ytdlp_cmd("-j", url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

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


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

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
