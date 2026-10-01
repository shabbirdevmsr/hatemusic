"""Combine a static image + audio into a 1920x1080 YouTube-ratio MP4."""

import subprocess
from pathlib import Path

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_DIR


def get_duration(filename) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1",
         str(filename)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def create_video(image_path, audio_path, log=print) -> Path:
    image_path = Path(image_path)
    audio_path = Path(audio_path)

    Path(VIDEO_DIR).mkdir(parents=True, exist_ok=True)
    out = Path(VIDEO_DIR) / f"{audio_path.stem}_video.mp4"

    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
        f":force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
        f":(ow-iw)/2:(oh-ih)/2:color=black,"
        f"format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-framerate", "2",
        "-i", str(image_path),
        "-i", str(audio_path),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "stillimage",
        "-r", "2",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        "-movflags", "+faststart",
        str(out),
    ]

    log("Building video...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log("FFmpeg stderr:\n" + result.stderr[-2000:])
        raise RuntimeError("FFmpeg video build failed")

    size_mb = out.stat().st_size / (1024 * 1024)
    log(f"Video saved: {out}  ({size_mb:.2f} MB)")
    return out