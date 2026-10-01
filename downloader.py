"""Download audio from YouTube using the RapidAPI YouTube Download API.

Flow:
  1. GET /ajax/download.php  -> submit job, returns `id` + `progress_url`
  2. GET progress_url        -> poll until progress >= 1000, then get download_url
  3. GET download_url        -> save the MP3 locally
"""

import time
import requests
from pathlib import Path

from config import (
    RAPIDAPI_KEY,
    RAPIDAPI_HOST,
    RAPIDAPI_DOWNLOAD_URL,
    AUDIO_DIR,
)


def _headers():
    return {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key":  RAPIDAPI_KEY,
    }


def download_audio(video_url: str, video_id: str = None, log=print) -> Path:
    """Download bestaudio from YouTube using RapidAPI and save as MP3."""

    Path(AUDIO_DIR).mkdir(parents=True, exist_ok=True)

    log(f"[RapidAPI] Submitting download job for {video_id or video_url}...")

    params = {"url": video_url, "format": "mp3"}

    r = requests.get(
        RAPIDAPI_DOWNLOAD_URL,
        headers=_headers(),
        params=params,
        timeout=30,
    )

    if not r.ok:
        raise RuntimeError(
            f"RapidAPI download failed: {r.status_code} {r.text[:300]}"
        )

    data = r.json()

    if not data.get("success"):
        raise RuntimeError(f"RapidAPI returned success=false: {data}")

    job_id       = data.get("id")
    progress_url = data.get("progress_url")

    if not job_id or not progress_url:
        raise RuntimeError(f"Missing id or progress_url in response: {data}")

    log(f"[RapidAPI] Job ID: {job_id}")
    title = data.get("title")
    if title:
        log(f"[RapidAPI] Title : {title}")

    download_url = None
    deadline = time.time() + 600

    while time.time() < deadline:
        pr = requests.get(progress_url, timeout=30)

        if not pr.ok:
            log(f"  poll HTTP {pr.status_code}")
            time.sleep(3)
            continue

        state    = pr.json()
        progress = state.get("progress", 0)

        if progress >= 1000:
            download_url = state.get("download_url")
            if download_url:
                break

        log(f"  progress: {progress / 10:.1f}%")
        time.sleep(3)

    if not download_url:
        raise RuntimeError("Timed out waiting for RapidAPI download URL")

    log("[RapidAPI] Download URL ready, saving file...")

    stem = video_id or job_id
    out  = Path(AUDIO_DIR) / f"{stem}.mp3"

    with requests.get(download_url, stream=True, timeout=300) as audio_resp:
        audio_resp.raise_for_status()
        with open(out, "wb") as f:
            for chunk in audio_resp.iter_content(8192):
                f.write(chunk)

    size_mb = out.stat().st_size / (1024 * 1024)
    log(f"[RapidAPI] Saved: {out}  ({size_mb:.2f} MB)")

    return out