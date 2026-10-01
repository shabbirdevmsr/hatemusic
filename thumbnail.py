"""Fetch YouTube metadata + download the thumbnail image."""

import requests
from pathlib import Path

from config import YT_API_URL, YT_API_KEY, IMAGE_DIR


def get_youtube_metadata(video_id: str) -> dict:
    params = {"key": YT_API_KEY, "part": "snippet", "id": video_id}
    response = requests.get(YT_API_URL, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()

    if not data.get("items"):
        raise RuntimeError(f"Video not found: {video_id}")

    snippet = data["items"][0]["snippet"]
    thumbs = snippet.get("thumbnails", {})
    thumb_url = (
        thumbs.get("maxres", {}).get("url")
        or thumbs.get("high", {}).get("url")
        or thumbs.get("medium", {}).get("url")
        or thumbs.get("default", {}).get("url")
    )

    return {
        "title":       snippet.get("title"),
        "description": snippet.get("description"),
        "thumbnail":   thumb_url,
        "channel":     snippet.get("channelTitle"),
        "video_id":    video_id,
    }


def download_thumbnail(url: str, video_id: str) -> Path:
    Path(IMAGE_DIR).mkdir(parents=True, exist_ok=True)
    out = Path(IMAGE_DIR) / f"{video_id}.jpg"

    r = requests.get(url, stream=True, timeout=60,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()

    with open(out, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)

    return out