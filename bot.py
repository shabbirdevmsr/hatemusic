"""Telegram bot — vocals-only video pipeline (web-upload flow).

Flow:
  1. User sends a YouTube link.
     → Bot sends the thumbnail + direct audio download link + [✅ Done]

  2. User taps [✅ Done].
     → Bot replies with an UPLOAD LINK:
         https://your-app.blitz.cloud/upload/<sid>
     → User opens it in a browser, uploads their cleaned audio.
     → Bot detects the upload, runs FFmpeg clean + video build,
       and replies with the DIRECT VIDEO LINK + AI title + description.
     → Buttons: [🔄 Regenerate] [✏️ Edit with prompt]
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path

import requests
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
from ai import generate_metadata
from createvideo import create_video
from downloader import download_audio
from remove_silence import remove_silence
from sessions import create_session, get_session, update_session
from thumbnail import download_thumbnail, get_youtube_metadata


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "7907530787:AAEOfzOVPuCHfqMuOFVNMYQR6go5yI_fJmA",
)
LOCAL_API = os.getenv(
    "LOCAL_TELEGRAM_API",
    "https://telegram-bot-api.msr.blitz.cloud",
).rstrip("/")
FILE_SERVER = os.getenv(
    "FILE_SERVER_URL",
    "https://your-app.blitz.cloud",   # ← change to your Blitz URL
).rstrip("/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


# ============================================================
# BOT SETUP
# ============================================================

def build_session() -> AiohttpSession:
    if not LOCAL_API:
        log.info("Using cloud API (50 MB limit)")
        return AiohttpSession()
    server = TelegramAPIServer.from_base(LOCAL_API)  # NOT is_local
    log.info("Using local Bot API server: %s", LOCAL_API)
    return AiohttpSession(api=server)


bot = Bot(
    token=BOT_TOKEN,
    session=build_session(),
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ============================================================
# HELPERS
# ============================================================

VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


def extract_video_id(text: str) -> str:
    text = text.strip()
    if VIDEO_ID_RE.fullmatch(text):
        return text
    for p in (
        r"(?:v=)([A-Za-z0-9_-]{11})",
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"shorts/([A-Za-z0-9_-]{11})",
        r"embed/([A-Za-z0-9_-]{11})",
    ):
        m = re.search(p, text)
        if m:
            return m.group(1)
    raise ValueError("No YouTube video ID found.")


def upload_to_file_server(local_path: Path, kind: str = "audio") -> str:
    url = f"{FILE_SERVER}/api/upload_from_bot"
    with open(local_path, "rb") as f:
        r = requests.post(
            url,
            files={"file": (local_path.name, f, "application/octet-stream")},
            data={"kind": kind},
            timeout=300,
        )
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(data.get("error", "upload error"))
    log.info("Uploaded %s -> %s", local_path.name, data["url"])
    return data["url"]


class S(StatesGroup):
    editing_caption = State()


def kb_done(sid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Done", callback_data=f"done:{sid}")
    ]])


def kb_ai(sid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Regenerate",       callback_data=f"regen:{sid}"),
        InlineKeyboardButton(text="✏️ Edit with prompt", callback_data=f"edit:{sid}"),
    ]])


def fmt_caption_block(title: str, description: str) -> str:
    return (
        "<b>📌 Title</b> <i>(tap to copy)</i>\n"
        f"<code>{title}</code>\n\n"
        "<b>📝 Description</b> <i>(tap to copy)</i>\n"
        f"<code>{description}</code>"
    )


def build_ready_message(video_url: str, title: str, description: str) -> str:
    return (
        "✅ <b>Video ready!</b>\n\n"
        "⬇️ <b>Video download link:</b>\n"
        f"{video_url}\n\n"
        + fmt_caption_block(title, description)
    )


# ============================================================
# COMMANDS
# ============================================================

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "🎤 <b>Vocals-Only Video Pipeline</b>\n\n"
        "Send a <b>YouTube link</b> to begin."
    )


@router.message(Command("reset"))
async def cmd_reset(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("🔄 Reset. Send a YouTube link to start.")


# ============================================================
# EDIT-PROMPT HANDLER
# ============================================================

@router.message(S.editing_caption, F.text)
async def handle_edit_prompt(msg: Message, state: FSMContext):
    data = await state.get_data()
    sid = data.get("edit_sid")
    s = get_session(sid) if sid else None
    if not s:
        await msg.answer("❌ Session expired. Send a new YouTube link.")
        await state.set_state(None)
        return

    title = s.get("orig_title", "")
    desc  = s.get("orig_desc", "")
    instruction = msg.text.strip()

    wait = await msg.answer("🤖 Regenerating with your instruction...")
    try:
        ai = await asyncio.to_thread(generate_metadata, title, desc, instruction)
    except Exception as e:
        log.exception("ai edit failed")
        await wait.edit_text(f"❌ AI failed: <code>{e}</code>")
        await state.set_state(None)
        return

    update_session(sid, ai_result=ai)
    video_url = s.get("video_url", "")
    await state.set_state(None)

    try:
        await wait.edit_text(
            build_ready_message(video_url, ai["title"], ai["description"]),
            reply_markup=kb_ai(sid),
            disable_web_page_preview=True,
        )
    except Exception:
        await wait.edit_text(
            fmt_caption_block(ai["title"], ai["description"]),
            reply_markup=kb_ai(sid),
        )


# ============================================================
# YOUTUBE LINK HANDLER
# ============================================================

@router.message(F.text)
async def handle_youtube_link(msg: Message, state: FSMContext):
    try:
        video_id = extract_video_id(msg.text)
    except ValueError:
        await msg.answer("Send a valid YouTube link, or /help.")
        return

    status = await msg.answer("🖼 Fetching thumbnail...")
    try:
        meta = await asyncio.to_thread(get_youtube_metadata, video_id)
    except Exception as e:
        log.exception("metadata failed")
        await status.edit_text(f"❌ Metadata failed: <code>{e}</code>")
        return

    thumb_path = None
    if meta.get("thumbnail"):
        try:
            thumb_path = await asyncio.to_thread(
                download_thumbnail, meta["thumbnail"], video_id
            )
        except Exception as e:
            log.warning("thumbnail failed: %s", e)

    if thumb_path and Path(thumb_path).exists():
        try:
            await msg.answer_photo(
                FSInputFile(thumb_path),
                caption=f"🖼 <b>{meta['title']}</b>\n📺 {meta['channel'] or ''}",
            )
        except Exception as e:
            log.warning("photo send failed: %s", e)

    # ---- download audio ----
    await status.edit_text("📥 Downloading audio via RapidAPI...")
    try:
        audio_path = await asyncio.to_thread(
            download_audio,
            f"https://www.youtube.com/watch?v={video_id}",
            video_id,
        )
    except Exception as e:
        log.exception("download failed")
        await status.edit_text(f"❌ Download failed: <code>{e}</code>")
        return

    # ---- upload to file server ----
    await status.edit_text("📤 Uploading audio to file server...")
    try:
        audio_url = await asyncio.to_thread(
            upload_to_file_server, Path(audio_path), "audio"
        )
    except Exception as e:
        log.exception("file server upload failed")
        await status.edit_text(f"❌ Upload failed: <code>{e}</code>")
        return

    # ---- create session ----
    sid = create_session({
        "chat_id":     msg.chat.id,
        "video_id":    video_id,
        "thumb_path":  str(thumb_path) if thumb_path else "",
        "orig_title":  meta["title"] or "",
        "orig_desc":   meta["description"] or "",
    })

    await status.edit_text(
        "✅ <b>Audio ready</b>\n\n"
        "⬇️ <b>Direct download link:</b>\n"
        f"{audio_url}\n\n"
        "Clean the audio however you like, then tap <b>✅ Done</b> below "
        "to get a link where you can upload the cleaned file.",
        reply_markup=kb_done(sid),
        disable_web_page_preview=True,
    )


# ============================================================
# DONE BUTTON → send upload link + start polling
# ============================================================

@router.callback_query(F.data.startswith("done:"))
async def cb_done(cb: CallbackQuery, state: FSMContext):
    sid = cb.data.split(":", 1)[1]
    s = get_session(sid)
    if not s:
        await cb.answer("Session expired", show_alert=True)
        return

    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    upload_url = f"{FILE_SERVER}/upload/{sid}"
    await cb.message.answer(
        "📤 <b>Upload your cleaned vocal audio here:</b>\n\n"
        f"{upload_url}\n\n"
        "Open the link in a browser, pick your file, and upload it.\n"
        "I'll automatically clean it and build the video — no need to "
        "come back here.",
        disable_web_page_preview=True,
    )

    # background task: poll session until uploaded, then process
    asyncio.create_task(poll_and_process(sid, cb.message.chat.id))


async def poll_and_process(sid: str, chat_id: int):
    log.info("Polling session %s for upload", sid)
    deadline = time.time() + 1800  # 30 min

    while time.time() < deadline:
        s = get_session(sid)
        if not s:
            log.warning("session %s disappeared", sid)
            return
        if s.get("status") == "ready":
            await process_upload(sid, s, chat_id)
            return
        await asyncio.sleep(3)

    await bot.send_message(
        chat_id,
        "⏰ Upload timed out (30 min). Send a new YouTube link to start over.",
    )


async def process_upload(sid: str, s: dict, chat_id: int):
    uploaded_path = s.get("uploaded_path")
    thumb_path    = s.get("thumb_path")
    orig_title    = s.get("orig_title", "")
    orig_desc     = s.get("orig_desc", "")

    status = await bot.send_message(chat_id, "📥 File received. Cleaning audio...")

    # ---- clean ----
    try:
        cleaned = await asyncio.to_thread(remove_silence, Path(uploaded_path))
    except Exception as e:
        log.exception("clean failed")
        await status.edit_text(f"❌ Cleaning failed: <code>{e}</code>")
        return

    if not cleaned or not Path(cleaned).exists():
        await status.edit_text("❌ Cleaning produced no output.")
        return

    # ---- thumbnail check ----
    if not thumb_path or not Path(thumb_path).exists():
        await status.edit_text("❌ Thumbnail missing. Send the YouTube link again.")
        return

    # ---- build video ----
    await status.edit_text("🎬 Building 1920×1080 MP4...")
    try:
        video_path = await asyncio.to_thread(
            create_video, Path(thumb_path), Path(cleaned)
        )
    except Exception as e:
        log.exception("build failed")
        await status.edit_text(f"❌ Build failed: <code>{e}</code>")
        return

    # ---- upload ----
    size_mb = Path(video_path).stat().st_size / (1024 * 1024)
    await status.edit_text(f"📤 Uploading video ({size_mb:.1f} MB)...")
    try:
        video_url = await asyncio.to_thread(
            upload_to_file_server, Path(video_path), "video"
        )
    except Exception as e:
        log.exception("upload failed")
        await status.edit_text(f"❌ Upload failed: <code>{e}</code>")
        return

    # ---- AI ----
    await status.edit_text("🤖 Generating AI title + description...")
    try:
        ai = await asyncio.to_thread(generate_metadata, orig_title, orig_desc)
    except Exception as e:
        log.exception("ai failed")
        ai = {"title": orig_title or "Vocals only", "description": orig_desc or ""}

    update_session(sid, ai_result=ai, video_url=video_url, status="done")

    await status.edit_text(
        build_ready_message(video_url, ai["title"], ai["description"]),
        reply_markup=kb_ai(sid),
        disable_web_page_preview=True,
    )


# ============================================================
# AI CALLBACKS
# ============================================================

@router.callback_query(F.data.startswith("regen:"))
async def cb_regen(cb: CallbackQuery, state: FSMContext):
    sid = cb.data.split(":", 1)[1]
    s = get_session(sid)
    if not s:
        await cb.answer("Session expired", show_alert=True)
        return
    await cb.answer("Regenerating...")

    try:
        ai = await asyncio.to_thread(
            generate_metadata, s.get("orig_title", ""), s.get("orig_desc", "")
        )
    except Exception as e:
        await cb.message.answer(f"❌ AI failed: <code>{e}</code>")
        return

    update_session(sid, ai_result=ai)
    video_url = s.get("video_url", "")
    new_text = build_ready_message(video_url, ai["title"], ai["description"])

    try:
        await cb.message.edit_text(
            new_text, reply_markup=kb_ai(sid), disable_web_page_preview=True
        )
    except Exception:
        await cb.message.answer(
            fmt_caption_block(ai["title"], ai["description"]),
            reply_markup=kb_ai(sid),
        )


@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(cb: CallbackQuery, state: FSMContext):
    sid = cb.data.split(":", 1)[1]
    s = get_session(sid)
    if not s:
        await cb.answer("Session expired", show_alert=True)
        return
    await cb.answer()
    await state.update_data(edit_sid=sid)
    await state.set_state(S.editing_caption)
    await cb.message.answer(
        "✏️ Send an instruction for the AI.\n\n"
        "Examples:\n"
        "• make it shorter\n"
        "• add more emojis\n"
        "• use a mysterious tone\n"
        "• mention that it's remastered"
    )


# ============================================================
# FALLBACK
# ============================================================

@router.message()
async def fallback(msg: Message):
    await msg.answer("Send a YouTube link, or type /help.")


# ============================================================
# MAIN
# ============================================================

async def main():
    log.info("Starting bot...")
    for d in (config.AUDIO_DIR, config.IMAGE_DIR, config.VIDEO_DIR,
              config.UPLOAD_DIR, "state/sessions"):
        Path(d).mkdir(parents=True, exist_ok=True)

    me = await bot.get_me()
    log.info("Bot identity: @%s (id=%s)", me.username, me.id)
    log.info("File server: %s", FILE_SERVER)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")