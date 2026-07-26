"""Spotify as a metadata source.

This never touches Spotify audio. Spotify's streams are Widevine-encrypted and
yt-dlp refuses them by design; nothing here works around that. What it does is
read *metadata* — artist, title, duration — and then look for the same recording
on a source that serves unencrypted audio (YouTube), which yt-dlp downloads the
way it downloads any other link.

So you do not get "the Spotify file". You get a different upload of the same
recording, matched on metadata, and a match can be wrong. Duration is the guard:
a candidate whose length disagrees with Spotify's is rejected rather than
downloaded on a guess.

Metadata comes from Spotify's public embed page, which carries a JSON blob with
the tracklist. No account, no app registration, no credentials.

The official Web API is deliberately *not* used. Since February 2026 it returns
playlist ``items`` only for playlists the authenticated user owns or collaborates
on, and a Client Credentials token has no user at all — so registering an app
would not make arbitrary public playlists readable. The embed page is the only
path that works.

Verified limits of that page: a playlist or album yields at most 100 tracks, an
artist yields their top 10. It is also an *internal* structure rather than a
documented contract, so it can change shape without notice; the parsing here
fails loudly with an explanation rather than silently returning nothing.
"""

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

EMBED_BASE = "https://open.spotify.com/embed"
PAGE_BASE = "https://open.spotify.com"
# Spotify serves the embed JSON only to something that looks like a browser.
USER_AGENT = os.environ.get(
    "SPOTIFY_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
)

SEARCH_RESULTS = int(os.environ.get("SPOTIFY_SEARCH_RESULTS", "5"))
# Reject rather than guess past this gap between Spotify's duration and a
# candidate's. 15s is loose enough for fade-outs and silent tails, tight enough
# to throw out extended edits, live takes and hour-long loops.
DURATION_TOLERANCE = int(os.environ.get("SPOTIFY_DURATION_TOLERANCE", "15"))
MATCH_WORKERS = int(os.environ.get("SPOTIFY_MATCH_WORKERS", "4"))
HTTP_TIMEOUT = int(os.environ.get("SPOTIFY_HTTP_TIMEOUT", "30"))
SEARCH_TIMEOUT = int(os.environ.get("SPOTIFY_SEARCH_TIMEOUT", "90"))

# Verified empirically, not documented — see the module docstring.
EMBED_TRACK_CAP = 100

URL_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z]{2}/)?|spotify:)"
    r"(playlist|album|artist|track)[/:]([A-Za-z0-9]{22})"
)
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
SONG_COUNT_RE = re.compile(r'name="music:song_count" content="(\d+)"')

# Words meaning "not the recording Spotify described". Only ever used to
# penalize a candidate whose Spotify title lacks them too, so a track genuinely
# titled "... - Live" stays matchable.
NEGATIVE_WORDS = (
    "live", "cover", "remix", "karaoke", "instrumental", "acoustic",
    "sped up", "slowed", "reverb", "nightcore", "reaction", "tutorial",
    "lyrics", "lyric video", "mashup", "extended", "trailer", "teaser",
    "snippet", "preview", "loop", "1 hour", "full album",
)


class SpotifyError(Exception):
    """Message intended to be shown to the user verbatim."""


def parse_url(url):
    """Return (kind, spotify_id) for any Spotify link form, else (None, None).

    Covers open.spotify.com/track/ID, the /intl-xx/ locale prefix, the ?si=
    share suffix, and spotify:track:ID URIs.
    """
    match = URL_RE.search(url or "")
    return (match.group(1), match.group(2)) if match else (None, None)


def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Provider: embed page (no credentials)
# --------------------------------------------------------------------------

def _embed_entity(kind, spotify_id):
    url = f"{EMBED_BASE}/{kind}/{spotify_id}"
    try:
        html = _http_get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SpotifyError(
                "Spotify returned 404 — the link is private, deleted, or not "
                "publicly viewable."
            ) from e
        raise SpotifyError(f"Spotify embed page failed (HTTP {e.code})") from e
    except urllib.error.URLError as e:
        raise SpotifyError(f"Could not reach Spotify: {e.reason}") from e

    match = NEXT_DATA_RE.search(html)
    if not match:
        raise SpotifyError(
            "Spotify's embed page no longer contains the expected metadata "
            "block. This reads an internal structure that Spotify is free to "
            "change, and it appears to have changed."
        )
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        raise SpotifyError(f"Spotify's embed metadata was not valid JSON ({e})") from e

    page_props = (data.get("props") or {}).get("pageProps") or {}
    # A missing/unavailable item still returns HTTP 200; the giveaway is that
    # pageProps carries a status page instead of the entity state. Reporting
    # "structure changed" here would send you debugging the wrong thing.
    if "state" not in page_props:
        detail = page_props.get("description") or page_props.get("title") or ""
        raise SpotifyError(
            f"Spotify has no such {kind}, or it is private or region-locked"
            + (f" — Spotify says: {detail}" if detail else "")
        )
    try:
        return page_props["state"]["data"]["entity"]
    except (KeyError, TypeError) as e:
        raise SpotifyError(
            f"Could not read Spotify's embed metadata (missing {e}) — this reads "
            "an internal structure that Spotify is free to change."
        ) from e


def _artists_from_entity_field(value):
    """The embed uses a list of objects for a track and a plain string elsewhere."""
    if isinstance(value, str):
        # "Artist One, Artist Two" — the subtitle form used in tracklists.
        return [a.strip() for a in value.split(",") if a.strip()]
    names = []
    for item in value or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(item["name"].strip())
        elif isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names


def _track_from_embed_row(row):
    title = (row.get("title") or "").strip()
    artists = _artists_from_entity_field(row.get("subtitle") or row.get("artists"))
    if not title or not artists:
        return None
    duration_ms = row.get("duration") or 0
    return {
        "title": title,
        "artists": artists,
        "duration": round(duration_ms / 1000) or None,
        "spotify_url": _uri_to_url(row.get("uri")),
    }


def _uri_to_url(uri):
    if not uri or not uri.startswith("spotify:"):
        return ""
    parts = uri.split(":")
    return f"{PAGE_BASE}/{parts[1]}/{parts[2]}" if len(parts) >= 3 else ""


def _declared_track_count(kind, spotify_id):
    """Total per Spotify's own public page, so truncation can be reported."""
    if kind not in ("playlist", "album"):
        return None
    try:
        html = _http_get(f"{PAGE_BASE}/{kind}/{spotify_id}")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    match = SONG_COUNT_RE.search(html)
    return int(match.group(1)) if match else None


def _fetch_via_embed(kind, spotify_id):
    entity = _embed_entity(kind, spotify_id)
    label = (entity.get("title") or entity.get("name") or f"Spotify {kind}").strip()

    if kind == "track":
        track = _track_from_embed_row(entity)
        if not track:
            raise SpotifyError("Spotify returned no usable metadata for that track")
        return label, [track], None

    rows = entity.get("trackList") or []
    tracks = [t for t in (_track_from_embed_row(r) for r in rows) if t]
    if not tracks:
        raise SpotifyError(f"No readable tracks in that Spotify {kind}")

    note = None
    if kind == "artist":
        label = f"{label} (top tracks)"
        # Full discography is not reachable this way: the embed page exposes an
        # artist's top tracks and nothing else.
    else:
        declared = _declared_track_count(kind, spotify_id)
        if declared and declared > len(tracks):
            note = (
                f"Spotify says this {kind} has {declared} tracks but its public "
                f"embed only exposes {len(tracks)} (cap is {EMBED_TRACK_CAP})."
            )
    return label, tracks, note


def fetch_tracks(kind, spotify_id):
    """Return (label, tracks, note) for a Spotify link."""
    return _fetch_via_embed(kind, spotify_id)


# --------------------------------------------------------------------------
# Matching a track to a downloadable, non-DRM source
# --------------------------------------------------------------------------

def search_query(track):
    return f"{track['artists'][0]} - {track['title']}"


def _score(entry, track):
    """Rank a search hit. Higher is better; None means reject outright."""
    title = (entry.get("title") or "").lower()
    channel = (entry.get("channel") or entry.get("uploader") or "").lower()
    duration = entry.get("duration")
    want = track.get("duration")

    if want and duration:
        gap = abs(duration - want)
        if gap > DURATION_TOLERANCE:
            return None  # an extended edit, a live take, an hour loop — not this track
        score = 100 - gap * 4
    elif duration is None:
        score = 40  # unknown length: usable, never preferred over a verified one
    else:
        score = 50  # Spotify gave no duration, so nothing to verify against

    # "Artist - Topic" channels are YouTube's auto-generated official audio.
    if channel.endswith(" - topic"):
        score += 30
    if track["artists"][0].lower() in channel:
        score += 20
    if "official audio" in title:
        score += 15
    elif "official video" in title or "official music video" in title:
        score += 5

    source_title = track["title"].lower()
    for word in NEGATIVE_WORDS:
        if word in title and word not in source_title:
            score -= 25

    # Every listed artist appearing in the title is a good sign on features.
    if all(a.lower() in title for a in track["artists"][:2]):
        score += 10
    return score


def match_track(track, ytdlp_cmd):
    """Best non-DRM source for one track, or None if nothing matched safely."""
    cmd = ytdlp_cmd(
        f"ytsearch{SEARCH_RESULTS}:{search_query(track)}",
        "--flat-playlist", "-J", "--no-warnings",
        no_playlist=False,
    )
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=SEARCH_TIMEOUT)
        if result.returncode != 0:
            return None
        entries = (json.loads(result.stdout) or {}).get("entries") or []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return None

    best, best_score = None, None
    for entry in entries:
        # Search results sometimes include channel and playlist rows.
        if entry.get("ie_key") not in (None, "Youtube"):
            continue
        score = _score(entry, track)
        if score is None:
            continue
        if best_score is None or score > best_score:
            best, best_score = entry, score

    if not best:
        return None
    return {
        "url": best.get("url") or f"https://www.youtube.com/watch?v={best['id']}",
        "matched_title": best.get("title") or "",
        "matched_duration": best.get("duration"),
        "score": best_score,
    }


def resolve(url, ytdlp_cmd):
    """Spotify link -> matched source URLs, plus whatever could not be matched."""
    kind, spotify_id = parse_url(url)
    if not kind:
        raise SpotifyError("Not a Spotify track, playlist, album, or artist link")

    label, tracks, note = fetch_tracks(kind, spotify_id)
    if not tracks:
        raise SpotifyError(f"No downloadable tracks found in {label}")

    from concurrent.futures import ThreadPoolExecutor

    matches = [None] * len(tracks)
    with ThreadPoolExecutor(max_workers=max(1, MATCH_WORKERS)) as pool:
        futures = {pool.submit(match_track, t, ytdlp_cmd): i for i, t in enumerate(tracks)}
        for future in futures:
            try:
                matches[futures[future]] = future.result()
            except Exception:
                matches[futures[future]] = None

    urls, matched, unmatched = [], [], []
    for track, match in zip(tracks, matches):
        if match:
            urls.append(match["url"])
            matched.append({**track, **match})
        else:
            unmatched.append(search_query(track))

    return {
        "label": label,
        "kind": kind,
        "urls": urls,
        "matched": matched,
        "unmatched": unmatched,
        "total": len(tracks),
        "note": note,
    }
