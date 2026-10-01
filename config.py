"""Shared configuration for all modules."""

import os

# ============================================================
# RapidAPI YouTube Download
# ============================================================
RAPIDAPI_KEY = os.getenv(
    "RAPIDAPI_KEY",
    "41ac3cccb3msh09cce7da9f3d0b3p17f5d8jsne0438b971bdd",
)
RAPIDAPI_HOST         = "youtube-info-download-api.p.rapidapi.com"
RAPIDAPI_DOWNLOAD_URL = f"https://{RAPIDAPI_HOST}/ajax/download.php"
RAPIDAPI_PROGRESS_URL = f"https://{RAPIDAPI_HOST}/ajax/progress"

# ============================================================
# Groq AI
# ============================================================
GROQ_API_KEY = os.getenv(
    "GROQ_API_KEY",
    "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",
)
GROQ_MODEL = "openai/gpt-oss-120b"

# ============================================================
# YouTube metadata mirror
# ============================================================
YT_API_URL = "https://ytapi.apps.mattw.io/v3/videos"
YT_API_KEY = os.getenv("YT_API_KEY", "foo1")

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
