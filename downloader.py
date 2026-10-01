"""Download audio from YouTube using the RapidAPI YouTube Download API.

The API works in three steps:
  1. GET /ajax/download.php  -> submit job, returns an `id`
  2. GET /ajax/progress      -> poll until progress == 1000, then get download_url
  3. GET download_url        -> save the MP3 locally
"""

import time
import requests
from pathlib import Path

from config import (
    RAPIDAPI_KEY,
    RAPIDAPI_HOST,
    RAPIDAPI_DOWNLOAD_URL,
    RAPIDAPI_PROGRESS_URL,
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

    # ---------- Step 1: submit job ----------
    log(f"[RapidAPI] Submitting download job for {video_id or video_url}...")

    params = {"url": video_url, "format": "mp3"}

    r = requests.get(
        RAPIDAPI_DOWNLOAD_URL,
        headers=_headers(),
        params=params,
        timeout=30,
    )

    if not r.ok:
        raise RuntimeError(f"RapidAPI download failed: {r.status_code} {r.text[:300]}")

    data = r.json()
    job_id = data.get("id")
    if not job_id:
        raise RuntimeError(f"No job id returned: {data}")

    log(f"[RapidAPI] Job ID: {job_id}")
    title = data.get("title")
    if title:
        log(f"[RapidAPI] Title : {title}")

    # ---------- Step 2: poll progress ----------
    download_url = None
    deadline = time.time() + 600  # 10 minutes max

    while time.time() < deadline:
        pr = requests.get(
            RAPIDAPI_PROGRESS_URL,
            headers=_headers(),
            params={"id": job_id},
            timeout=30,
        )

        if not pr.ok:
            log(f"  poll HTTP {pr.status_code}")
            time.sleep(3)
            continue

        state    = pr.json()
        progress = state.get("progress", 0)

        # API reports 0..1000
        if progress == 1000:
            download_url = state.get("download_url")
            if download_url:
                break

        log(f"  progress: {progress / 10:.1f}%")
        time.sleep(3)

    if not download_url:
        raise RuntimeError("Timed out waiting for RapidAPI download URL")

    log("[RapidAPI] Download URL ready, saving file...")

    # ---------- Step 3: save the MP3 ----------
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
