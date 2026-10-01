"""Shared configuration for all modules."""

import os

# ============================================================
# Secrets — read from environment, with local fallbacks
# ============================================================
GROQ_API_KEY = os.getenv(
    "GROQ_API_KEY",
    "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",
)

YT_API_KEY = os.getenv("YT_API_KEY", "foo1")

# ---- yt-dlp cookie options ----
USE_BROWSER_COOKIES = os.getenv("USE_BROWSER_COOKIES") or None
COOKIE_FILE         = os.getenv("COOKIE_FILE") or None

# ============================================================
# yt-dlp
# ============================================================
AUDIO_FORMAT  = "mp3"     # mp3, m4a, wav, flac, opus
AUDIO_QUALITY = "0"       # 0 = best VBR; or "320K" for fixed bitrate

# ============================================================
# Groq AI
# ============================================================
GROQ_MODEL = "openai/gpt-oss-120b"

# ============================================================
# YouTube metadata mirror
# ============================================================
YT_API_URL = "https://ytapi.apps.mattw.io/v3/videos"

# ============================================================
# Video
# ============================================================
VIDEO_WIDTH  = 1920
VIDEO_HEIGHT = 1080

# ============================================================
# Silence / noise removal
# ============================================================
SILENCE_THRESHOLD      = "-35dB"
MIN_SILENCE_DURATION   = 0.6
MIN_SEGMENT_DURATION   = 0.15
ENABLE_NOISE_REDUCTION = True
NOISE_REDUCTION_DB     = 12
NOISE_FLOOR_DB         = -40

# ============================================================
# Working directories
# ============================================================
AUDIO_DIR  = "audio"
IMAGE_DIR  = "images"
VIDEO_DIR  = "videos"
UPLOAD_DIR = "uploads"

# ============================================================
# Flask
# ============================================================
FLASK_HOST = "0.0.0.0"
FLASK_PORT = int(os.getenv("PORT", "5000"))