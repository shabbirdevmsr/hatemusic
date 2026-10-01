"""Combine a static image + audio into a 1920x1080 YouTube-ratio MP4.

Key trick: a still image does not need 25 fps. Encoding at low fps cuts
processing time by 5-25x, so the encode finishes well inside any HTTP
timeout used by the host (Blitz, Render, Railway, etc.).
"""

import subprocess
from pathlib import Path

from config import VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_DIR

# 1 fps is valid on YouTube and dramatically faster to encode.
# Use 2 or 5 if you prefer slightly smoother playback metadata.
IMAGE_FPS = 1


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

    if not image_path.exists():
        raise RuntimeError(f"Image not found: {image_path}")
    if not audio_path.exists():
        raise RuntimeError(f"Audio not found: {audio_path}")

    Path(VIDEO_DIR).mkdir(parents=True, exist_ok=True)
    out = Path(VIDEO_DIR) / f"{audio_path.stem}_video.mp4"

    vf = (
        f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
        f":force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
        f":(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,"
        f"format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y",
        # --- image input: loop forever, only read it at IMAGE_FPS ---
        "-f", "image2",
        "-framerate", str(IMAGE_FPS),
        "-loop", "1",
        "-i", str(image_path),
        # --- audio input ---
        "-i", str(audio_path),
        # --- video filter ---
        "-vf", vf,
        # --- video codec ---
        "-c:v", "libx264",
        "-preset", "ultrafast",     # fastest preset; fine for a still frame
        "-tune", "stillimage",
        "-r", str(IMAGE_FPS),       # output frame rate
        "-pix_fmt", "yuv420p",
        # --- audio codec ---
        "-c:a", "aac",
        "-b:a", "192k",
        # --- stop when the shortest stream ends (audio) ---
        "-shortest",
        "-movflags", "+faststart",
        str(out),
    ]

    log("FFmpeg command:")
    log("  " + " ".join(cmd))
    log("Building video...")

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        # Show only the LAST ~40 lines so the log stays readable
        tail = "\n".join(result.stderr.splitlines()[-40:])
        log("FFmpeg stderr (tail):\n" + tail)
        raise RuntimeError(
            f"FFmpeg video build failed (exit {result.returncode}). "
            f"See log tail above."
        )

    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("FFmpeg exited 0 but produced no output")

    size_mb = out.stat().st_size / (1024 * 1024)
    dur = get_duration(out)
    log(f"Video saved: {out}  ({size_mb:.2f} MB, {dur:.1f}s)")
    return out
