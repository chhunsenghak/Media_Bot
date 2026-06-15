#!/usr/bin/env python3
"""
Telegram Bot — Multi-Platform Downloader
Downloads MP4 (video) or MP3 (audio) from TikTok, YouTube, and Facebook.

Requirements:
    pip install -r requirements.txt
    ffmpeg must be in PATH (needed for MP3 conversion)

Setup:
    1. Create a bot via @BotFather on Telegram → get your token.
    2. Create a .env file: TELEGRAM_BOT_TOKEN=<your_token>
    3. Run: python media_bot.py
"""

import os
import re
import sys
import asyncio
import tempfile
import logging
from pathlib import Path

# Fix: ProactorEventLoop (Windows default) conflicts with httpx/anyio → ReadTimeout
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest
import yt_dlp

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

SUPPORTED_URL_PATTERN = re.compile(
    r"https?://("
    r"(www\.|vm\.|vt\.)?tiktok\.com/\S+"
    r"|"
    r"(www\.)?youtube\.com/(watch\?[^\s]*v=|shorts/|live/)\S+"
    r"|"
    r"youtu\.be/\S+"
    r"|"
    r"(www\.|m\.|web\.)?facebook\.com/(watch/?\?[^\s]*v=|videos/|reel/|share/[rv]/)\S+"
    r"|"
    r"fb\.watch/\S+"
    r")",
    re.IGNORECASE,
)

PLATFORM_LABELS = {
    "tiktok.com":   "TikTok",
    "youtube.com":  "YouTube",
    "youtu.be":     "YouTube",
    "facebook.com": "Facebook",
    "fb.watch":     "Facebook",
}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def h(text) -> str:
    """Escape a string for Telegram HTML parse mode."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def extract_media_url(text: str) -> str | None:
    match = SUPPORTED_URL_PATTERN.search(text)
    return match.group(0) if match else None


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    for domain, label in PLATFORM_LABELS.items():
        if domain in url_lower:
            return label
    return "Video"


def _common_ydl_opts(output_path: str) -> dict:
    return {
        "outtmpl": output_path,
        "quiet":   True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }


def download_video(url: str, output_path: str) -> tuple[str, str]:
    """Download as MP4. Returns (prepared_filename, title)."""
    opts = _common_ydl_opts(output_path)
    opts.update({
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
    })
    if "tiktok.com" in url.lower():
        opts["extractor_args"] = {"tiktok": {"webpage_url_basename": "video"}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info), info.get("title", "")


def download_audio(url: str, output_path: str) -> tuple[str, str]:
    """Download as MP3 192 kbps. Returns (mp3_path, title)."""
    opts = _common_ydl_opts(output_path)
    opts.update({
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        base = ydl.prepare_filename(info)
        return str(Path(base).with_suffix(".mp3")), info.get("title", "")


# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 <b>Media Downloader Bot</b>\n\n"
        "Supported platforms:\n"
        "• 🎵 <b>TikTok</b> — video &amp; audio\n"
        "• 📺 <b>YouTube</b> — video &amp; audio\n"
        "• 📘 <b>Facebook</b> — video &amp; audio\n\n"
        "Just send a link and choose 🎬 MP4 or 🎵 MP3.\n\n"
        "/start — show this message\n"
        "/help  — show this message",
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect a supported URL and show the format picker."""
    text = update.message.text or ""
    url  = extract_media_url(text)

    if not url:
        await update.message.reply_text(
            "⚠️ No supported URL detected.\n"
            "Please send a TikTok, YouTube, or Facebook link."
        )
        return

    platform = detect_platform(url)
    context.user_data["media_url"] = url

    keyboard = [[
        InlineKeyboardButton("🎬 MP4 (Video)", callback_data="fmt:mp4"),
        InlineKeyboardButton("🎵 MP3 (Audio)", callback_data="fmt:mp3"),
    ]]
    await update.message.reply_text(
        f"🔗 <b>{h(platform)}</b> link detected! Choose your format:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def handle_format_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle MP4/MP3 button press, download, and send the file."""
    query = update.callback_query
    await query.answer()

    fmt = query.data.split(":")[1]
    url = context.user_data.get("media_url")

    if not url:
        await query.edit_message_text("❌ Session expired. Please send the link again.")
        return

    platform = detect_platform(url)
    label    = "🎬 video (MP4)" if fmt == "mp4" else "🎵 audio (MP3)"

    await query.edit_message_text(f"⏳ Downloading {platform} {label}… please wait.")

    with tempfile.TemporaryDirectory() as tmpdir:
        base_path = os.path.join(tmpdir, "media_download.%(ext)s")
        try:
            if fmt == "mp4":
                _, title = download_video(url, base_path)
                candidates = list(Path(tmpdir).glob("media_download.*"))
                if not candidates:
                    raise FileNotFoundError("Downloaded file not found.")
                file_path = str(candidates[0])
            else:
                file_path, title = download_audio(url, base_path)

            size = os.path.getsize(file_path)
            if size > TELEGRAM_MAX_BYTES:
                await query.edit_message_text(
                    f"❌ File too large ({size // 1024 // 1024} MB). "
                    "Telegram's bot limit is 50 MB."
                )
                return

            if fmt == "mp4":
                await query.edit_message_text("📤 Uploading video…")
                with open(file_path, "rb") as f:
                    await context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=f,
                        caption=f"🎬 <b>{h(title)}</b>\n<i>{h(platform)}</i>",
                        supports_streaming=True,
                        parse_mode="HTML",
                    )
            else:
                await query.edit_message_text("📤 Uploading audio…")
                with open(file_path, "rb") as f:
                    await context.bot.send_audio(
                        chat_id=query.message.chat_id,
                        audio=f,
                        title=title,
                        caption=f"🎵 <b>{h(title)}</b>\n<i>{h(platform)}</i>",
                        parse_mode="HTML",
                    )

            await query.edit_message_text(f"✅ Done! Enjoy your {label}.")

        except yt_dlp.utils.DownloadError as e:
            logger.error("yt-dlp error: %s", e)
            await query.edit_message_text(
                "❌ Download failed. The video may be private, deleted, or geo-restricted.\n"
                f"Details: <code>{h(str(e)[:200])}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Unexpected error")
            await query.edit_message_text(
                f"❌ Unexpected error: <code>{h(str(e)[:200])}</code>",
                parse_mode="HTML",
            )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError(
            "Bot token not set!\n"
            "  Create a .env file with: TELEGRAM_BOT_TOKEN=<your_token>"
        )

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(handle_format_choice, pattern=r"^fmt:"))

    logger.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
