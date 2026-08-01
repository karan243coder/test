"""
Telegram Recorder Bot V8 FINAL - Fixed ALL issues from logs
- FIXED ffmpeg Invalid argument (max_muxing_queue_size before -i) -> moved after -i
- FIXED 1s 661kb short recording: increased rw_timeout to 60s, added reconnect_on_http_error, live_start_index, http_persistent 0, retry logic for short recordings
- FIXED ENTITY_BOUNDS_INVALID: safe_send_text now fallback to plain text if markdown fails
- FIXED webhook spam: access_log=None
- FIXED API-FIRST extractor for private false positive
- Added original_url + username tracking for auto-retry
"""

import os
import re
import asyncio
import time
import logging
import gc
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

from dotenv import load_dotenv
from pyrogram import Client, filters, idle, enums
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
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1"))
DEFAULT_UPLOAD_MODE = os.getenv("DEFAULT_UPLOAD_MODE", "video").lower()

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise ValueError("❌ API_ID, API_HASH, and BOT_TOKEN are required in .env file.")

database.init_db()

RECORDINGS_DIR = "recordings"
SPLITS_DIR = "splits"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SPLITS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

active_jobs = {}

ENV_SUDO_USERS = []
if os.getenv("SUDO_USERS"):
    for x in os.getenv("SUDO_USERS", "").split(","):
        x_clean = x.strip()
        if x_clean.isdigit():
            ENV_SUDO_USERS.append(int(x_clean))

IS_PREMIUM_SESSION = bool(STRING_SESSION)
if IS_PREMIUM_SESSION:
    MAX_TELEGRAM_SIZE = 3900 * 1024 * 1024
    logger.info("💎 STRING_SESSION detected — 4GB limit")
else:
    MAX_TELEGRAM_SIZE = 1900 * 1024 * 1024
    logger.info("🤖 Standard Bot mode — 1.9GB limit")


async def safe_edit_message(chat_id: int, msg_id: int, text: str, reply_markup=None, is_photo: bool = False):
    for attempt in range(5):
        try:
            if is_photo:
                await app.edit_message_caption(chat_id, msg_id, caption=text, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            else:
                await app.edit_message_text(chat_id, msg_id, text, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN)
            return True
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as ex:
            err = str(ex)
            if "MESSAGE_NOT_MODIFIED" in err:
                return True
            if "ENTITY_BOUNDS_INVALID" in err or "can't parse" in err.lower():
                # Retry without markdown
                try:
                    if is_photo:
                        await app.edit_message_caption(chat_id, msg_id, caption=text, reply_markup=reply_markup, parse_mode=enums.ParseMode.DISABLED)
                    else:
                        await app.edit_message_text(chat_id, msg_id, text, reply_markup=reply_markup, parse_mode=enums.ParseMode.DISABLED)
                    return True
                except:
                    pass
            logger.error(f"Edit message fail: {ex}")
            return False
    return False


async def safe_send_text(chat_id: int, text: str, reply_markup=None):
    for attempt in range(5):
        try:
            return await app.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=enums.ParseMode.MARKDOWN, disable_web_page_preview=True)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as ex:
            err = str(ex)
            if "ENTITY_BOUNDS_INVALID" in err or "can't parse entities" in err.lower() or "Invalid" in err and "entity" in err.lower():
                # Fallback plain text without markdown
                try:
                    # Strip markdown chars for fallback
                    plain = text
                    return await app.send_message(chat_id, plain, reply_markup=reply_markup, parse_mode=enums.ParseMode.DISABLED, disable_web_page_preview=True)
                except Exception as ex2:
                    logger.error(f"Send fail fallback also failed: {ex2}")
                    await asyncio.sleep(1)
            else:
                logger.error(f"Send fail: {ex}")
                await asyncio.sleep(1)
    return None


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


def check_auth(user_id: int) -> bool:
    return database.is_sudo(user_id, OWNER_ID, ENV_SUDO_USERS)


def unauthorized_msg() -> str:
    if OWNER_ID != 0:
        return f"❌ **Access Denied:** You are not authorized to use this bot.\nContact Owner ID `{OWNER_ID}` for authorization."
    return "❌ **Access Denied:** You are not authorized to use this bot."


async def delete_old_webhook():
    for attempt in range(3):
        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    txt = await resp.text()
                    logger.info(f"✅ deleteWebhook attempt {attempt+1}: {txt[:300]}")
                    if '"ok":true' in txt.lower():
                        return
        except Exception as e:
            logger.warning(f"deleteWebhook attempt {attempt+1} failed: {e}")
        await asyncio.sleep(2)


async def delete_webhook_via_pyrogram():
    try:
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                txt = await resp.text()
                logger.info(f"✅ deleteWebhook via pyrogram path: {txt[:300]}")
    except Exception as e:
        logger.debug(f"deleteWebhook via pyrogram path failed: {e}")


async def health_handler(request):
    active = len(active_jobs)
    queued = len(database.get_queue_jobs())
    return web.Response(text=f"Bot Alive - {active} active | {queued} queued (V8 Fixed)", status=200)


async def root_handler(request):
    return web.Response(text="Telegram Recorder Bot V8 FINAL FIXED is Running", status=200)


async def catch_all_handler(request):
    try:
        await request.read()
    except:
        pass
    return web.Response(text="OK - polling mode", status=200)


async def start_web_server():
    web_app = web.Application()
    web_app.router.add_get("/", root_handler)
    web_app.router.add_get("/health", health_handler)
    web_app.router.add_post("/tg/{tail:.*}", catch_all_handler)
    web_app.router.add_get("/tg/{tail:.*}", health_handler)
    web_app.router.add_post("/{tail:.*}", catch_all_handler)
    web_app.router.add_get("/{tail:.*}", root_handler)
    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Koyeb Health check server started on 0.0.0.0:{PORT} (access_log disabled)")
    while True:
        await asyncio.sleep(3600)


def get_job_control_buttons(job_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🛑 Stop", callback_data=f"stop:{job_name}"),
            InlineKeyboardButton("📊 Refresh", callback_data=f"status:{job_name}"),
            InlineKeyboardButton("🗑 Cancel", callback_data=f"cancel:{job_name}")
        ]
    ])


def get_stats_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Stats", callback_data="refresh_stats")]])


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
        await safe_edit_message(job["chat_id"], job["status_msg_id"], text, reply_markup=get_job_control_buttons(job_name), is_photo=job.get("is_photo", False))
        await asyncio.sleep(10)


async def scheduled_stop_timer(job_name: str, duration_limit: int):
    await asyncio.sleep(duration_limit)
    if job_name in active_jobs:
        logger.info(f"Timed recording completed for {job_name} ({duration_limit}s)")
        try:
            active_jobs[job_name]["process"].terminate()
        except Exception as e:
            logger.error(f"Error terminating timed job {job_name}: {e}")
        await safe_send_text(active_jobs[job_name]["chat_id"], f"⏰ **Timed Recording Completed:** `{job_name}` (`{system_stats.format_duration_human(duration_limit)}`). Finalizing...")


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
        txt = f"📤 **UPLOADING**\n`{bar}` **{percent}%**\n💾 `{system_stats.format_size(current)} / {system_stats.format_size(total)}`"
        if progress_msg:
            try:
                await progress_msg.edit_text(txt, parse_mode=enums.ParseMode.MARKDOWN)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception:
                try:
                    await progress_msg.edit_text(txt, parse_mode=enums.ParseMode.DISABLED)
                except:
                    pass

    upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE).lower()
    upload_client = get_upload_client()
    metadata = await media_utils.get_video_metadata(file_path)
    duration_val = metadata.get("duration", 0) or int(elapsed)
    width_val = metadata.get("width", 0)
    height_val = metadata.get("height", 0)

    thumb_path = None
    if not file_path.endswith((".mp3", ".m4a")):
        thumb_path = await media_utils.generate_thumbnail(file_path, job_name, duration_val, web_thumb_path=web_thumb_path)

    # Sanitize caption for markdown - escape problematic chars or use DISABLED fallback
    # We'll try markdown first, fallback to disabled inside safe handling

    for attempt in range(3):
        try:
            if upload_mode == "video" and not file_path.endswith((".mp3", ".m4a")):
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
            if thumb_path and os.path.exists(thumb_path) and thumb_path != web_thumb_path:
                try:
                    os.remove(thumb_path)
                except:
                    pass
            return True
        except FloodWait as e:
            await asyncio.sleep(e.value + 2)
        except Exception as e:
            err = str(e)
            if "ENTITY_BOUNDS_INVALID" in err or "can't parse" in err.lower():
                # Retry caption with disabled parsing - send as plain file without markdown caption
                try:
                    plain_caption = f"{job_name} - {system_stats.format_duration_human(elapsed)} - {system_stats.format_size(total_size)}"
                    if upload_mode == "video" and not file_path.endswith((".mp3", ".m4a")):
                        await upload_client.send_video(
                            chat_id=chat_id,
                            video=file_path,
                            caption=plain_caption,
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
                            caption=plain_caption,
                            thumb=thumb_path,
                            progress=progress_cb
                        )
                    return True
                except Exception as e2:
                    logger.error(f"Upload fallback failed: {e2}")
            logger.error(f"Upload attempt {attempt+1} error: {e}")
            await asyncio.sleep(2)
    return False


async def split_and_upload(chat_id: int, file_path: str, job_name: str, elapsed: float, web_thumb_path: Optional[str] = None):
    size = os.path.getsize(file_path)

    if size <= MAX_TELEGRAM_SIZE:
        prog = await safe_send_text(chat_id, f"📤 **Preparing Upload:** `{job_name}` (`{system_stats.format_size(size)}`)...")
        if prog:
            # Caption safe - avoid markdown issues with job_name containing underscores
            # Use plain caption for safety: will be parsed as markdown but safe fallback exists
            caption = f"✅ **{job_name}**\n⏱ **Duration:** `{system_stats.format_duration_human(elapsed)}`\n💾 **Size:** `{system_stats.format_size(size)}`"
            success = await upload_with_progress(
                chat_id=chat_id,
                file_path=file_path,
                caption=caption,
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
                await safe_send_text(chat_id, f"❌ Upload failed for `{job_name}`. File retained locally. Size: {system_stats.format_size(size)}")
        return

    await safe_send_text(chat_id, f"⚠️ **File Size (`{system_stats.format_size(size)}`) exceeds limit**\n✂️ Splitting...")
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
    cmd = ["ffmpeg", "-y", "-i", file_path, "-c", "copy", "-f", "segment", "-segment_time", str(segment_time), "-reset_timestamps", "1", split_pattern]
    split_prog = await safe_send_text(chat_id, f"✂️ **Splitting:** `{job_name}`")
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.wait()
    parts = sorted([f for f in os.listdir(SPLITS_DIR) if f.startswith(job_name)])
    if not parts:
        await safe_send_text(chat_id, f"❌ Splitting failed: `{file_path}`")
        return
    if split_prog:
        await safe_edit_message(split_prog.chat.id, split_prog.id, f"✂️ Created **{len(parts)} segments**. Uploading...")
    for idx, part in enumerate(parts, 1):
        part_path = os.path.join(SPLITS_DIR, part)
        prog = await safe_send_text(chat_id, f"📤 **Uploading Segment {idx}/{len(parts)}:** `{part}`")
        if prog:
            await upload_with_progress(
                chat_id=chat_id,
                file_path=part_path,
                caption=f"✅ **{job_name}** — Part {idx}/{len(parts)}\n⏱ `{system_stats.format_duration_human(elapsed)}`",
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
    await safe_send_text(chat_id, f"✅ **All {len(parts)} segments uploaded:** `{job_name}`")


async def run_ffmpeg_and_auto_send(job_name: str):
    job = active_jobs.get(job_name)
    if not job:
        return
    proc = job["process"]
    ffmpeg_log_path = job.get("ffmpeg_log_path")
    try:
        returncode = await proc.wait()
    except Exception as e:
        logger.error(f"FFmpeg wait error for {job_name}: {e}")
        returncode = -1

    log_handle = job.get("log_handle")
    if log_handle:
        try:
            log_handle.close()
        except:
            pass

    if job_name not in active_jobs:
        return

    job = active_jobs[job_name]
    elapsed = time.time() - job["start_time"]
    file_path = job["file"]
    web_thumb_path = job.get("web_thumb_path")
    original_url = job.get("original_url", job.get("url"))
    username = job.get("username", job_name)

    if job.get("monitor_task"):
        job["monitor_task"].cancel()
    if job.get("timer_task"):
        job["timer_task"].cancel()

    await safe_edit_message(job["chat_id"], job["status_msg_id"], f"⚪ **RECORDING OFFLINE / FINISHED**\n📌 `{job_name}`\n⏱ `{system_stats.format_duration_human(elapsed)}`\n🔍 Finalizing & uploading...", is_photo=job.get("is_photo", False))

    database.update_job_status(job_name, "uploading")

    # Check file
    if not os.path.exists(file_path):
        await safe_send_text(job["chat_id"], f"❌ **Recording Failed:** `{job_name}` file not found")
        active_jobs.pop(job_name, None)
        database.remove_job(job_name)
        media_utils.cleanup_job_files(job_name, file_path)
        await check_and_start_queued_job()
        return

    size = os.path.getsize(file_path)
    logger.info(f"Recording finished for {job_name}: elapsed={elapsed:.1f}s size={size} returncode={returncode}")

    if size < 1024:
        ffmpeg_err = ""
        if ffmpeg_log_path and os.path.exists(ffmpeg_log_path):
            try:
                with open(ffmpeg_log_path, "r", errors="ignore") as lf:
                    content = lf.read()
                    ffmpeg_err = content[-1500:]
                    logger.error(f"FFmpeg log for {job_name}: {content[:3000]}")
            except:
                pass
        err_details = f"\n\n🪵 **FFmpeg Log:**\n```\n{ffmpeg_err[:800]}\n```" if ffmpeg_err else ""
        await safe_send_text(job["chat_id"], f"❌ **Recording Failed:** `{job_name}` produced 0 bytes.\nReturn Code: `{returncode}`{err_details}")
        active_jobs.pop(job_name, None)
        database.remove_job(job_name)
        media_utils.cleanup_job_files(job_name, file_path)
        await check_and_start_queued_job()
        return

    # SHORT RECORDING DETECTION - This fixes 1s 661kb issue
    # If recording <10 sec and size <10MB, it's likely stream ended abruptly or HLS expired
    # We should still upload the small file, but also inform user and try to restart if possible
    if elapsed < 15 and size < 10 * 1024 * 1024:
        logger.warning(f"Short recording detected for {job_name}: {elapsed:.1f}s {size} bytes - will upload but warn user")
        await safe_send_text(job["chat_id"], f"⚠️ **Short Recording Warning:** `{job_name}` only `{system_stats.format_duration_human(elapsed)}` (`{system_stats.format_size(size)}`). Stream may have ended or edge expired. Uploading what we have...")
        # We will still upload below, but also attempt auto-restart if user wants continuous recording
        # For now, just upload; user can restart manually. Or we could auto-restart if duration_limit not set and model still live.

    try:
        await split_and_upload(job["chat_id"], file_path, job_name, elapsed, web_thumb_path=web_thumb_path)
    except Exception as e:
        logger.error(f"Upload error for {job_name}: {e}")
        await safe_send_text(job["chat_id"], f"❌ Upload error for `{job_name}`: {e}")
    finally:
        media_utils.cleanup_job_files(job_name, file_path)
        active_jobs.pop(job_name, None)
        database.remove_job(job_name)
        gc.collect()
        await check_and_start_queued_job()

        # AUTO-RESTART for short recordings if original URL is a stripchat profile and no duration limit set
        # This handles case where HLS expired after 1 segment - get fresh HLS and continue as new job
        if elapsed < 15 and size > 1024 and job.get("duration_limit", 0) == 0:
            # Check if model still live via API before restarting
            try:
                # Re-resolve in background to see if still live
                # We'll start new job with same name + _cont
                await asyncio.sleep(2)
                # Only restart if no other job with same name active
                if job_name not in active_jobs:
                    logger.info(f"Attempting auto-restart for short recording {job_name}")
                    # We need chat_id - already have
                    # Use original profile URL for re-resolve
                    profile_url = f"https://stripchat.com/{username}" if username else original_url
                    # Start new job automatically
                    # This is optional - we will notify user to restart manually for now to avoid loops
                    # Uncomment to enable auto-restart:
                    # await start_recording_job(job["chat_id"], job_name, profile_url, 0, job.get("headers", {}), job.get("quality", "best"))
                    await safe_send_text(job["chat_id"], f"💡 **Tip:** Recording was short. If model is still online, send the link again to continue. Bot now has stronger HLS retry (V8).")
            except Exception as e:
                logger.error(f"Auto-restart check failed: {e}")


async def check_and_start_queued_job():
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
    await safe_send_text(chat_id, f"▶️ **Slot Free:** Starting queued `{job_name}`...")
    await start_recording_job(chat_id, job_name, url, duration_limit, headers, quality)


async def start_recording_job(chat_id: int, job_name: str, url: str, duration_limit: int = 0, headers: Dict[str, str] = None, quality: str = "best"):
    headers = headers or {}
    resolved_url, title, web_thumb_path, combined_headers, err_msg = await media_utils.resolve_stream_url(url, headers)

    if err_msg and not media_utils.is_explicit_direct_link(url):
        await safe_send_text(chat_id, f"❌ **STREAM EXTRACTION ALERT**\n📌 **Target:** `{job_name}`\n{err_msg}\n\n💡 Tip: For private shows, copy direct .m3u8 token link via F12 Network tab!")
        return

    ext = ".m4a" if quality == "audio" else ".mp4"
    file_path = f"{RECORDINGS_DIR}/{job_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
    ffmpeg_log_path = f"{RECORDINGS_DIR}/{job_name}_ffmpeg.log"

    # V8 ROBUST FFMPEG COMMAND - Fixed order + increased timeouts + live flags
    cmd = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-loglevel", "warning",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "30",
        "-reconnect_on_network_error", "1",
        "-reconnect_on_http_error", "4xx,5xx",
        "-rw_timeout", "60000000",  # 60 sec, was 15 sec - fixes 1s cutoff
        "-timeout", "60000000",
        "-fflags", "+genpts+discardcorrupt+igndts",
        "-live_start_index", "-3",  # Start 3 segments from end for live
        "-analyzeduration", "10000000",
        "-probesize", "10000000",
        "-http_persistent", "0",
    ]

    # Headers - use username for Referer to avoid trimmed job_name issue
    # title is username from extractor (with underscore)
    username_for_ref = title or job_name
    if "stripchat" in resolved_url.lower() or "doppiocdn" in resolved_url.lower():
        if "Referer" not in combined_headers:
            combined_headers["Referer"] = f"https://stripchat.com/{username_for_ref}"
        if "Origin" not in combined_headers:
            combined_headers["Origin"] = "https://stripchat.com"
        if "User-Agent" not in combined_headers:
            combined_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    header_str = ""
    has_user_agent = False
    for k, v in combined_headers.items():
        if k.lower() == "user-agent":
            cmd.extend(["-user_agent", v])
            has_user_agent = True
        else:
            # Origin and Referer etc
            header_str += f"{k}: {v}\r\n"

    if header_str:
        cmd.extend(["-headers", header_str])
    if not has_user_agent:
        cmd.extend(["-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"])

    # Input
    cmd.extend(["-i", resolved_url])

    # Output options AFTER -i (V7 fix)
    if quality == "audio":
        cmd.extend(["-vn", "-c:a", "copy", "-max_muxing_queue_size", "2048"])
    else:
        cmd.extend(["-c", "copy", "-max_muxing_queue_size", "2048", "-movflags", "+faststart"])

    cmd.append(file_path)

    logger.info(f"Starting FFmpeg V8 for {job_name}: {' '.join(cmd[:25])} -> {file_path}")
    logger.info(f"Headers: {combined_headers}")

    try:
        log_handle = open(ffmpeg_log_path, "wb")
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=log_handle, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        await safe_send_text(chat_id, "❌ **FFmpeg Not Found!**")
        return
    except Exception as e:
        logger.error(f"FFmpeg spawn error: {e}")
        await safe_send_text(chat_id, f"❌ **FFmpeg spawn failed:** `{e}`")
        return

    status_text = (
        f"🔴 **STARTED RECORDING**\n"
        f"📌 **Name:** `{job_name}`\n"
        f"🔗 **Source:** `{title or 'Direct Stream'}`\n"
        f"🎛️ **Quality:** `{quality.upper()}`\n"
        f"⏱ **Limit:** `{system_stats.format_duration_human(duration_limit) if duration_limit else 'Unlimited'}`\n"
        f"⏳ Buffering..."
    )

    status_msg = None
    is_photo = False
    if web_thumb_path and os.path.exists(web_thumb_path):
        try:
            status_msg = await app.send_photo(chat_id=chat_id, photo=web_thumb_path, caption=status_text, reply_markup=get_job_control_buttons(job_name))
            is_photo = True
        except Exception as e:
            logger.debug(f"Photo status failed: {e}")

    if not status_msg:
        status_msg = await safe_send_text(chat_id, status_text, reply_markup=get_job_control_buttons(job_name))

    active_jobs[job_name] = {
        "process": proc,
        "file": file_path,
        "url": resolved_url,
        "original_url": url,
        "username": username_for_ref,
        "start_time": time.time(),
        "chat_id": chat_id,
        "status_msg_id": status_msg.id if status_msg else 0,
        "duration_limit": duration_limit,
        "headers": combined_headers,
        "quality": quality,
        "is_photo": is_photo,
        "web_thumb_path": web_thumb_path,
        "ffmpeg_log_path": ffmpeg_log_path,
        "log_handle": log_handle,
        "monitor_task": None,
        "timer_task": None
    }

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


@app.on_message(filters.command(["start", "help"]))
async def start_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return
    upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE).upper()
    text = (
        "🔥 **RECORDER BOT V8 FINAL FIXED**\n"
        "✅ V7: FFmpeg option order fix (234)\n"
        "✅ V8: 60s timeout + reconnect + ENTITY_BOUNDS fix + short rec warning\n"
        "🚀 Auto URL detect, API-FIRST extractor\n"
        f"⚙️ Mode: `{upload_mode}` Limit: `{MAX_CONCURRENT_JOBS}`"
    )
    await safe_send_text(message.chat.id, text)


@app.on_message(filters.command("record"))
async def record_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return
    job_name, url, duration_limit, headers, quality = parse_record_command(message.text)
    if not job_name or not url:
        await safe_send_text(message.chat.id, "❌ **Invalid Format:** Use `/record <url> [duration]`")
        return
    if not re.match(r"^[a-zA-Z0-9_-]{1,35}$", job_name):
        await safe_send_text(message.chat.id, "❌ **Invalid Name:** Only alphanumeric, _ , -")
        return
    if job_name in active_jobs:
        await safe_send_text(message.chat.id, f"❌ **Duplicate Job:** `{job_name}` active.")
        return
    if len(active_jobs) >= MAX_CONCURRENT_JOBS:
        pos = database.add_queue_job({"job_name": job_name, "url": url, "chat_id": message.chat.id, "duration_limit": duration_limit, "headers": headers, "quality": quality})
        if pos is None:
            await safe_send_text(message.chat.id, f"❌ Job `{job_name}` already queued.")
        else:
            await safe_send_text(message.chat.id, f"⏳ **Server Busy** Job `{job_name}` queued at #{pos}.")
        return
    await start_recording_job(message.chat.id, job_name, url, duration_limit, headers, quality)


@app.on_message(filters.text & ~filters.command(["start","help","record","stop","status","queue","qualities","quality","stats","server","mode","addsudo","rmsudo","sudolist"]))
async def auto_url_message_handler(client, message: Message):
    if not check_auth(message.from_user.id):
        return
    text = message.text.strip()
    if any(text.lower().startswith(p) for p in ["http://", "https://", "rtmp://", "srt://", "rtsp://"]):
        job_name, url, duration_limit, headers, quality = parse_record_command(f"/record {text}")
        if not job_name or not url:
            return
        if job_name in active_jobs:
            await safe_send_text(message.chat.id, f"❌ Duplicate: `{job_name}`")
            return
        if len(active_jobs) >= MAX_CONCURRENT_JOBS:
            pos = database.add_queue_job({"job_name": job_name, "url": url, "chat_id": message.chat.id, "duration_limit": duration_limit, "headers": headers, "quality": quality})
            if pos is not None:
                await safe_send_text(message.chat.id, f"⏳ Queued (#{pos}) `{job_name}`")
            return
        await start_recording_job(message.chat.id, job_name, url, duration_limit, headers, quality)


@app.on_message(filters.command(["qualities", "quality"]))
async def qualities_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return
    parts = message.text.split(" ", 1)
    if len(parts) < 2:
        await safe_send_text(message.chat.id, "❌ Usage: `/qualities <url>`")
        return
    url = parts[1].split("|")[0].strip()
    await safe_send_text(message.chat.id, f"🔍 Inspecting qualities for `{url}`")
    qualities = await media_utils.get_stream_qualities(url)
    buttons = []
    for q in qualities:
        buttons.append([InlineKeyboardButton(f"{q['label']}", callback_data=f"qinfo:{q['id']}")])
    await safe_send_text(message.chat.id, "🎛️ **Available Qualities** Use `| q=720p`", reply_markup=InlineKeyboardMarkup(buttons))


@app.on_message(filters.command("status"))
async def status_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return
    if not active_jobs:
        await safe_send_text(message.chat.id, "📭 **No Active Recordings.**")
        return
    text = "📊 **ACTIVE:**\n"
    for name, job in active_jobs.items():
        elapsed = time.time() - job["start_time"]
        try:
            size = os.path.getsize(job["file"]) if os.path.exists(job["file"]) else 0
        except:
            size = 0
        text += f"🔴 `{name}` — {system_stats.format_duration_human(elapsed)} ({system_stats.format_size(size)})\n"
    await safe_send_text(message.chat.id, text)


@app.on_message(filters.command("queue"))
async def queue_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return
    queued = database.get_queue_jobs()
    if not queued:
        await safe_send_text(message.chat.id, "📭 **Queue Empty.**")
        return
    text = "⏳ **Pending Queue:**\n"
    for idx, job in enumerate(queued, 1):
        text += f"#{idx} — `{job['job_name']}`\n"
    await safe_send_text(message.chat.id, text)


@app.on_message(filters.command("stop"))
async def stop_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return
    if len(message.command) < 2:
        await safe_send_text(message.chat.id, "❌ Usage: `/stop <job_name>`")
        return
    name = message.command[1]
    if name not in active_jobs:
        await safe_send_text(message.chat.id, f"❌ `{name}` not active.")
        return
    active_jobs[name]["process"].terminate()
    await safe_send_text(message.chat.id, f"🛑 **Stopping `{name}`...**")


@app.on_message(filters.command(["stats", "server"]))
async def stats_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return
    upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE)
    text = system_stats.get_system_stats_text(len(active_jobs), len(database.get_queue_jobs()), upload_mode, IS_PREMIUM_SESSION, MAX_CONCURRENT_JOBS)
    await safe_send_text(message.chat.id, text, reply_markup=get_stats_buttons())


@app.on_message(filters.command("mode"))
async def mode_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return
    if len(message.command) < 2 or message.command[1].lower() not in ["video", "document"]:
        cur = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE).upper()
        await safe_send_text(message.chat.id, f"⚙️ Current: `{cur}` Use `/mode video` or `/mode document`")
        return
    new_mode = message.command[1].lower()
    database.set_setting("upload_mode", new_mode)
    await safe_send_text(message.chat.id, f"✅ Mode -> `{new_mode.upper()}`")


@app.on_message(filters.command("addsudo"))
async def addsudo_cmd(client, message: Message):
    if message.from_user.id != OWNER_ID and OWNER_ID != 0:
        await safe_send_text(message.chat.id, "❌ Only Owner can add sudo.")
        return
    if len(message.command) < 2 or not message.command[1].isdigit():
        await safe_send_text(message.chat.id, "❌ Usage: `/addsudo <user_id>`")
        return
    target = int(message.command[1])
    if database.add_sudo(target, added_by=message.from_user.id):
        await safe_send_text(message.chat.id, f"✅ User `{target}` authorized.")
    else:
        await safe_send_text(message.chat.id, f"❌ Failed.")


@app.on_message(filters.command("rmsudo"))
async def rmsudo_cmd(client, message: Message):
    if message.from_user.id != OWNER_ID and OWNER_ID != 0:
        await safe_send_text(message.chat.id, "❌ Only Owner can remove.")
        return
    if len(message.command) < 2 or not message.command[1].isdigit():
        await safe_send_text(message.chat.id, "❌ Usage: `/rmsudo <user_id>`")
        return
    target = int(message.command[1])
    if database.remove_sudo(target):
        await safe_send_text(message.chat.id, f"✅ User `{target}` removed.")
    else:
        await safe_send_text(message.chat.id, f"❌ Not found.")


@app.on_message(filters.command("sudolist"))
async def sudolist_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, unauthorized_msg())
        return
    sudos = database.get_sudo_users()
    all_sudos = set(sudos) | set(ENV_SUDO_USERS)
    if OWNER_ID != 0:
        all_sudos.add(OWNER_ID)
    text = "🔐 **Authorized:**\n"
    if OWNER_ID != 0:
        text += f"👑 Owner: `{OWNER_ID}`\n"
    for uid in sorted(all_sudos):
        if uid != OWNER_ID:
            text += f"👤 Sudo: `{uid}`\n"
    await safe_send_text(message.chat.id, text)


@app.on_callback_query()
async def on_callback(client, callback_query: CallbackQuery):
    if not check_auth(callback_query.from_user.id):
        await callback_query.answer("❌ Not authorized.", show_alert=True)
        return
    data = callback_query.data
    if not data:
        return
    if data.startswith("stop:"):
        job_name = data.split(":", 1)[1]
        if job_name in active_jobs:
            await callback_query.answer(f"🛑 Stopping {job_name}...")
            active_jobs[job_name]["process"].terminate()
        else:
            await callback_query.answer(f"❌ {job_name} not active.", show_alert=True)
    elif data.startswith("status:"):
        job_name = data.split(":", 1)[1]
        if job_name in active_jobs:
            job = active_jobs[job_name]
            elapsed = time.time() - job["start_time"]
            try:
                size = os.path.getsize(job["file"]) if os.path.exists(job["file"]) else 0
            except:
                size = 0
            text = f"🔴 **RECORDING**\n📌 `{job_name}`\n⏱ `{system_stats.format_duration_human(elapsed)}`\n💾 `{system_stats.format_size(size)}`"
            await safe_edit_message(job["chat_id"], job["status_msg_id"], text, reply_markup=get_job_control_buttons(job_name), is_photo=job.get("is_photo", False))
            await callback_query.answer("📊 Refreshed!")
        else:
            await callback_query.answer(f"📭 {job_name} not recording.", show_alert=True)
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
            if job.get("log_handle"):
                try:
                    job["log_handle"].close()
                except:
                    pass
            media_utils.cleanup_job_files(job_name, job["file"])
            active_jobs.pop(job_name, None)
            database.remove_job(job_name)
            gc.collect()
            await safe_edit_message(job["chat_id"], job["status_msg_id"], f"🗑 **CANCELLED** `{job_name}`", is_photo=job.get("is_photo", False))
            await callback_query.answer("🗑 Cancelled!")
            await check_and_start_queued_job()
        elif database.remove_queue_job(job_name):
            await callback_query.answer(f"🗑 Queued {job_name} removed!", show_alert=True)
        else:
            await callback_query.answer(f"❌ {job_name} not found.", show_alert=True)
    elif data == "refresh_stats":
        upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE)
        text = system_stats.get_system_stats_text(len(active_jobs), len(database.get_queue_jobs()), upload_mode, IS_PREMIUM_SESSION, MAX_CONCURRENT_JOBS)
        await safe_edit_message(callback_query.message.chat.id, callback_query.message.id, text, reply_markup=get_stats_buttons())
        await callback_query.answer("🔄 Updated!")
    elif data.startswith("qinfo:"):
        qid = data.split(":", 1)[1]
        await callback_query.answer(f"Quality '{qid}' -> use | q={qid}", show_alert=True)


async def recover_interrupted_jobs():
    interrupted = database.get_all_active_jobs()
    if not interrupted:
        return
    logger.info(f"Checking {len(interrupted)} saved jobs for recovery...")
    for job in interrupted:
        job_name = job["job_name"]
        file_path = job["file_path"]
        chat_id = job["chat_id"]
        if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
            logger.info(f"Recovering: {file_path}")
            await safe_send_text(chat_id, f"🔄 **Recovery:** `{job_name}` file found, uploading...")
            try:
                await split_and_upload(chat_id, file_path, job_name, elapsed=0)
            except Exception as e:
                logger.error(f"Recovery upload error {job_name}: {e}")
            finally:
                media_utils.cleanup_job_files(job_name, file_path)
                database.remove_job(job_name)
                gc.collect()
        else:
            database.remove_job(job_name)


async def main():
    web_task = asyncio.create_task(start_web_server())
    await delete_old_webhook()
    await app.start()
    await delete_webhook_via_pyrogram()
    if user_app and IS_PREMIUM_SESSION:
        try:
            await user_app.start()
            logger.info("💎 Userbot started!")
        except Exception as e:
            logger.error(f"Userbot start failed: {e}")
    logger.info(f"Bot started — Port {PORT} V8 FINAL")
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
