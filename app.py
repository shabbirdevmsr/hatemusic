"""Flask web app — also serves files for the Telegram bot."""

import re
import shutil
import traceback
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

import config
from thumbnail      import get_youtube_metadata, download_thumbnail
from downloader     import download_audio
from remove_silence import remove_silence
from createvideo    import create_video
from ai             import generate_metadata


app = Flask(__name__)

for d in (config.AUDIO_DIR, config.IMAGE_DIR, config.VIDEO_DIR, config.UPLOAD_DIR):
    Path(d).mkdir(parents=True, exist_ok=True)


# ============================================================
# HELPERS
# ============================================================

VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def extract_video_id(url_or_id: str) -> str:
    url_or_id = url_or_id.strip()
    if VIDEO_ID_RE.fullmatch(url_or_id):
        return url_or_id
    for p in (
        r"(?:v=)([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"shorts/([A-Za-z0-9_-]{11})",
        r"embed/([A-Za-z0-9_-]{11})",
    ):
        m = re.search(p, url_or_id)
        if m:
            return m.group(1)
    raise ValueError(f"Cannot extract video ID from: {url_or_id}")


def check_ffmpeg():
    return [p for p in ("ffmpeg", "ffprobe") if shutil.which(p) is None]


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def api_health():
    missing = check_ffmpeg()
    return jsonify({"ok": not missing, "missing": missing})


# ---------- DIRECT FILE SERVER (for the Telegram bot) ----------

@app.route("/files/audio/<path:name>")
def serve_audio(name):
    return send_from_directory(config.AUDIO_DIR, name, as_attachment=True)


@app.route("/files/video/<path:name>")
def serve_video(name):
    return send_from_directory(config.VIDEO_DIR, name, as_attachment=True)


@app.route("/files/image/<path:name>")
def serve_image(name):
    return send_from_directory(config.IMAGE_DIR, name, as_attachment=False)


# ---------- BOT → APP: save a file from the bot and return a URL ----------

@app.route("/api/upload_from_bot", methods=["POST"])
def api_upload_from_bot():
    """The bot POSTs a file here; we save it and return a public URL."""
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "error": "no file"}), 400

        f = request.files["file"]
        kind = request.form.get("kind", "audio")   # "audio" | "video"
        safe = secure_filename(f.filename or "file.bin")

        if kind == "video":
            dest_dir = Path(config.VIDEO_DIR)
            url_prefix = "video"
        else:
            dest_dir = Path(config.AUDIO_DIR)
            url_prefix = "audio"

        dest = dest_dir / safe
        f.save(str(dest))

        size_mb = dest.stat().st_size / (1024 * 1024)
        url = f"{request.host_url.rstrip('/')}/files/{url_prefix}/{safe}"

        return jsonify({
            "success": True,
            "url": url,
            "name": safe,
            "size_mb": round(size_mb, 2),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- existing pipeline routes (unchanged) ----------

@app.route("/api/metadata", methods=["POST"])
def api_metadata():
    try:
        data = request.get_json(force=True)
        video_id = extract_video_id(data.get("url", ""))
        meta = get_youtube_metadata(video_id)
        thumb_name = None
        if meta.get("thumbnail"):
            path = download_thumbnail(meta["thumbnail"], video_id)
            thumb_name = path.name
        return jsonify({
            "success": True,
            "video_id": video_id,
            "title": meta["title"],
            "description": meta["description"],
            "channel": meta["channel"],
            "thumbnail_url": f"/api/file/images/{thumb_name}" if thumb_name else None,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def api_download():
    try:
        data = request.get_json(force=True)
        video_id = data.get("video_id") or extract_video_id(data.get("url", ""))
        logs = []
        audio_url  = f"https://www.youtube.com/watch?v={video_id}"
        audio_path = download_audio(audio_url, video_id=video_id,
                                    log=lambda m: logs.append(str(m)))
        return jsonify({
            "success": True,
            "video_id": video_id,
            "audio_name": audio_path.name,
            "audio_url": f"/api/file/audio/{audio_path.name}",
            "logs": logs,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/clean", methods=["POST"])
def api_clean():
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "error": "No file"}), 400
        f = request.files["file"]
        safe = secure_filename(f.filename)
        upload_path = Path(config.UPLOAD_DIR) / safe
        f.save(str(upload_path))
        logs = []
        cleaned = remove_silence(upload_path, log=lambda m: logs.append(str(m)))
        if not cleaned:
            return jsonify({"success": False, "error": "Cleaning failed", "logs": logs}), 500
        final_path = Path(config.AUDIO_DIR) / cleaned.name
        if final_path.exists():
            final_path.unlink()
        shutil.move(str(cleaned), str(final_path))
        return jsonify({
            "success": True,
            "cleaned_name": final_path.name,
            "cleaned_url": f"/api/file/audio/{final_path.name}",
            "logs": logs,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/build_video", methods=["POST"])
def api_build_video():
    logs = []
    try:
        data = request.get_json(force=True)
        audio_name = data["audio_name"]
        video_id   = data["video_id"]
        audio_path = Path(config.AUDIO_DIR) / audio_name
        thumb_path = Path(config.IMAGE_DIR) / f"{video_id}.jpg"
        if not audio_path.exists():
            return jsonify({"success": False, "error": "Audio not found", "logs": logs}), 404
        if not thumb_path.exists():
            return jsonify({"success": False, "error": "Thumbnail not found", "logs": logs}), 404
        video_path = create_video(thumb_path, audio_path,
                                  log=lambda m: logs.append(str(m)))
        return jsonify({
            "success": True,
            "video_name": video_path.name,
            "video_url": f"/api/file/videos/{video_path.name}",
            "logs": logs,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e), "logs": logs}), 500


@app.route("/api/ai_metadata", methods=["POST"])
def api_ai_metadata():
    try:
        data = request.get_json(force=True)
        meta = generate_metadata(data.get("title", ""), data.get("description", ""))
        return jsonify({"success": True, **meta})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/file/<folder>/<path:name>")
def api_file(folder, name):
    allowed = {
        "audio":   config.AUDIO_DIR,
        "videos":  config.VIDEO_DIR,
        "images":  config.IMAGE_DIR,
        "uploads": config.UPLOAD_DIR,
    }
    if folder not in allowed:
        return jsonify({"error": "bad folder"}), 400
    return send_from_directory(allowed[folder], name, as_attachment=False)


if __name__ == "__main__":
    missing = check_ffmpeg()
    print("=" * 60)
    print(" Vocals-Only Video Pipeline (RapidAPI + Flask)")
    print("=" * 60)
    if missing:
        print(f"WARNING: missing on PATH -> {', '.join(missing)}")
    print(f"Local dev: http://127.0.0.1:{config.FLASK_PORT}")
    print()
    app.run(host="127.0.0.1", port=config.FLASK_PORT, debug=True)
