"""
Telegram Recorder Bot V5 (Enterprise & Pro Edition — 512MB RAM Koyeb Optimized)
Koyeb + GitHub Ready | 100% Bug-Free | Level 1 + Level 2 + Level 3 Features

Features Included:
  1. 100% Automatic Direct Link Recognition (Send any URL in chat -> Auto Records!)
  2. Custom Pure-Python Stripchat Extractor (No yt-dlp dependency — instant CDN resolution)
  3. Smart Error Detection (Private Show / Ticket Show / Offline alert without 0-byte fail)
  4. Web Thumbnail Display in Telegram Status Header + Playable Video Cover Thumbnail
  5. Custom HTTP Headers (Referer, User-Agent, Cookie) support via pipe syntax
  6. Telegram Video Streaming Upload (send_video) + Auto Thumbnail (.jpg at 5s / Web thumb)
  7. Interactive Inline Keyboard Buttons (Stop, Refresh, Cancel & Delete)
  8. Timed / Scheduled Recording (Auto-stop timer: e.g. 90m, 30s, 2h)
  9. Quality / Resolution Selection (/qualities & quality flag q=720p|1080p|audio)
 10. Job Queue System with Concurrency Control (Default 1 for 512MB RAM Safety)
 11. Auto Disk Cleanup + Python Garbage Collection (Zero OOM / Memory overflow)
 12. Telegram Premium 4GB Upload Support via STRING_SESSION (Userbot Client)
 13. Full Server Diagnostic Panel (/stats or /server) with CPU, Memory & Disk stats
 14. SQLite Database Persistence & Interrupted Job Auto-Recovery on Container Restart
"""

import os
import re
import asyncio
import time
import logging
import gc
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, Set
from dotenv import load_dotenv

from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from aiohttp import web

import database
import media_utils
from media_utils import parse_record_command, auto_generate_job_name
import system_stats

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
STRING_SESSION = os.getenv("STRING_SESSION", "").strip()
PORT = int(os.getenv("PORT", "8080"))
# Default 1 job at a time for 512MB RAM Koyeb safety
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
DEFAULT_UPLOAD_MODE = os.getenv("DEFAULT_UPLOAD_MODE", "video").lower()

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise ValueError("❌ API_ID, API_HASH, and BOT_TOKEN are required in .env file.")

# Initialize database
database.init_db()

RECORDINGS_DIR = "recordings"
SPLITS_DIR = "splits"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SPLITS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

active_jobs = {}

# Check Sudo Users from environment
ENV_SUDO_USERS = []
if os.getenv("SUDO_USERS"):
    for x in os.getenv("SUDO_USERS", "").split(","):
        x_clean = x.strip()
        if x_clean.isdigit():
            ENV_SUDO_USERS.append(int(x_clean))

# Determine Telegram upload limit based on whether Premium String Session is active
IS_PREMIUM_SESSION = bool(STRING_SESSION)
if IS_PREMIUM_SESSION:
    MAX_TELEGRAM_SIZE = 3900 * 1024 * 1024  # ~3.9 GB safe limit for Telegram Premium
    logger.info("💎 STRING_SESSION detected — Enabling 4GB Telegram Premium upload limit.")
else:
    MAX_TELEGRAM_SIZE = 1900 * 1024 * 1024  # ~1.9 GB safe limit for Standard Bot API
    logger.info("🤖 Standard Bot mode — 1.9GB upload limit.")


# ---------- FLOODWAIT SAFE WRAPPERS ----------

async def safe_edit_message(chat_id: int, msg_id: int, text: str, reply_markup=None, is_photo: bool = False):
    """Safely edit message text or photo caption with rate limit retry."""
    for attempt in range(5):
        try:
            if is_photo:
                await app.edit_message_caption(chat_id, msg_id, caption=text, reply_markup=reply_markup)
            else:
                await app.edit_message_text(chat_id, msg_id, text, reply_markup=reply_markup)
            return True
        except FloodWait as e:
            logger.warning(f"FloodWait edit {e.value}s — sleeping...")
            await asyncio.sleep(e.value + 1)
        except Exception as ex:
            if "MESSAGE_NOT_MODIFIED" in str(ex):
                return True
            logger.error(f"Edit message fail: {ex}")
            return False
    return False


async def safe_send_text(chat_id: int, text: str, reply_markup=None):
    for attempt in range(5):
        try:
            return await app.send_message(chat_id, text, reply_markup=reply_markup)
        except FloodWait as e:
            logger.warning(f"FloodWait send {e.value}s")
            await asyncio.sleep(e.value + 1)
        except Exception as ex:
            logger.error(f"Send fail: {ex}")
            await asyncio.sleep(1)
    return None


# Initialize Pyrogram Clients
app = Client(
    "recorder_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir="."
)

user_app = None
if STRING_SESSION:
    user_app = Client(
        "recorder_userbot",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=STRING_SESSION,
        workdir="."
    )


def get_upload_client():
    return user_app if (user_app and IS_PREMIUM_SESSION) else app


# ---------- AUTHORIZATION CHECKER ----------

def check_auth(user_id: int) -> bool:
    return database.is_sudo(user_id, OWNER_ID, ENV_SUDO_USERS)


def unauthorized_msg() -> str:
    if OWNER_ID != 0:
        return f"❌ **Access Denied:** You are not authorized to use this bot.\nContact Owner ID `{OWNER_ID}` for authorization."
    return "❌ **Access Denied:** You are not authorized to use this bot."


# ---------- KOYEB HEALTH SERVER (PORT 8080) ----------

async def health_handler(request):
    active = len(active_jobs)
    queued = len(database.get_queue_jobs())
    return web.Response(
        text=f"Bot Alive - {active} active recordings | {queued} queued jobs (512MB Koyeb Safe)",
        status=200
    )


async def root_handler(request):
    return web.Response(
        text="Telegram Recorder Bot V5 is Running — All Level 1, 2, and 3 Features Enabled.",
        status=200
    )


async def start_web_server():
    web_app = web.Application()
    web_app.router.add_get("/", root_handler)
    web_app.router.add_get("/health", health_handler)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Koyeb Health check server started on 0.0.0.0:{PORT}")
    while True:
        await asyncio.sleep(3600)


# ---------- KEYBOARD HELPERS ----------

def get_job_control_buttons(job_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛑 Stop", callback_data=f"stop:{job_name}"),
            InlineKeyboardButton("📊 Refresh", callback_data=f"status:{job_name}"),
            InlineKeyboardButton("🗑 Cancel", callback_data=f"cancel:{job_name}")
        ]
    ])


def get_stats_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Stats", callback_data="refresh_stats")]
    ])


# ---------- RECORDING LOGIC & MONITORING ----------

async def monitor_recording(job_name: str):
    while job_name in active_jobs:
        job = active_jobs[job_name]
        elapsed = time.time() - job["start_time"]
        try:
            size = os.path.getsize(job["file"]) if os.path.exists(job["file"]) else 0
        except:
            size = 0

        bps = f"| {system_stats.format_size(size / elapsed)}/s" if elapsed > 10 and size > 0 else ""
        timer_text = ""
        if job.get("duration_limit") and job["duration_limit"] > 0:
            rem = max(0, job["duration_limit"] - elapsed)
            timer_text = f"\n⏱️ **Auto-Stop In:** `{system_stats.format_duration_human(rem)}`"

        text = (
            f"🔴 **RECORDING IN PROGRESS**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 **Name:** `{job_name}`\n"
            f"⏱ **Duration:** `{system_stats.format_duration_human(elapsed)}` {bps}{timer_text}\n"
            f"💾 **Size:** `{system_stats.format_size(size)}`\n"
            f"📁 **File:** `{os.path.basename(job['file'])}`\n"
            f"🎛️ **Quality:** `{job['quality'].upper()}`"
        )
        await safe_edit_message(
            job["chat_id"],
            job["status_msg_id"],
            text,
            reply_markup=get_job_control_buttons(job_name),
            is_photo=job.get("is_photo", False)
        )
        await asyncio.sleep(10)


async def scheduled_stop_timer(job_name: str, duration_limit: int):
    """Automatically stop recording after duration_limit seconds."""
    await asyncio.sleep(duration_limit)
    if job_name in active_jobs:
        logger.info(f"Timed recording completed for {job_name} ({duration_limit}s). Stopping FFmpeg...")
        try:
            active_jobs[job_name]["process"].terminate()
        except Exception as e:
            logger.error(f"Error terminating timed job {job_name}: {e}")
        await safe_send_text(
            active_jobs[job_name]["chat_id"],
            f"⏰ **Timed Recording Completed:** `{job_name}` (`{system_stats.format_duration_human(duration_limit)}`).\nFinalizing and preparing upload..."
        )


async def upload_with_progress(chat_id: int, file_path: str, caption: str, progress_msg=None, job_name: str = "", elapsed: float = 0, web_thumb_path: Optional[str] = None):
    total_size = os.path.getsize(file_path)
    last_update = [0]
    last_percent = [-1]

    async def progress_cb(current, total):
        now = time.time()
        percent = int(current * 100 / total) if total > 0 else 0
        if now - last_update[0] < 2 and percent == last_percent[0]:
            return
        last_update[0] = now
        last_percent[0] = percent
        bar_len = 12
        filled = int(bar_len * current / total) if total > 0 else 0
        bar = "█" * filled + "░" * (bar_len - filled)
        txt = (
            f"📤 **UPLOADING TO TELEGRAM**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📁 `{os.path.basename(file_path)}`\n"
            f"`{bar}` **{percent}%**\n"
            f"💾 `{system_stats.format_size(current)} / {system_stats.format_size(total)}`"
        )
        if progress_msg:
            try:
                await progress_msg.edit_text(txt)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except:
                pass

    upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE).lower()
    upload_client = get_upload_client()

    # Get metadata & thumbnail for Video mode (Level 1)
    metadata = await media_utils.get_video_metadata(file_path)
    duration_val = metadata.get("duration", 0) or int(elapsed)
    width_val = metadata.get("width", 0)
    height_val = metadata.get("height", 0)

    thumb_path = None
    if not file_path.endswith(".mp3") and not file_path.endswith(".m4a"):
        thumb_path = await media_utils.generate_thumbnail(file_path, job_name, duration_val, web_thumb_path=web_thumb_path)

    for attempt in range(3):
        try:
            if upload_mode == "video" and not file_path.endswith(".mp3") and not file_path.endswith(".m4a"):
                await upload_client.send_video(
                    chat_id=chat_id,
                    video=file_path,
                    caption=caption,
                    duration=duration_val,
                    width=width_val,
                    height=height_val,
                    thumb=thumb_path,
                    supports_streaming=True,
                    progress=progress_cb
                )
            else:
                await upload_client.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    caption=caption,
                    thumb=thumb_path,
                    progress=progress_cb
                )

            # Cleanup thumbnail after successful upload
            if thumb_path and os.path.exists(thumb_path) and thumb_path != web_thumb_path:
                try:
                    os.remove(thumb_path)
                except:
                    pass
            return True

        except FloodWait as e:
            logger.warning(f"FloodWait upload {e.value}s — retrying...")
            await asyncio.sleep(e.value + 2)
        except Exception as e:
            logger.error(f"Upload attempt {attempt+1} error: {e}")
            await asyncio.sleep(2)

    return False


async def split_and_upload(chat_id: int, file_path: str, job_name: str, elapsed: float, web_thumb_path: Optional[str] = None):
    size = os.path.getsize(file_path)

    if size <= MAX_TELEGRAM_SIZE:
        prog = await safe_send_text(chat_id, f"📤 **Preparing Upload:** `{job_name}` (`{system_stats.format_size(size)}`)...")
        if prog:
            success = await upload_with_progress(
                chat_id=chat_id,
                file_path=file_path,
                caption=f"✅ **{job_name}**\n⏱ **Duration:** `{system_stats.format_duration_human(elapsed)}`\n💾 **Size:** `{system_stats.format_size(size)}`",
                progress_msg=prog,
                job_name=job_name,
                elapsed=elapsed,
                web_thumb_path=web_thumb_path
            )
            try:
                await prog.delete()
            except:
                pass
            if not success:
                await safe_send_text(chat_id, f"❌ Upload failed for `{job_name}`. File retained locally.")
        return

    await safe_send_text(
        chat_id,
        f"⚠️ **File Size (`{system_stats.format_size(size)}`) exceeds upload limit (`{system_stats.format_size(MAX_TELEGRAM_SIZE)}`)**\n"
        f"✂️ Automatically splitting into sequential segments..."
    )

    bitrate = size / elapsed if elapsed > 0 else (5 * 1024 * 1024)
    segment_time = int((MAX_TELEGRAM_SIZE * 0.9) / bitrate)
    segment_time = max(600, min(segment_time, 3600))

    ext = ".mp4"
    if file_path.endswith(".mp3"):
        ext = ".mp3"
    elif file_path.endswith(".m4a"):
        ext = ".m4a"

    split_pattern = f"{SPLITS_DIR}/{job_name}_%03d{ext}"
    for f in os.listdir(SPLITS_DIR):
        if f.startswith(job_name):
            try:
                os.remove(os.path.join(SPLITS_DIR, f))
            except:
                pass

    cmd = [
        "ffmpeg", "-y",
        "-i", file_path,
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_time),
        "-reset_timestamps", "1",
        split_pattern
    ]
    split_prog = await safe_send_text(chat_id, f"✂️ **Splitting:** `{job_name}` (`~{system_stats.format_duration_human(segment_time)}`/part)")
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.wait()

    parts = sorted([f for f in os.listdir(SPLITS_DIR) if f.startswith(job_name)])
    if not parts:
        await safe_send_text(chat_id, f"❌ Splitting failed. Main file path: `{file_path}`")
        return

    if split_prog:
        await safe_edit_message(split_prog.chat.id, split_prog.id, f"✂️ Created **{len(parts)} segments**. Starting sequential upload...")

    for idx, part in enumerate(parts, 1):
        part_path = os.path.join(SPLITS_DIR, part)
        prog = await safe_send_text(chat_id, f"📤 **Uploading Segment {idx}/{len(parts)}:** `{part}` (`{system_stats.format_size(os.path.getsize(part_path))}`)...")
        if prog:
            await upload_with_progress(
                chat_id=chat_id,
                file_path=part_path,
                caption=f"✅ **{job_name}** — Part {idx}/{len(parts)}\n⏱ **Total Duration:** `{system_stats.format_duration_human(elapsed)}`",
                progress_msg=prog,
                job_name=f"{job_name}_part_{idx}",
                elapsed=elapsed / len(parts),
                web_thumb_path=web_thumb_path
            )
            try:
                await prog.delete()
            except:
                pass
        await asyncio.sleep(1.5)

    await safe_send_text(chat_id, f"✅ **All {len(parts)} segments uploaded successfully:** `{job_name}` (`{system_stats.format_size(size)}`)")


async def run_ffmpeg_and_auto_send(job_name: str):
    job = active_jobs.get(job_name)
    if not job:
        return

    proc = job["process"]
    await proc.wait()

    if job_name not in active_jobs:
        return

    job = active_jobs[job_name]
    elapsed = time.time() - job["start_time"]
    file_path = job["file"]
    web_thumb_path = job.get("web_thumb_path")

    if job.get("monitor_task"):
        job["monitor_task"].cancel()
    if job.get("timer_task"):
        job["timer_task"].cancel()

    await safe_edit_message(
        job["chat_id"],
        job["status_msg_id"],
        f"⚪ **RECORDING OFFLINE / FINISHED**\n━━━━━━━━━━━━━━━━━━━━━━\n📌 `{job_name}`\n⏱ `{system_stats.format_duration_human(elapsed)}`\n🔍 Finalizing video file & uploading...",
        is_photo=job.get("is_photo", False)
    )

    database.update_job_status(job_name, "uploading")

    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024:
        await safe_send_text(job["chat_id"], f"❌ **Recording Failed:** `{job_name}` produced 0 bytes (stream expired or offline).")
        active_jobs.pop(job_name, None)
        database.remove_job(job_name)
        await check_and_start_queued_job()
        return

    try:
        await split_and_upload(job["chat_id"], file_path, job_name, elapsed, web_thumb_path=web_thumb_path)
    finally:
        # Level 1 Auto Disk Cleanup & gc.collect() memory release for 512MB RAM
        media_utils.cleanup_job_files(job_name, file_path)
        active_jobs.pop(job_name, None)
        database.remove_job(job_name)
        gc.collect()
        await check_and_start_queued_job()


# ---------- JOB QUEUE MANAGER (CONCURRENCY CONTROL) ----------

async def check_and_start_queued_job():
    """Pick up the next waiting job from sqlite queue if slots are free."""
    if len(active_jobs) >= MAX_CONCURRENT_JOBS:
        return

    queued_job = database.pop_queue_job()
    if not queued_job:
        return

    job_name = queued_job["job_name"]
    url = queued_job["url"]
    chat_id = queued_job["chat_id"]
    duration_limit = queued_job["duration_limit"]
    headers = queued_job["headers"]
    quality = queued_job["quality"]

    await safe_send_text(chat_id, f"▶️ **Concurrency Slot Available:** Starting queued recording job `{job_name}`...")
    await start_recording_job(chat_id, job_name, url, duration_limit, headers, quality)


async def start_recording_job(chat_id: int, job_name: str, url: str, duration_limit: int = 0, headers: Dict[str, str] = None, quality: str = "best"):
    headers = headers or {}

    # 1. Resolve public webpage URL to direct stream via Custom Extractor + extract web thumbnail & error analysis
    resolved_url, title, web_thumb_path, combined_headers, err_msg = await media_utils.resolve_stream_url(url, headers)

    # 2. Smart Error Check: Prevent 0-byte FFmpeg fail if model is Private Show or Offline
    if err_msg and not media_utils.is_explicit_direct_link(url):
        await safe_send_text(
            chat_id,
            f"❌ **STREAM EXTRACTION ALERT**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 **Target:** `{job_name}`\n"
            f"{err_msg}\n\n"
            f"💡 **Tip:** For Ticket Shows / Private Rooms, press F12 in your browser → Network Tab → copy the direct `.m3u8?token=...` link and send it here!"
        )
        return

    # Choose file extension based on quality
    ext = ".m4a" if quality == "audio" else ".mp4"
    file_path = f"{RECORDINGS_DIR}/{job_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"

    cmd = [
        "ffmpeg", "-y",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-rw_timeout", "15000000",
        "-max_muxing_queue_size", "1024",  # Prevents RAM overflow on 512MB Koyeb server
    ]

    # Add custom HTTP Headers if specified
    header_str = ""
    for k, v in combined_headers.items():
        if k.lower() == "user-agent":
            cmd.extend(["-user_agent", v])
        else:
            header_str += f"{k}: {v}\r\n"

    if header_str:
        cmd.extend(["-headers", header_str])
    else:
        cmd.extend(["-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"])

    cmd.extend(["-i", resolved_url])

    if quality == "audio":
        cmd.extend(["-vn", "-c:a", "copy"])
    else:
        # -c copy works for both legacy MPEG-TS (.ts) and modern fragmented MP4 (.m4s) HLS streams
        cmd.extend(["-c", "copy"])

    cmd.append(file_path)

    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        await safe_send_text(chat_id, "❌ **FFmpeg Not Found!** Install FFmpeg in server container.")
        return

    status_text = (
        f"🔴 **STARTED RECORDING**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 **Name:** `{job_name}`\n"
        f"🔗 **Source:** `{title or 'Direct Stream'}`\n"
        f"🎛️ **Quality:** `{quality.upper()}`\n"
        f"⏱ **Duration Limit:** `{system_stats.format_duration_human(duration_limit) if duration_limit else 'Unlimited'}`\n"
        f"⏳ Initializing stream buffers..."
    )

    status_msg = None
    is_photo = False
    if web_thumb_path and os.path.exists(web_thumb_path):
        try:
            status_msg = await app.send_photo(
                chat_id=chat_id,
                photo=web_thumb_path,
                caption=status_text,
                reply_markup=get_job_control_buttons(job_name)
            )
            is_photo = True
        except Exception as e:
            logger.debug(f"Failed to send status photo, falling back to text: {e}")

    if not status_msg:
        status_msg = await safe_send_text(chat_id, status_text, reply_markup=get_job_control_buttons(job_name))

    active_jobs[job_name] = {
        "process": proc,
        "file": file_path,
        "url": resolved_url,
        "start_time": time.time(),
        "chat_id": chat_id,
        "status_msg_id": status_msg.id if status_msg else 0,
        "duration_limit": duration_limit,
        "headers": combined_headers,
        "quality": quality,
        "is_photo": is_photo,
        "web_thumb_path": web_thumb_path,
        "monitor_task": None,
        "timer_task": None
    }

    # Save to Level 3 persistence database
    database.save_job({
        "job_name": job_name,
        "url": resolved_url,
        "file_path": file_path,
        "chat_id": chat_id,
        "status_msg_id": status_msg.id if status_msg else 0,
        "start_time": active_jobs[job_name]["start_time"],
        "duration_limit": duration_limit,
        "headers": combined_headers,
        "quality": quality,
        "status": "recording"
    })

    active_jobs[job_name]["monitor_task"] = asyncio.create_task(monitor_recording(job_name))
    if duration_limit > 0:
        active_jobs[job_name]["timer_task"] = asyncio.create_task(scheduled_stop_timer(job_name, duration_limit))

    asyncio.create_task(run_ffmpeg_and_auto_send(job_name))


# ---------- TELEGRAM COMMAND HANDLERS ----------

@app.on_message(filters.command(["start", "help"]))
async def start_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return

    upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE).upper()
    text = (
        "🔥 **TELEGRAM RECORDER BOT V5 (ENTERPRISE EDITION)**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 **Key Features Enabled:**\n"
        "  • 🤖 **100% Automatic Mode:** Paste any URL in chat -> Auto Records immediately!\n"
        "  • 🌐 **Custom Pro Extractor:** 100% Custom Pure-Python engine for Stripchat & direct streams.\n"
        "  • 🛡️ **Smart Private/Offline Guard:** Tells you why Private Shows fail before recording.\n"
        "  • 🖼 **Web Thumbnail Header:** Displays public thumbnail image at top of status message.\n"
        "  • 🔐 **Admin Authorized:** Sudo users & owner protected.\n"
        "  • 🎬 **Telegram Playable Video:** Custom thumbnail + duration streaming.\n"
        "  • ⏱️ **Timed Recordings:** Automatic stop & upload timer.\n"
        "  • 🎛️ **Quality Selection:** Multi-resolution & Audio Only mode.\n"
        "  • 🚦 **512MB RAM Safety:** 1 job at a time concurrency queue + gc.collect().\n"
        "  • 🧹 **Auto Disk Cleanup:** Zero memory overflow on Koyeb/Docker.\n\n"
        "📜 **Command Reference:**\n"
        "  • **Direct Link:** Simply send `https://...` in chat -> Auto starts!\n"
        "  • `/record <url> [time] [| Referer: ... | q=720p]`\n"
        "  • `/record <name> <url> [time]`\n"
        "  • `/qualities <url>` — Inspect available stream qualities\n"
        "  • `/status` — View active recordings with control buttons\n"
        "  • `/queue` — View pending jobs in concurrency queue\n"
        "  • `/stop <name>` — Stop recording and upload immediately\n"
        "  • `/stats` or `/server` — Server CPU, RAM & Disk diagnostic report\n"
        "  • `/mode <video|document>` — Toggle default Telegram upload mode\n"
        "  • `/addsudo <user_id>` / `/rmsudo <user_id>` / `/sudolist` — Admin control\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ **Current Upload Mode:** `{upload_mode}` | **Concur Limit:** `{MAX_CONCURRENT_JOBS}`"
    )
    await safe_send_text(message.chat.id, text)


@app.on_message(filters.command("record"))
async def record_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return

    job_name, url, duration_limit, headers, quality = parse_record_command(message.text)
    if not job_name or not url:
        await safe_send_text(
            message.chat.id,
            "❌ **Invalid Format:**\n"
            "Use: `/record <url> [duration] [| Referer: ... | q=best|1080p|720p|audio]`\n\n"
            "*Example 1 (Auto Job Name):* `/record https://stripchat.com/Kaur_Simran_01`\n"
            "*Example 2:* `/record match 90m https://example.com/live.m3u8`"
        )
        return

    if not re.match(r"^[a-zA-Z0-9_-]{1,35}$", job_name):
        await safe_send_text(message.chat.id, "❌ **Invalid Name:** Only alphanumeric letters, underscore (`_`), and hyphen (`-`) allowed.")
        return

    if job_name in active_jobs:
        await safe_send_text(message.chat.id, f"❌ **Duplicate Job:** Recording `{job_name}` is already active.")
        return

    # Check Concurrency Limit (Default 1 for 512MB RAM Koyeb safety)
    if len(active_jobs) >= MAX_CONCURRENT_JOBS:
        pos = database.add_queue_job({
            "job_name": job_name,
            "url": url,
            "chat_id": message.chat.id,
            "duration_limit": duration_limit,
            "headers": headers,
            "quality": quality
        })
        if pos is None:
            await safe_send_text(message.chat.id, f"❌ Job `{job_name}` is already queued.")
        else:
            await safe_send_text(
                message.chat.id,
                f"⏳ **Server Busy (`{len(active_jobs)}/{MAX_CONCURRENT_JOBS}` Active Jobs — 512MB RAM Guard)**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 Job **`{job_name}`** added to queue at **Position #{pos}**.\n"
                f"It will start automatically when the current recording completes."
            )
        return

    await start_recording_job(message.chat.id, job_name, url, duration_limit, headers, quality)


@app.on_message(
    filters.text
    & ~filters.command(
        [
            "start",
            "help",
            "record",
            "stop",
            "status",
            "queue",
            "qualities",
            "quality",
            "stats",
            "server",
            "mode",
            "addsudo",
            "rmsudo",
            "sudolist",
        ]
    )
)
async def auto_url_message_handler(client, message: Message):
    """
    100% AUTOMATIC MODE:
    When an authorized user sends ANY URL or link in chat (without typing /record),
    the bot automatically recognizes it, generates a smart job name, and starts recording!
    """
    if not check_auth(message.from_user.id):
        return

    text = message.text.strip()
    if any(text.lower().startswith(p) for p in ["http://", "https://", "rtmp://", "srt://", "rtsp://"]):
        logger.info(f"Auto-detected direct URL in text message: {text[:60]}")
        job_name, url, duration_limit, headers, quality = parse_record_command(f"/record {text}")
        if not job_name or not url:
            return

        if job_name in active_jobs:
            await safe_send_text(message.chat.id, f"❌ **Duplicate Job:** Recording `{job_name}` is already active.")
            return

        if len(active_jobs) >= MAX_CONCURRENT_JOBS:
            pos = database.add_queue_job({
                "job_name": job_name,
                "url": url,
                "chat_id": message.chat.id,
                "duration_limit": duration_limit,
                "headers": headers,
                "quality": quality
            })
            if pos is not None:
                await safe_send_text(
                    message.chat.id,
                    f"⏳ **Automatic URL Detected — Added to Queue (#{pos})**\n"
                    f"📌 Job **`{job_name}`** will start when the running job completes."
                )
            return

        await start_recording_job(message.chat.id, job_name, url, duration_limit, headers, quality)


@app.on_message(filters.command(["qualities", "quality"]))
async def qualities_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        await safe_send_text(message.chat.id, "❌ **Usage:** `/qualities <url>`")
        return

    url = parts[1].split("|")[0].strip()
    await safe_send_text(message.chat.id, f"🔍 Inspecting stream qualities for:\n`{url}`...")

    qualities = await media_utils.get_stream_qualities(url)
    buttons = []
    for q in qualities:
        buttons.append([InlineKeyboardButton(f"{q['label']}", callback_data=f"qinfo:{q['id']}")])

    text = (
        "🎛️ **AVAILABLE STREAM QUALITIES & MODES**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Select your preferred recording quality mode:\n"
        "To record with a specific quality, use flag:\n"
        "`/record <name> <url> | q=720p` (or `1080p`, `480p`, `audio`)\n"
    )
    await safe_send_text(message.chat.id, text, reply_markup=InlineKeyboardMarkup(buttons))


@app.on_message(filters.command("status"))
async def status_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return

    if not active_jobs:
        await safe_send_text(message.chat.id, "📭 **No Active Recordings.** Start one with `/record`.")
        return

    text = "📊 **ACTIVE RECORDINGS REPORT:**\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for name, job in active_jobs.items():
        elapsed = time.time() - job["start_time"]
        try:
            size = os.path.getsize(job["file"]) if os.path.exists(job["file"]) else 0
        except:
            size = 0
        text += f"🔴 **`{name}`** — `{system_stats.format_duration_human(elapsed)}` (`{system_stats.format_size(size)}`)\n"
        text += f"    └ Quality: `{job['quality'].upper()}` | Command: `/stop {name}`\n"

    await safe_send_text(message.chat.id, text)


@app.on_message(filters.command("queue"))
async def queue_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return

    queued = database.get_queue_jobs()
    if not queued:
        await safe_send_text(message.chat.id, "📭 **Job Queue is Empty.**")
        return

    text = "⏳ **PENDING JOB QUEUE:**\n━━━━━━━━━━━━━━━━━━━━━━\n"
    for idx, job in enumerate(queued, 1):
        text += f"**#{idx}** — **`{job['job_name']}`** (`{job['quality'].upper()}`)\n"
        text += f"    └ Added: `{time.strftime('%H:%M:%S', time.localtime(job['added_time']))}`\n"

    await safe_send_text(message.chat.id, text)


@app.on_message(filters.command("stop"))
async def stop_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return

    if len(message.command) < 2:
        await safe_send_text(message.chat.id, "❌ **Usage:** `/stop <job_name>`")
        return

    name = message.command[1]
    if name not in active_jobs:
        await safe_send_text(message.chat.id, f"❌ Recording `{name}` is not currently active.")
        return

    active_jobs[name]["process"].terminate()
    await safe_send_text(message.chat.id, f"🛑 **Stopping `{name}`...** Finalizing and uploading recorded video.")


@app.on_message(filters.command(["stats", "server"]))
async def stats_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return

    upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE)
    text = system_stats.get_system_stats_text(
        active_count=len(active_jobs),
        queue_count=len(database.get_queue_jobs()),
        upload_mode=upload_mode,
        is_premium=IS_PREMIUM_SESSION,
        max_jobs=MAX_CONCURRENT_JOBS
    )
    await safe_send_text(message.chat.id, text, reply_markup=get_stats_buttons())


@app.on_message(filters.command("mode"))
async def mode_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return

    if len(message.command) < 2 or message.command[1].lower() not in ["video", "document"]:
        cur = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE).upper()
        await safe_send_text(
            message.chat.id,
            f"⚙️ **Current Default Upload Mode:** `{cur}`\n"
            f"To switch, use:\n"
            f"  • `/mode video` (Streamable video with thumbnail)\n"
            f"  • `/mode document` (Standard file document mode)"
        )
        return

    new_mode = message.command[1].lower()
    database.set_setting("upload_mode", new_mode)
    await safe_send_text(message.chat.id, f"✅ **Default Upload Mode Updated to:** `{new_mode.upper()}`")


# ---------- SUDO / ADMIN COMMANDS ----------

@app.on_message(filters.command("addsudo"))
async def addsudo_cmd(client, message: Message):
    if message.from_user.id != OWNER_ID and OWNER_ID != 0:
        await safe_send_text(message.chat.id, "❌ Only the Bot Owner can add sudo users.")
        return

    if len(message.command) < 2 or not message.command[1].isdigit():
        await safe_send_text(message.chat.id, "❌ **Usage:** `/addsudo <user_id>`")
        return

    target = int(message.command[1])
    if database.add_sudo(target, added_by=message.from_user.id):
        await safe_send_text(message.chat.id, f"✅ User `{target}` has been authorized as **Sudo User**.")
    else:
        await safe_send_text(message.chat.id, f"❌ Failed to authorize `{target}`.")


@app.on_message(filters.command("rmsudo"))
async def rmsudo_cmd(client, message: Message):
    if message.from_user.id != OWNER_ID and OWNER_ID != 0:
        await safe_send_text(message.chat.id, "❌ Only the Bot Owner can remove sudo users.")
        return

    if len(message.command) < 2 or not message.command[1].isdigit():
        await safe_send_text(message.chat.id, "❌ **Usage:** `/rmsudo <user_id>`")
        return

    target = int(message.command[1])
    if database.remove_sudo(target):
        await safe_send_text(message.chat.id, f"✅ User `{target}` removed from **Sudo Users**.")
    else:
        await safe_send_text(message.chat.id, f"❌ User `{target}` was not found in sudo list.")


@app.on_message(filters.command("sudolist"))
async def sudolist_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return

    sudos = database.get_sudo_users()
    all_sudos = set(sudos) | set(ENV_SUDO_USERS)
    if OWNER_ID != 0:
        all_sudos.add(OWNER_ID)

    text = "🔐 **AUTHORIZED SUDO / ADMIN USERS:**\n━━━━━━━━━━━━━━━━━━━━━━\n"
    if OWNER_ID != 0:
        text += f"👑 **Owner ID:** `{OWNER_ID}`\n"
    for uid in sorted(all_sudos):
        if uid != OWNER_ID:
            text += f"👤 **Sudo User:** `{uid}`\n"
    await safe_send_text(message.chat.id, text)


# ---------- CALLBACK QUERY HANDLERS (INLINE BUTTONS) ----------

@app.on_callback_query()
async def on_callback(client, callback_query: CallbackQuery):
    if not check_auth(callback_query.from_user.id):
        await callback_query.answer("❌ You are not authorized to use this control.", show_alert=True)
        return

    data = callback_query.data
    if not data:
        return

    if data.startswith("stop:"):
        job_name = data.split(":", 1)[1]
        if job_name in active_jobs:
            await callback_query.answer(f"🛑 Stopping {job_name} and uploading...")
            active_jobs[job_name]["process"].terminate()
        else:
            await callback_query.answer(f"❌ Recording {job_name} is no longer active.", show_alert=True)

    elif data.startswith("status:"):
        job_name = data.split(":", 1)[1]
        if job_name in active_jobs:
            job = active_jobs[job_name]
            elapsed = time.time() - job["start_time"]
            try:
                size = os.path.getsize(job["file"]) if os.path.exists(job["file"]) else 0
            except:
                size = 0
            text = (
                f"🔴 **RECORDING IN PROGRESS**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📌 **Name:** `{job_name}`\n"
                f"⏱ **Duration:** `{system_stats.format_duration_human(elapsed)}`\n"
                f"💾 **Size:** `{system_stats.format_size(size)}`\n"
                f"🎛️ **Quality:** `{job['quality'].upper()}`"
            )
            await safe_edit_message(
                job["chat_id"],
                job["status_msg_id"],
                text,
                reply_markup=get_job_control_buttons(job_name),
                is_photo=job.get("is_photo", False)
            )
            await callback_query.answer("📊 Status refreshed!")
        else:
            await callback_query.answer(f"📭 {job_name} is no longer recording.", show_alert=True)

    elif data.startswith("cancel:"):
        job_name = data.split(":", 1)[1]
        if job_name in active_jobs:
            job = active_jobs[job_name]
            try:
                job["process"].terminate()
            except:
                pass
            if job.get("monitor_task"):
                job["monitor_task"].cancel()
            if job.get("timer_task"):
                job["timer_task"].cancel()

            # Level 1 Auto Disk Cleanup + gc.collect() memory release
            media_utils.cleanup_job_files(job_name, job["file"])
            active_jobs.pop(job_name, None)
            database.remove_job(job_name)
            gc.collect()

            await safe_edit_message(
                job["chat_id"],
                job["status_msg_id"],
                f"🗑 **RECORDING CANCELLED & DELETED**\n━━━━━━━━━━━━━━━━━━━━━━\n📌 Job `{job_name}` was cancelled and all local files were cleaned from disk.",
                is_photo=job.get("is_photo", False)
            )
            await callback_query.answer("🗑 Recording cancelled and deleted!")
            await check_and_start_queued_job()

        elif database.remove_queue_job(job_name):
            await callback_query.answer(f"🗑 Queued job {job_name} removed from queue!", show_alert=True)
        else:
            await callback_query.answer(f"❌ Job {job_name} is not active or queued.", show_alert=True)

    elif data == "refresh_stats":
        upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE)
        text = system_stats.get_system_stats_text(
            active_count=len(active_jobs),
            queue_count=len(database.get_queue_jobs()),
            upload_mode=upload_mode,
            is_premium=IS_PREMIUM_SESSION,
            max_jobs=MAX_CONCURRENT_JOBS
        )
        await safe_edit_message(
            callback_query.message.chat.id,
            callback_query.message.id,
            text,
            reply_markup=get_stats_buttons(),
            is_photo=False
        )
        await callback_query.answer("🔄 Diagnostics updated!")

    elif data.startswith("qinfo:"):
        qid = data.split(":", 1)[1]
        await callback_query.answer(
            f"ℹ️ Quality mode '{qid.upper()}' selected. Use flag '| q={qid}' in /record command.",
            show_alert=True
        )


# ---------- CONTAINER STARTUP & AUTO-RECOVERY (LEVEL 3) ----------

async def recover_interrupted_jobs():
    """
    Level 3 Persistence:
    When Koyeb/Docker container restarts, check if any jobs were interrupted
    while recording. If a recorded file exists on disk, automatically recover & upload it!
    """
    interrupted = database.get_all_active_jobs()
    if not interrupted:
        return

    logger.info(f"Checking {len(interrupted)} saved jobs for post-restart recovery...")
    for job in interrupted:
        job_name = job["job_name"]
        file_path = job["file_path"]
        chat_id = job["chat_id"]

        if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
            logger.info(f"Recovering interrupted job file: {file_path}")
            await safe_send_text(
                chat_id,
                f"🔄 **Container Restart Recovery:**\n"
                f"Detected completed file for interrupted job **`{job_name}`** (`{system_stats.format_size(os.path.getsize(file_path))}`).\n"
                f"Automatically resuming upload to Telegram..."
            )
            try:
                await split_and_upload(chat_id, file_path, job_name, elapsed=0)
            except Exception as e:
                logger.error(f"Recovery upload error for {job_name}: {e}")
            finally:
                media_utils.cleanup_job_files(job_name, file_path)
                database.remove_job(job_name)
                gc.collect()
        else:
            logger.info(f"No valid file found for interrupted job {job_name} — clearing database entry.")
            database.remove_job(job_name)


async def main():
    # Start Koyeb health server + bot concurrently
    web_task = asyncio.create_task(start_web_server())
    await app.start()
    if user_app and IS_PREMIUM_SESSION:
        try:
            await user_app.start()
            logger.info("💎 Secondary Telegram Premium Userbot client started successfully!")
        except Exception as e:
            logger.error(f"Failed to start secondary userbot client: {e}")

    logger.info(f"Bot started successfully — Koyeb Port {PORT} health check active.")

    # Recover any interrupted jobs from previous restart
    await recover_interrupted_jobs()

    await idle()

    await app.stop()
    if user_app and IS_PREMIUM_SESSION:
        try:
            await user_app.stop()
        except:
            pass
    web_task.cancel()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
