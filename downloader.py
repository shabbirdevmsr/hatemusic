"""Download audio from a YouTube URL using yt-dlp.

yt-dlp is the best maintained fork of youtube-dl:
  - Actively updated (YouTube breaks things constantly)
  - Handles age-gate, region lock, cookies, playlists, etc.
  - Native Python API
  - Uses FFmpeg post-processors for audio extraction
"""

import yt_dlp
from pathlib import Path

from config import (
    AUDIO_DIR, AUDIO_FORMAT, AUDIO_QUALITY,
    USE_BROWSER_COOKIES, COOKIE_FILE,
)


def _make_ydl_opts(video_id: str, log):
    """Build yt-dlp options dict."""

    def _hook(d):
        if d["status"] == "downloading":
            pct   = d.get("_percent_str", "").strip()
            speed = d.get("_speed_str", "").strip()
            eta   = d.get("_eta_str", "").strip()
            log(f"  {pct}  speed {speed}  eta {eta}")
        elif d["status"] == "finished":
            log("  download complete, converting to mp3...")

    opts = {
        # ---- format / output ----
        "format": "bestaudio/best",
        "outtmpl": str(Path(AUDIO_DIR) / f"{video_id}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [_hook],

        # ---- post-processing ----
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": AUDIO_FORMAT,
            "preferredquality": AUDIO_QUALITY,
        }],
        "keepvideo": False,

        # ---- network ----
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "geo_bypass": True,

        # ---- bot-check mitigation ----
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "skip": ["hls", "dash"],
            }
        },
    }

    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    elif USE_BROWSER_COOKIES:
        opts["cookiesfrombrowser"] = (USE_BROWSER_COOKIES,)

    return opts


def download_audio(video_id: str, log=print) -> Path:
    """Download bestaudio from YouTube and convert to MP3."""

    Path(AUDIO_DIR).mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={video_id}"

    log(f"[yt-dlp] Fetching audio for {video_id}...")
    opts = _make_ydl_opts(video_id, log)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in to confirm" in msg or "bot" in msg.lower():
            raise RuntimeError(
                "YouTube is asking for sign-in (bot check). "
                "Set USE_BROWSER_COOKIES or COOKIE_FILE in config."
            ) from e
        raise RuntimeError(f"yt-dlp download failed: {msg}") from e

    out = Path(AUDIO_DIR) / f"{video_id}.{AUDIO_FORMAT}"

    if not out.exists():
        candidates = list(Path(AUDIO_DIR).glob(f"{video_id}.*"))
        audio_only = [c for c in candidates if c.suffix != ".webm"]
        if audio_only:
            out = audio_only[0]
        else:
            raise RuntimeError(f"Downloaded but file not found in {AUDIO_DIR}/")

    size_mb  = out.stat().st_size / (1024 * 1024)
    duration = info.get("duration", 0)
    log(f"[yt-dlp] Saved : {out}  ({size_mb:.2f} MB, {duration}s)")
    log(f"[yt-dlp] Title : {info.get('title')}")

    return out