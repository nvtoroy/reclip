# ReClip

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

https://github.com/user-attachments/assets/419d3e50-c933-444b-8cab-a9724986ba05

![ReClip MP3 Mode](assets/preview-mp3.png)

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- MP4 video or MP3 audio extraction
- Quality/resolution picker
- Bulk downloads — paste multiple URLs at once
- Playlist expansion — paste a playlist URL and get one card per video
- Automatic URL deduplication
- Clean, responsive UI — no frameworks, no build step
- Single Python file backend (~150 lines)

## Quick Start

```bash
brew install ffmpeg    # or: sudo apt install ffmpeg
git clone https://github.com/averygan/reclip.git
cd reclip
./reclip.sh
```

`reclip.sh` creates the venv and installs the pinned deps (including yt-dlp) itself —
you don't need a system-wide yt-dlp, and a stale one on `PATH` would only shadow the pin.

Open **http://localhost:8899**.

Or with Docker:

```bash
docker build -t reclip . && docker run -p 8899:8899 reclip
```

## Configuration

All settings are environment variables — no config files:

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_TOKEN` | *(empty — auth off)* | Access token required to use the app. **Set this before exposing to the internet.** |
| `FILE_TTL_MINUTES` | `60` | Downloaded files and finished jobs are auto-deleted after this many minutes |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Simultaneous yt-dlp downloads; extra jobs wait in a queue |
| `MAX_FILESIZE` | `2G` | Per-file size limit. Passed to yt-dlp and also enforced by watching the partial file, since yt-dlp ignores `--max-filesize` when aria2c does the transfer |
| `DOWNLOAD_TIMEOUT` | `3600` | Hard ceiling per download, in seconds. A backstop only — stalls are caught by `DOWNLOAD_STALL_TIMEOUT` |
| `DOWNLOAD_STALL_TIMEOUT` | `120` | Abort after this many seconds with no output from yt-dlp. This is what detects a dead transfer, so a slow one is left to finish |
| `USE_ARIA2C` | `1` | Use aria2c for plain-file downloads when the binary is present. Set to `0` to force yt-dlp's own downloader |
| `ARIA2C_CONNECTIONS` | `8` | Parallel connections aria2c opens per file |
| `MAX_PLAYLIST_ITEMS` | `50` | How many videos a pasted playlist URL expands to; the rest are ignored |
| `YTDLP_COOKIES_FILE` | *(empty)* | Path to a Netscape-format cookies file, passed to yt-dlp (`--cookies`). Helps with "confirm you're not a bot" checks on datacenter IPs |
| `DOWNLOAD_DIR` | `./downloads` (`/tmp/reclip-downloads` in Docker) | Where files are staged before being sent to the browser |
| `PORT` | `8899` | Listen port (honored by cloud platforms that inject `PORT`) |

## Cloud Deployment

The container is self-contained — no external storage or database. Files are staged
in a temp dir inside the container, streamed to the browser on demand, and
auto-cleaned by a background janitor after `FILE_TTL_MINUTES`.

Deploy the image to any container platform (a VPS with Docker, Fly.io, Cloud Run,
Railway, Render...). Two things to remember:

1. **Always set `AUTH_TOKEN`** — an open downloader on a public address will be
   found and abused within hours:

   ```bash
   docker run -d -p 8899:8899 -e AUTH_TOKEN=your-secret-token reclip
   ```

2. **Datacenter IPs get bot-checked.** YouTube in particular may refuse downloads
   from cloud provider IPs. If that happens, export cookies from your browser
   (e.g. with the "Get cookies.txt" extension), mount the file into the container
   and point `YTDLP_COOKIES_FILE` at it:

   ```bash
   docker run -d -p 8899:8899 \
     -e AUTH_TOKEN=your-secret-token \
     -e YTDLP_COOKIES_FILE=/data/cookies.txt \
     -v ./cookies.txt:/data/cookies.txt:ro \
     reclip
   ```

Job state lives in process memory, so run a single container instance (the
default gunicorn config already does the right thing: 1 worker, 8 threads).

## Keeping yt-dlp current

Sites break yt-dlp's extractors constantly — a download that worked last month
failing today almost always means yt-dlp needs updating, not that the site is down.

The version is pinned in `requirements.txt` so builds stay reproducible and a bad
release can be reverted in one commit. To keep that pin from going stale,
`.github/workflows/update-yt-dlp.yml` runs weekly: it bumps the pin to the latest
release, builds the image, checks the container comes up healthy, and opens a PR.
Merge it and redeploy.

Deliberately *not* done: updating yt-dlp at container start. It makes every boot
depend on the network, slows startup, and means two containers from the same image
can behave differently — bad properties for something you deploy to the cloud.

To update by hand:

```bash
pip install -U yt-dlp && pip freeze | grep -i yt-dlp
```

Then put that version in `requirements.txt` and rebuild.

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video) or **MP3** (audio)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution if available
5. Click **Download** on individual videos, or **Download All**

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

**Not supported: DRM-protected streams.** Apple Music, Tidal, Netflix and similar
are recognized by yt-dlp and deliberately refused — their streams are
Widevine-encrypted, and yt-dlp does not circumvent DRM. Spotify links are handled,
but as *metadata* only; see below.

## Spotify links (metadata matching)

Paste a Spotify track, playlist, album, or artist link and ReClip will read its
**metadata** — artist, title, duration — then look for the same recording on a
source that serves unencrypted audio and download that.

Spotify's own audio is never touched. It is Widevine-encrypted, yt-dlp refuses it
by design, and nothing here works around that. **You therefore do not get "the
Spotify file"** — you get a different upload of the same recording, matched on
metadata. Matching can be wrong, so duration is used as a guard: a candidate whose
length disagrees with Spotify's by more than `SPOTIFY_DURATION_TOLERANCE` seconds
is rejected rather than downloaded on a guess. Tracks with no confident match are
reported in the UI instead of being silently skipped.

No Spotify account, app registration, or credentials are needed. Metadata comes
from Spotify's public embed page.

**Limits, all verified rather than assumed:**

| Link | Yields |
|---|---|
| `/track/…` | that track |
| `/playlist/…` | up to **100** tracks |
| `/album/…` | up to **100** tracks |
| `/artist/…` | that artist's **top 10** |

The 100-track cap is Spotify's, not ReClip's — the embed page stops there, and the
UI says so when a playlist is larger. The official Web API is deliberately not
used: since February 2026 it returns playlist contents only for playlists the
authenticated user owns or collaborates on, and an app-only token has no user, so
registering an app would not lift the cap either. Reading all of a large playlist
would require an OAuth login as its owner, which ReClip does not implement.

This reads an *internal* structure of Spotify's embed page, not a documented API,
so Spotify can change it without notice. When that happens the lookup fails with
an explanation rather than silently returning nothing.

| Variable | Default | Purpose |
|---|---|---|
| `SPOTIFY_DURATION_TOLERANCE` | `15` | Max seconds a candidate's length may differ before it is rejected |
| `SPOTIFY_SEARCH_RESULTS` | `5` | Candidates fetched per track before ranking |
| `SPOTIFY_MATCH_WORKERS` | `4` | Concurrent metadata searches |
| `SPOTIFY_SEARCH_TIMEOUT` | `90` | Per-track search timeout, seconds |
| `SPOTIFY_HTTP_TIMEOUT` | `30` | Spotify page fetch timeout, seconds |

Respect the terms of service of every service involved, and the rights of the
people whose work you are downloading.

## Stack

- **Backend:** Python + Flask (~150 lines)
- **Frontend:** Vanilla HTML/CSS/JS (single file, no build step)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Dependencies:** 3, all pinned (Flask, gunicorn, yt-dlp)

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
