"""Telegram bot — vocals-only video pipeline.

Flow:
  1. User sends a YouTube link.
     → Bot sends the thumbnail
     → Bot downloads the audio, uploads it, and replies with a DIRECT DOWNLOAD LINK
     → Bot shows an inline button [✅ Done]

  2. User taps [✅ Done].
     → Bot asks the user to upload their cleaned vocal audio.

  3. User uploads audio.
     → Bot removes silence + noise
     → Bot builds a 1920x1080 MP4 (thumbnail from step 1)
     → Bot uploads the video and replies with:
         • DIRECT VIDEO DOWNLOAD LINK
         • AI title (copyable)
         • AI description (copyable)
         • buttons [🔄 Regenerate] [✏️ Edit with prompt]

  4. User taps a button → AI regenerates the caption.

Uses the local Bot API server (2 GB uploads).
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
from thumbnail import download_thumbnail, get_youtube_metadata


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "6965134031:AAHdZxo1I4WuV4pvhEgfN24UOzPPL2R76oE",
)

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
# BOT SETUP — local Bot API server (2 GB upload limit)
# ============================================================

def build_session() -> AiohttpSession:
    """Route API calls through the local Bot API server.

    NOTE: we do NOT pass is_local=True, because the bot and the Bot API server
    live on different machines — passing local paths would break uploads.
    We upload the file bytes as multipart form data instead.
    """
    if not LOCAL_API:
        log.info("No LOCAL_TELEGRAM_API — using cloud API (50 MB limit)")
        return AiohttpSession()
    server = TelegramAPIServer.from_base(LOCAL_API)
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
    raise ValueError("No YouTube video ID found in that text.")


async def upload_and_get_url(chat_id: int, file_path, caption: str) -> str:
    """Upload a file as a Telegram document, then return a direct download URL
    served by the local Bot API server:

        {LOCAL_API}/file/bot{TOKEN}/{file_path}
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"Missing file: {p}")

    sent = await bot.send_document(chat_id, FSInputFile(p), caption=caption)
    tg_file = await bot.get_file(sent.document.file_id)
    return f"{LOCAL_API}/file/bot{BOT_TOKEN}/{tg_file.file_path}"


class S(StatesGroup):
    awaiting_audio  = State()
    editing_caption = State()


def kb_done():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Done", callback_data="start_upload")
    ]])


def kb_ai():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Regenerate",       callback_data="regen_ai"),
        InlineKeyboardButton(text="✏️ Edit with prompt", callback_data="edit_ai"),
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


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "<b>How it works</b>\n\n"
        "1. Send a YouTube link → thumbnail + a direct audio download link\n"
        "2. Tap <b>✅ Done</b>\n"
        "3. Upload your cleaned vocal audio\n"
        "4. I clean it + build the 1920×1080 MP4\n"
        "5. You get a direct video download link + AI title + description\n"
        "6. Use <b>🔄 Regenerate</b> or <b>✏️ Edit with prompt</b> as needed\n"
    )


# ============================================================
# EDIT-PROMPT HANDLER (must run before the YT-link handler)
# ============================================================

@router.message(S.editing_caption, F.text)
async def handle_edit_prompt(msg: Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("orig_title", "")
    desc  = data.get("orig_desc", "")
    instruction = msg.text.strip()

    wait = await msg.answer("🤖 Regenerating with your instruction...")
    try:
        ai = await asyncio.to_thread(
            generate_metadata, title, desc, instruction
        )
    except Exception as e:
        log.exception("ai edit failed")
        await wait.edit_text(f"❌ AI failed: <code>{e}</code>")
        await state.set_state(None)
        return

    await state.update_data(ai_result=ai)
    await state.set_state(None)

    await wait.edit_text(
        fmt_caption_block(ai["title"], ai["description"]),
        reply_markup=kb_ai(),
    )


# ============================================================
# AUDIO UPLOAD (only in awaiting_audio state)
# ============================================================

@router.message(S.awaiting_audio, F.audio | F.voice | F.document)
async def handle_audio_upload(msg: Message, state: FSMContext):
    data = await state.get_data()

    # pick the right object
    if msg.audio:
        tg_file = msg.audio
        ext = ".mp3"
    elif msg.voice:
        tg_file = msg.voice
        ext = ".ogg"
    else:
        tg_file = msg.document
        name = msg.document.file_name or "audio.bin"
        ext = Path(name).suffix or ".mp3"

    Path(config.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    dest = Path(config.UPLOAD_DIR) / f"tg_{msg.from_user.id}_{msg.message_id}{ext}"

    status = await msg.answer("📥 Downloading your audio from Telegram...")
    try:
        await bot.download(tg_file, destination=dest)
    except Exception as e:
        log.exception("telegram download failed")
        await status.edit_text(f"❌ Download failed: <code>{e}</code>")
        return

    # ---- remove silence + noise ----
    await status.edit_text("🧹 Removing silence + background noise...")
    try:
        cleaned = await asyncio.to_thread(remove_silence, dest)
    except Exception as e:
        log.exception("clean failed")
        await status.edit_text(f"❌ Cleaning failed: <code>{e}</code>")
        return

    if not cleaned or not Path(cleaned).exists():
        await status.edit_text("❌ Cleaning produced no output.")
        return

    # ---- build video ----
    thumb_path = data.get("thumb_path")
    if not thumb_path or not Path(thumb_path).exists():
        await status.edit_text(
            "❌ Thumbnail missing. Send the YouTube link again."
        )
        await state.set_state(None)
        return

    await status.edit_text("🎬 Building 1920×1080 MP4...")
    try:
        video_path = await asyncio.to_thread(
            create_video, thumb_path, Path(cleaned)
        )
    except Exception as e:
        log.exception("video build failed")
        await status.edit_text(f"❌ Video build failed: <code>{e}</code>")
        return

    # ---- upload video, get direct link ----
    size_mb = Path(video_path).stat().st_size / (1024 * 1024)
    await status.edit_text(f"📤 Uploading video ({size_mb:.1f} MB)...")
    try:
        video_url = await upload_and_get_url(
            msg.chat.id, video_path, "🎬 Final video"
        )
    except Exception as e:
        log.exception("video upload failed")
        await status.edit_text(f"❌ Video upload failed: <code>{e}</code>")
        return

    # ---- AI caption ----
    await status.edit_text("🤖 Generating AI title + description...")
    title = data.get("orig_title", "")
    desc  = data.get("orig_desc", "")
    try:
        ai = await asyncio.to_thread(generate_metadata, title, desc)
    except Exception as e:
        log.exception("ai failed")
        ai = {"title": title or "Vocals only", "description": desc or ""}

    await state.update_data(ai_result=ai, video_url=video_url)
    await state.set_state(None)

    await status.edit_text(
        build_ready_message(video_url, ai["title"], ai["description"]),
        reply_markup=kb_ai(),
        disable_web_page_preview=True,
    )


# ============================================================
# TEXT IN awaiting_audio STATE → restart or hint
# ============================================================

@router.message(S.awaiting_audio, F.text)
async def handle_text_in_awaiting(msg: Message, state: FSMContext):
    try:
        extract_video_id(msg.text)
    except ValueError:
        await msg.answer("Please upload an audio file, or /reset to start over.")
        return
    # It's a new YT link — restart
    await state.set_state(None)
    await do_youtube_link(msg, state)


# ============================================================
# YOUTUBE LINK HANDLER
# ============================================================

@router.message(F.text)
async def handle_youtube_link(msg: Message, state: FSMContext):
    await do_youtube_link(msg, state)


async def do_youtube_link(msg: Message, state: FSMContext):
    try:
        video_id = extract_video_id(msg.text)
    except ValueError:
        await msg.answer("Send a valid YouTube link, or /help.")
        return

    # ---- metadata + thumbnail ----
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
            log.warning("thumbnail download failed: %s", e)

    if thumb_path and Path(thumb_path).exists():
        try:
            await msg.answer_photo(
                FSInputFile(thumb_path),
                caption=(
                    f"🖼 <b>{meta['title']}</b>\n"
                    f"📺 {meta['channel'] or ''}"
                ),
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

    # ---- upload audio to get a direct link ----
    await status.edit_text("📤 Uploading audio to get a direct link...")
    try:
        audio_url = await upload_and_get_url(
            msg.chat.id, audio_path, "🎵 Raw audio"
        )
    except Exception as e:
        log.exception("audio upload failed")
        await status.edit_text(f"❌ Audio upload failed: <code>{e}</code>")
        return

    # ---- save state ----
    await state.update_data(
        video_id=video_id,
        thumb_path=str(thumb_path) if thumb_path else "",
        orig_title=meta["title"] or "",
        orig_desc=meta["description"] or "",
    )

    # ---- reply with link + Done button ----
    await status.edit_text(
        "✅ <b>Audio ready</b>\n\n"
        "⬇️ <b>Direct download link:</b>\n"
        f"{audio_url}\n\n"
        "Clean the audio however you like, then tap <b>✅ Done</b> below "
        "and upload your cleaned vocal file.",
        reply_markup=kb_done(),
        disable_web_page_preview=True,
    )


# ============================================================
# DONE BUTTON
# ============================================================

@router.callback_query(F.data == "start_upload")
async def cb_done(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await state.set_state(S.awaiting_audio)
    await cb.message.answer(
        "📤 <b>Now upload your cleaned vocal audio.</b>\n\n"
        "Supported: MP3 / WAV / M4A / FLAC / OGG / OPUS "
        "(or send as a file)."
    )


# ============================================================
# AI REGENERATE / EDIT CALLBACKS
# ============================================================

@router.callback_query(F.data == "regen_ai")
async def cb_regen(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Regenerating...")
    data = await state.get_data()

    title = data.get("orig_title", "")
    desc  = data.get("orig_desc", "")
    video_url = data.get("video_url", "")

    try:
        ai = await asyncio.to_thread(generate_metadata, title, desc)
    except Exception as e:
        log.exception("ai regen failed")
        await cb.message.answer(f"❌ AI failed: <code>{e}</code>")
        return

    await state.update_data(ai_result=ai)

    new_text = build_ready_message(video_url, ai["title"], ai["description"])

    try:
        await cb.message.edit_text(
            new_text,
            reply_markup=kb_ai(),
            disable_web_page_preview=True,
        )
    except Exception:
        # message might be too old to edit; send a fresh one
        await cb.message.answer(
            fmt_caption_block(ai["title"], ai["description"]),
            reply_markup=kb_ai(),
        )


@router.callback_query(F.data == "edit_ai")
async def cb_edit(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
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
async def fallback(msg: Message, state: FSMContext):
    await msg.answer("Send a YouTube link, or type /help.")


# ============================================================
# MAIN
# ============================================================

async def main():
    log.info("Starting bot...")
    for d in (config.AUDIO_DIR, config.IMAGE_DIR, config.VIDEO_DIR, config.UPLOAD_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)

    me = await bot.get_me()
    log.info("Bot identity: @%s (id=%s)", me.username, me.id)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
