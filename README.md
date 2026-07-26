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
| `MAX_FILESIZE` | `2G` | Per-file size limit passed to yt-dlp |
| `DOWNLOAD_TIMEOUT` | `300` | Per-download timeout in seconds |
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

**Not supported: DRM-protected services.** Spotify, Apple Music, Tidal, Netflix and
similar are recognized by yt-dlp and deliberately refused — their streams are
Widevine-encrypted, and yt-dlp does not circumvent DRM. For music, use sources that
serve unencrypted audio (SoundCloud, Bandcamp, YouTube Music, podcast feeds) in MP3 mode.

## Stack

- **Backend:** Python + Flask (~150 lines)
- **Frontend:** Vanilla HTML/CSS/JS (single file, no build step)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Dependencies:** 3, all pinned (Flask, gunicorn, yt-dlp)

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
