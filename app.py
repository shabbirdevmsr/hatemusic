"""Flask web app — serves the upload page and hosts file links for the bot."""

import re
import shutil
import traceback
from pathlib import Path

from flask import (
    Flask, render_template, render_template_string, request,
    jsonify, send_from_directory,
)
from werkzeug.utils import secure_filename

import config
from sessions      import create_session, get_session, update_session
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
# UPLOAD PAGE (browser)
# ============================================================

UPLOAD_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload cleaned audio</title>
<style>
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; padding:20px;
         font-family:-apple-system,"Segoe UI",Roboto,sans-serif;
         background:#0e1116; color:#e6edf3; }
  .card { background:#161b22; border:1px solid #21262d; border-radius:12px;
          padding:28px; max-width:480px; width:100%; }
  h1 { margin:0 0 8px; font-size:20px; }
  p  { color:#8b949e; margin:0 0 20px; font-size:14px; line-height:1.5; }
  input[type=file] { width:100%; padding:12px; border-radius:8px;
                     border:1px dashed #30363d; background:#0d1117;
                     color:#e6edf3; font-size:14px; margin-bottom:16px;
                     cursor:pointer; }
  button { width:100%; padding:14px; background:#238636; color:#fff; border:0;
           border-radius:8px; font-size:15px; font-weight:600; cursor:pointer; }
  button:hover { background:#2ea043; }
  button:disabled { opacity:0.6; cursor:not-allowed; }
  .progress { margin-top:16px; height:6px; background:#0d1117;
              border-radius:3px; overflow:hidden; display:none; }
  .progress > div { height:100%; width:0; background:#238636;
                    transition:width .2s; }
  .status { margin-top:16px; font-size:14px; text-align:center; min-height:20px; }
  .ok  { color:#7ee787; }
  .err { color:#ffa198; }
</style>
</head>
<body>
<div class="card">
  <h1>📤 Upload cleaned audio</h1>
  <p>Pick the file you cleaned. It will be sent to the bot automatically.</p>
  <input type="file" id="file" accept="audio/*">
  <button id="upload">Upload</button>
  <div class="progress" id="progressWrap"><div id="progress"></div></div>
  <div class="status" id="status"></div>
</div>
<script>
const fileInput = document.getElementById("file");
const btn       = document.getElementById("upload");
const status    = document.getElementById("status");
const progWrap  = document.getElementById("progressWrap");
const prog      = document.getElementById("progress");
const SID       = {{ sid|tojson }};

btn.onclick = () => {
  const f = fileInput.files[0];
  if (!f) { status.className = "status err"; status.textContent = "Pick a file first."; return; }

  const fd = new FormData();
  fd.append("file", f);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload_audio/" + SID);

  progWrap.style.display = "block";
  btn.disabled = true;
  status.className = "status";
  status.textContent = "Uploading...";

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = (e.loaded / e.total * 100).toFixed(0);
      prog.style.width = pct + "%";
      status.textContent = "Uploading... " + pct + "%";
    }
  };

  xhr.onload = () => {
    btn.disabled = false;
    try {
      const data = JSON.parse(xhr.responseText);
      if (data.success) {
        status.className = "status ok";
        status.textContent = "✅ " + (data.message || "Uploaded! You can close this tab.");
        prog.style.width = "100%";
      } else {
        status.className = "status err";
        status.textContent = "❌ " + (data.error || "Upload failed");
      }
    } catch (e) {
      status.className = "status err";
      status.textContent = "❌ " + xhr.responseText.slice(0, 200);
    }
  };

  xhr.onerror = () => {
    btn.disabled = false;
    status.className = "status err";
    status.textContent = "❌ Network error";
  };

  xhr.send(fd);
};
</script>
</body>
</html>
"""


@app.route("/upload/<sid>")
def upload_page(sid):
    s = get_session(sid)
    if not s:
        return "Session not found or expired.", 404
    return render_template_string(UPLOAD_PAGE, sid=sid)


@app.route("/api/upload_audio/<sid>", methods=["POST"])
def api_upload_audio(sid):
    try:
        s = get_session(sid)
        if not s:
            return jsonify({"success": False, "error": "session not found"}), 404

        if "file" not in request.files:
            return jsonify({"success": False, "error": "no file"}), 400

        f = request.files["file"]
        safe = secure_filename(f.filename or "audio.mp3")
        Path(config.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
        dest = Path(config.UPLOAD_DIR) / f"web_{sid}_{safe}"
        f.save(str(dest))

        size_mb = dest.stat().st_size / (1024 * 1024)
        update_session(
            sid,
            status="ready",
            uploaded_path=str(dest),
            uploaded_name=safe,
            uploaded_size_mb=round(size_mb, 2),
        )
        return jsonify({
            "success": True,
            "message": f"File received ({size_mb:.1f} MB). You can close this tab.",
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/session_status/<sid>")
def api_session_status(sid):
    s = get_session(sid)
    if not s:
        return jsonify({"success": False, "error": "not found"}), 404
    return jsonify({
        "success": True,
        "status": s.get("status"),
        "uploaded_name": s.get("uploaded_name"),
    })


# ============================================================
# DIRECT FILE SERVER (for bot links)
# ============================================================

@app.route("/files/audio/<path:name>")
def serve_audio(name):
    return send_from_directory(config.AUDIO_DIR, name, as_attachment=True)


@app.route("/files/video/<path:name>")
def serve_video(name):
    return send_from_directory(config.VIDEO_DIR, name, as_attachment=True)


@app.route("/files/image/<path:name>")
def serve_image(name):
    return send_from_directory(config.IMAGE_DIR, name, as_attachment=False)


# ============================================================
# BOT → APP: save a file from the bot
# ============================================================

@app.route("/api/upload_from_bot", methods=["POST"])
def api_upload_from_bot():
    try:
        if "file" not in request.files:
            return jsonify({"success": False, "error": "no file"}), 400
        f = request.files["file"]
        kind = request.form.get("kind", "audio")
        safe = secure_filename(f.filename or "file.bin")

        if kind == "video":
            dest_dir, url_prefix = Path(config.VIDEO_DIR), "video"
        else:
            dest_dir, url_prefix = Path(config.AUDIO_DIR), "audio"

        dest = dest_dir / safe
        f.save(str(dest))

        url = f"{request.host_url.rstrip('/')}/files/{url_prefix}/{safe}"
        return jsonify({
            "success": True,
            "url": url,
            "name": safe,
            "size_mb": round(dest.stat().st_size / (1024 * 1024), 2),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# EXISTING PIPELINE ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": not check_ffmpeg(), "missing": check_ffmpeg()})


@app.route("/api/metadata", methods=["POST"])
def api_metadata():
    try:
        data = request.get_json(force=True)
        video_id = extract_video_id(data.get("url", ""))
        meta = get_youtube_metadata(video_id)
        thumb_name = None
        if meta.get("thumbnail"):
            p = download_thumbnail(meta["thumbnail"], video_id)
            thumb_name = p.name
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
        ap = download_audio(
            f"https://www.youtube.com/watch?v={video_id}",
            video_id=video_id, log=lambda m: logs.append(str(m)),
        )
        return jsonify({
            "success": True,
            "video_id": video_id,
            "audio_name": ap.name,
            "audio_url": f"/api/file/audio/{ap.name}",
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
        up = Path(config.UPLOAD_DIR) / safe
        f.save(str(up))
        logs = []
        cleaned = remove_silence(up, log=lambda m: logs.append(str(m)))
        if not cleaned:
            return jsonify({"success": False, "error": "Cleaning failed", "logs": logs}), 500
        final = Path(config.AUDIO_DIR) / cleaned.name
        if final.exists():
            final.unlink()
        shutil.move(str(cleaned), str(final))
        return jsonify({
            "success": True,
            "cleaned_name": final.name,
            "cleaned_url": f"/api/file/audio/{final.name}",
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
    print(" Vocals-Only Pipeline (Flask + bot helper)")
    print("=" * 60)
    if missing:
        print(f"WARNING: missing -> {', '.join(missing)}")
    print(f"Local: http://127.0.0.1:{config.FLASK_PORT}")
    app.run(host="127.0.0.1", port=config.FLASK_PORT, debug=True)