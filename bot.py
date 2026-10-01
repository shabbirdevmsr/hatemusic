"""Telegram bot — vocals-only video pipeline.

Uses the local Bot API server at telegram-bot-api.msr.blitz.cloud
(raises the upload limit from 50 MB to ~2000 MB).

Flow:
  1. Send a YT link   → bot sends thumbnail + raw audio + auto-cleaned audio
  2. (Optional) Upload your own audio → bot auto-cleans it
  3. Send another YT link → bot builds 1920×1080 MP4 → sends the video
  4. Tap "📝 Get Title & Description" → AI caption
     with [🔄 Regenerate] + [✏️ Edit with prompt]

Run:
    python bot.py
"""

import asyncio
import logging
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatAction, ParseMode
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
from thumbnail import download_thumbnail, get_youtube_metadata


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "6965134031:AAHdZxo1I4WuV4pvhEgfN24UOzPPL2R76oE",
)

# Local Bot API server (2 GB uploads instead of 50 MB)
LOCAL_API = os.getenv(
    "LOCAL_TELEGRAM_API",
    "https://telegram-bot-api.msr.blitz.cloud",
).rstrip("/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


# ============================================================
# BOT + DISPATCHER (local Bot API server)
# ============================================================

def build_session() -> AiohttpSession:
    """Return an AiohttpSession using the local Bot API server.

    is_local=True tells aiogram it can:
      - upload files > 50 MB
      - pass local file paths directly to the server
      - download files without hitting the cloud API
    """
    if not LOCAL_API:
        log.info("No LOCAL_TELEGRAM_API set — using cloud API (50 MB limit)")
        return AiohttpSession()

    try:
        server = TelegramAPIServer.from_base(LOCAL_API, is_local=True)
        log.info("Using local Bot API server: %s", LOCAL_API)
        return AiohttpSession(api=server)
    except Exception as e:
        log.exception("Local API config failed, falling back to cloud: %s", e)
        return AiohttpSession()


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
    raise ValueError("No YouTube video ID found in that text.")


class S(StatesGroup):
    editing_caption = State()


def kb_ai_initial():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="📝 Get Title & Description",
            callback_data="gen_ai",
        )
    ]])


def kb_ai_after():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Regenerate",       callback_data="regen_ai"),
        InlineKeyboardButton(text="✏️ Edit with prompt", callback_data="edit_ai"),
    ]])


def fmt_ai(result: dict) -> str:
    return (
        f"<b>📌 Title</b>\n{result['title']}\n\n"
        f"<b>📝 Description</b>\n{result['description']}"
    )


async def safe_send_document(msg: Message, path, caption: str):
    """Send a document; never crash if the file is missing."""
    p = Path(path)
    if not p.exists():
        log.warning("file missing, cannot send: %s", p)
        await msg.answer(f"⚠️ File missing: <code>{p.name}</code>")
        return
    try:
        await msg.answer_document(FSInputFile(p), caption=caption)
    except Exception as e:
        log.exception("send document failed")
        await msg.answer(f"⚠️ Could not send file: <code>{e}</code>")


# ============================================================
# COMMANDS
# ============================================================

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await state.update_data(mode="source")
    await msg.answer(
        "🎤 <b>Vocals-Only Video Pipeline</b>\n\n"
        "Send a <b>YouTube link</b> → I will:\n"
        "  1️⃣ Fetch the thumbnail\n"
        "  2️⃣ Download the audio via RapidAPI\n"
        "  3️⃣ Auto-remove silence + background noise\n\n"
        "Then:\n"
        "  • Upload your own cleaned audio (optional)\n"
        "  • Send another YouTube link → I build the 1920×1080 MP4\n"
        "    using that link's thumbnail + the cleaned audio\n"
        "  • Tap <b>📝 Get Title &amp; Description</b> for AI captions\n\n"
        "/help for details, /reset to start over."
    )


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "<b>How to use</b>\n\n"
        "1. Send a YouTube URL → thumbnail + audio + auto-cleaned audio\n"
        "2. Upload your own audio → I auto-clean it\n"
        "3. Send a YouTube URL → I build the MP4 (its thumbnail + cleaned audio)\n"
        "4. Tap <b>📝 Get Title &amp; Description</b> → AI caption\n"
        "   • <b>🔄 Regenerate</b> → try again\n"
        "   • <b>✏️ Edit with prompt</b> → send a custom instruction\n\n"
        "/reset — start a new project\n"
    )


@router.message(Command("reset"))
async def cmd_reset(msg: Message, state: FSMContext):
    await state.clear()
    await state.update_data(mode="source")
    await msg.answer("🔄 Reset done. Send a YouTube link to start.")


# ============================================================
# AI CALLBACKS
# ============================================================

@router.callback_query(F.data == "gen_ai")
async def cb_gen_ai(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Generating...")
    data  = await state.get_data()
    title = data.get("orig_title", "")
    desc  = data.get("orig_desc", "")

    if not title:
        await cb.message.answer("❌ No metadata stored. Run the pipeline again.")
        return

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    wait = await cb.message.answer("🤖 Asking AI...")
    try:
        result = await asyncio.to_thread(generate_metadata, title, desc)
    except Exception as e:
        log.exception("ai gen failed")
        await wait.edit_text(f"❌ AI failed: <code>{e}</code>")
        return

    await wait.edit_text(fmt_ai(result), reply_markup=kb_ai_after())
    await state.update_data(ai_result=result)


@router.callback_query(F.data == "regen_ai")
async def cb_regen(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Regenerating...")
    data  = await state.get_data()
    title = data.get("orig_title", "")
    desc  = data.get("orig_desc", "")

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    wait = await cb.message.answer("🤖 Asking AI again...")
    try:
        result = await asyncio.to_thread(generate_metadata, title, desc)
    except Exception as e:
        log.exception("ai regen failed")
        await wait.edit_text(f"❌ AI failed: <code>{e}</code>")
        return

    await wait.edit_text(fmt_ai(result), reply_markup=kb_ai_after())
    await state.update_data(ai_result=result)


@router.callback_query(F.data == "edit_ai")
async def cb_edit(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await state.set_state(S.editing_caption)
    await cb.message.answer(
        "✏️ Send an instruction for the AI.\n\n"
        "<i>Examples:</i>\n"
        "• make it shorter\n"
        "• add more emojis\n"
        "• use a mysterious tone\n"
        "• mention that it's remastered"
    )


@router.message(S.editing_caption, F.text)
async def handle_edit_prompt(msg: Message, state: FSMContext):
    data  = await state.get_data()
    title = data.get("orig_title", "")
    desc  = data.get("orig_desc", "")
    instruction = msg.text.strip()

    wait = await msg.answer("🤖 Regenerating with your instruction...")
    try:
        result = await asyncio.to_thread(
            generate_metadata, title, desc, instruction
        )
    except Exception as e:
        log.exception("ai edit failed")
        await wait.edit_text(f"❌ AI failed: <code>{e}</code>")
        await state.set_state(None)
        return

    await wait.edit_text(fmt_ai(result), reply_markup=kb_ai_after())
    await state.update_data(ai_result=result)
    await state.set_state(None)


# ============================================================
# AUDIO UPLOAD → auto-clean
# ============================================================

@router.message(F.audio | F.voice | F.document)
async def handle_audio_upload(msg: Message, state: FSMContext):
    # pick the right file object
    if msg.audio:
        tg_file = msg.audio
        ext = ".mp3"
    elif msg.voice:
        tg_file = msg.voice
        ext = ".ogg"
    else:
        tg_file = msg.document
        name = msg.document.file_name or "audio.bin"
        mime = (msg.document.mime_type or "").lower()
        looks_audio = (
            mime.startswith("audio/")
            or name.lower().endswith(
                (".mp3", ".wav", ".m4a", ".flac",
                 ".ogg", ".opus", ".aac")
            )
        )
        if not looks_audio:
            await msg.answer("That file doesn't look like audio.")
            return
        ext = Path(name).suffix or ".mp3"

    Path(config.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    dest = Path(config.UPLOAD_DIR) / f"tg_{msg.from_user.id}_{msg.message_id}{ext}"

    await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    status = await msg.answer("📥 Downloading your audio...")
    try:
        await bot.download(tg_file, destination=dest)
    except Exception as e:
        log.exception("telegram download failed")
        await status.edit_text(f"❌ Download failed: <code>{e}</code>")
        return

    await status.edit_text("🧹 Removing silence + noise...")
    try:
        cleaned = await asyncio.to_thread(remove_silence, dest)
    except Exception as e:
        log.exception("clean failed")
        await status.edit_text(f"❌ Cleaning failed: <code>{e}</code>")
        return

    if not cleaned or not Path(cleaned).exists():
        await status.edit_text("❌ Cleaning produced no output.")
        return

    await safe_send_document(msg, cleaned, "✅ Cleaned audio ready!")

    await state.update_data(mode="build", cleaned_path=str(cleaned))
    await status.edit_text(
        "Now send a <b>YouTube link</b> → I'll use its thumbnail to build the video."
    )


# ============================================================
# TEXT MESSAGE — source link or build link
# ============================================================

@router.message(F.text)
async def handle_text(msg: Message, state: FSMContext):
    try:
        video_id = extract_video_id(msg.text)
    except ValueError:
        await msg.answer("Send a YouTube link, upload an audio file, or /help.")
        return

    data = await state.get_data()
    mode = data.get("mode", "source")

    if mode == "source" or not data.get("cleaned_path"):
        await do_source(msg, state, video_id)
    else:
        await do_build(msg, state, video_id)


# -------- SOURCE MODE: download + auto-clean --------------------------------

async def do_source(msg: Message, state: FSMContext, video_id: str):
    await bot.send_chat_action(msg.chat.id, ChatAction.TYPING)
    try:
        meta = await asyncio.to_thread(get_youtube_metadata, video_id)
    except Exception as e:
        log.exception("metadata failed")
        await msg.answer(f"❌ Metadata failed: <code>{e}</code>")
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
                caption=(
                    f"🖼 <b>{meta['title']}</b>\n"
                    f"📺 {meta['channel'] or ''}\n"
                    f"🆔 <code>{video_id}</code>"
                ),
            )
        except Exception as e:
            log.warning("photo send failed: %s", e)

    status = await msg.answer("📥 Downloading audio via RapidAPI...")
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

    await safe_send_document(msg, audio_path, "🎵 Raw audio (before cleaning)")

    await status.edit_text("🧹 Auto-removing silence + noise...")
    cleaned = None
    try:
        cleaned = await asyncio.to_thread(remove_silence, audio_path)
    except Exception as e:
        log.exception("auto clean failed")
        await msg.answer(f"⚠️ Auto-clean failed: <code>{e}</code>")

    if cleaned and Path(cleaned).exists():
        await safe_send_document(msg, cleaned, "✅ Auto-cleaned audio")

    await state.update_data(
        mode="build",
        source_video_id=video_id,
        orig_title=meta["title"] or "",
        orig_desc=meta["description"] or "",
        cleaned_path=str(cleaned) if cleaned else str(audio_path),
    )

    await status.edit_text(
        "✅ Done.\n\n"
        "<b>Next steps:</b>\n"
        "• Upload your own cleaned audio → I'll auto-clean it (optional)\n"
        "• Or send a <b>YouTube link</b> → I'll use its thumbnail to build the video"
    )


# -------- BUILD MODE: thumbnail + cleaned audio → MP4 -----------------------

async def do_build(msg: Message, state: FSMContext, video_id: str):
    data = await state.get_data()
    cleaned_path = data.get("cleaned_path")

    if not cleaned_path or not Path(cleaned_path).exists():
        await msg.answer(
            "❌ No cleaned audio found. Send a YouTube link first to fetch audio."
        )
        await state.update_data(mode="source")
        return

    status = await msg.answer("🖼 Fetching thumbnail...")
    try:
        meta = await asyncio.to_thread(get_youtube_metadata, video_id)
    except Exception as e:
        log.exception("metadata failed")
        await status.edit_text(f"❌ Metadata failed: <code>{e}</code>")
        return

    thumb_path = Path(config.IMAGE_DIR) / f"{video_id}.jpg"
    if not thumb_path.exists():
        try:
            thumb_path = await asyncio.to_thread(
                download_thumbnail, meta["thumbnail"], video_id
            )
        except Exception as e:
            log.exception("thumbnail failed")
            await status.edit_text(f"❌ Thumbnail failed: <code>{e}</code>")
            return

    await status.edit_text("🎬 Building 1920×1080 MP4...")
    try:
        video_path = await asyncio.to_thread(
            create_video, thumb_path, Path(cleaned_path)
        )
    except Exception as e:
        log.exception("video build failed")
        await status.edit_text(f"❌ Build failed: <code>{e}</code>")
        return

    size_mb = Path(video_path).stat().st_size / (1024 * 1024)
    await status.edit_text(f"📤 Uploading video ({size_mb:.1f} MB)...")

    try:
        await msg.answer_video(
            FSInputFile(video_path),
            caption=f"✅ <b>Video ready!</b>\n\n{meta['title']}",
        )
    except Exception as e:
        log.exception("video send failed")
        await msg.answer(f"⚠️ Could not send video: <code>{e}</code>")
        # try as a document instead — local Bot API supports big files
        try:
            await msg.answer_document(
                FSInputFile(video_path),
                caption="⚠️ Sent as document because video send failed.",
            )
        except Exception as e2:
            await msg.answer(f"⚠️ Document send also failed: <code>{e2}</code>")

    await state.update_data(
        mode="build",
        video_id=video_id,
        orig_title=meta["title"] or data.get("orig_title", ""),
        orig_desc=meta["description"] or data.get("orig_desc", ""),
        video_path=str(video_path),
    )

    await status.edit_text(
        "Tap below to generate a fresh title + description.",
        reply_markup=kb_ai_initial(),
    )


# ============================================================
# FALLBACK
# ============================================================

@router.message()
async def fallback(msg: Message):
    await msg.answer("Send a YouTube link, upload an audio file, or type /help.")


# ============================================================
# MAIN
# ============================================================

async def main():
    log.info("Starting bot...")
    for d in (config.AUDIO_DIR, config.IMAGE_DIR, config.VIDEO_DIR, config.UPLOAD_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)

    # sanity check: who am I?
    me = await bot.get_me()
    log.info("Bot identity: @%s (id=%s)", me.username, me.id)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
