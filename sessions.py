"""Tiny file-based session store shared between bot.py and app.py.

Both processes run in the same container, so they share the filesystem.
Each session is one JSON file under state/sessions/<id>.json
"""

import json
import time
import uuid
from pathlib import Path

SESSIONS_DIR = Path("state/sessions")


def _ensure():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def create_session(data: dict) -> str:
    _ensure()
    sid = uuid.uuid4().hex[:12]
    data = dict(data)
    data["created_at"] = time.time()
    data.setdefault("status", "waiting_upload")
    (SESSIONS_DIR / f"{sid}.json").write_text(json.dumps(data))
    return sid


def get_session(sid: str):
    p = SESSIONS_DIR / f"{sid}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def update_session(sid: str, **kwargs):
    p = SESSIONS_DIR / f"{sid}.json"
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text())
    except Exception:
        data = {}
    data.update(kwargs)
    p.write_text(json.dumps(data))


def delete_session(sid: str):
    p = SESSIONS_DIR / f"{sid}.json"
    if p.exists():
        p.unlink()