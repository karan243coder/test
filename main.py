"""
main.py - SUPER ADVANCED TELEGRAM RECORDER BOT V11 (MOUFLON AD-FIX)
==================================================
What's new / fixed vs the broken V8/V9:
  1. FIXED: yt-dlp is the primary stream resolver (Stripchat official
     extractor - handles anti-bot, private-show/offline detection, dynamic
     edge-HLS hosts). The old dead "custom extractor" that was never wired
     in has been removed.
  2. NEW: 24/7 Auto-Watchlist - /watch <link>, bot checks models every
     WATCH_INTERVAL seconds and auto-records + auto-sends when they go live.
  3. NEW: real MOUFLON proxy endpoint (playlist decryption when pkey:pdkey
     pairs are provided).
  4. NEW: auto-restart when a live stream drops (HLS expiry) - up to
     MAX_AUTO_RESTARTS times.
  5. NEW: status transition notifications (offline/private/public).
  6. Kept: /record, /stop, /status, /queue, /stats, /mode, sudo management,
     queue system, split-upload >1.9GB/3.9GB, Koyeb health server.

Commands:
  /record <name> <url> [duration] [| q=best]   start recording
  /watch <link-or-username>                    add to 24/7 auto-watchlist
  /unwatch <username>                          remove from watchlist
  /watchlist                                   list watchlist
  /check <link-or-username>                    check model status now
  /stop <name>  /status  /queue  /stats  /mode video|document
  /watchinterval <sec>  /addsudo <id>  /rmsudo <id>  /sudolist  /clean
  Or simply paste any URL to start recording.
"""

import os
import re
import time
import asyncio
import logging
import gc
import urllib.parse
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv
from pyrogram import Client, filters, enums, idle
from pyrogram.errors import FloodWait
from pyrogram.types import (Message, InlineKeyboardMarkup, InlineKeyboardButton,
                            CallbackQuery)
from aiohttp import web

import database
import media_utils
import recorder
import system_stats

load_dotenv()

# ----------------------------- CONFIG -----------------------------
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
STRING_SESSION = os.getenv("STRING_SESSION", "").strip()
PORT = int(os.getenv("PORT", "8080") or 8080)
MAX_CONCURRENT_JOBS = int(os.getenv("MAX_CONCURRENT_JOBS", "1") or 1)
DEFAULT_UPLOAD_MODE = os.getenv("DEFAULT_UPLOAD_MODE", "video").lower()
WATCH_INTERVAL = int(os.getenv("WATCH_INTERVAL", "180") or 180)
WATCH_COOLDOWN = int(os.getenv("WATCH_COOLDOWN", "300") or 300)
MAX_AUTO_RESTARTS = int(os.getenv("MAX_AUTO_RESTARTS", "3") or 3)
KEEP_FAILED_LOGS = os.getenv("KEEP_FAILED_LOGS", "1").strip().lower() in {"1", "true", "yes", "on"}

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise ValueError("❌ API_ID, API_HASH and BOT_TOKEN are required in .env")

ENV_SUDO_USERS: List[int] = []
for x in os.getenv("SUDO_USERS", "").split(","):
    x = x.strip()
    if x.isdigit():
        ENV_SUDO_USERS.append(int(x))

IS_PREMIUM_SESSION = bool(STRING_SESSION)
MAX_TELEGRAM_SIZE = 3900 * 1024 * 1024 if IS_PREMIUM_SESSION else 1900 * 1024 * 1024

database.init_db()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("recorder_bot")

# ----------------------------- GLOBALS -----------------------------
active_jobs: Dict[str, Dict[str, Any]] = {}
auto_restart_count: Dict[str, int] = {}   # job_name -> restarts used

_BOOT_TIME = time.time()
_last_update_time = time.time()   # watchdog ke liye
VERSION = "V11.2-DIAG"           # health endpoint mein dikhega - deploy verify karne ke liye

app = Client("recorder_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workdir=".")
user_app = None
if STRING_SESSION:
    user_app = Client("recorder_userbot", api_id=API_ID, api_hash=API_HASH,
                      session_string=STRING_SESSION, workdir=".")


def get_upload_client():
    return user_app if (user_app and IS_PREMIUM_SESSION) else app


# ----------------------------- HELPERS -----------------------------

def esc(text: str) -> str:
    """Escape markdown special chars in user-controlled strings."""
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(text))


def check_auth(user_id: int) -> bool:
    return database.is_sudo(user_id, OWNER_ID, ENV_SUDO_USERS)


async def safe_send_text(chat_id: int, text: str, reply_markup=None):
    for _ in range(5):
        try:
            return await app.send_message(chat_id, text, reply_markup=reply_markup,
                                          parse_mode=enums.ParseMode.MARKDOWN,
                                          disable_web_page_preview=True)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as ex:
            err = str(ex)
            if "ENTITY_BOUNDS_INVALID" in err or "can't parse" in err.lower():
                try:
                    return await app.send_message(chat_id, text, reply_markup=reply_markup,
                                                  parse_mode=enums.ParseMode.DISABLED,
                                                  disable_web_page_preview=True)
                except Exception:
                    await asyncio.sleep(1)
            else:
                logger.error(f"send fail: {err}")
                await asyncio.sleep(1)
    return None


async def safe_edit_message(chat_id: int, msg_id: int, text: str, reply_markup=None, is_photo=False):
    for _ in range(5):
        try:
            if is_photo:
                return await app.edit_message_caption(chat_id, msg_id, caption=text,
                                                      reply_markup=reply_markup,
                                                      parse_mode=enums.ParseMode.MARKDOWN)
            return await app.edit_message_text(chat_id, msg_id, text, reply_markup=reply_markup,
                                               parse_mode=enums.ParseMode.MARKDOWN)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception as ex:
            err = str(ex)
            if "MESSAGE_NOT_MODIFIED" in err:
                return True
            if "ENTITY_BOUNDS_INVALID" in err or "can't parse" in err.lower():
                try:
                    if is_photo:
                        return await app.edit_message_caption(chat_id, msg_id, caption=text,
                                                              reply_markup=reply_markup,
                                                              parse_mode=enums.ParseMode.DISABLED)
                    return await app.edit_message_text(chat_id, msg_id, text,
                                                       reply_markup=reply_markup,
                                                       parse_mode=enums.ParseMode.DISABLED)
                except Exception:
                    pass
            logger.error(f"edit fail: {err}")
            return False
    return False


def job_control_buttons(job_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Stop", callback_data=f"stop:{job_name}"),
         InlineKeyboardButton("📊 Refresh", callback_data=f"status:{job_name}"),
         InlineKeyboardButton("🗑 Cancel", callback_data=f"cancel:{job_name}")]
    ])


def stats_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="refresh_stats")]])


def watch_buttons(username: str, enabled: bool) -> InlineKeyboardMarkup:
    toggle = "⏸ Pause" if enabled else "▶️ Resume"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=f"wtog:{username}"),
         InlineKeyboardButton("🗑 Remove", callback_data=f"wdel:{username}")]
    ])


# ============================================================
#  RECORDING JOB LIFECYCLE
# ============================================================

async def start_recording_job(chat_id: int, job_name: str, url: str,
                              duration_limit: int = 0,
                              headers: Optional[Dict[str, str]] = None,
                              quality: str = "best",
                              source: str = "manual",
                              watch_chat_id: Optional[int] = None):
    headers = headers or {}

    # hang guard: resolver kabhi 45s se zyada na le
    try:
        resolved = await asyncio.wait_for(
            media_utils.resolve_stream_url(url, headers), timeout=45)
    except asyncio.TimeoutError:
        await safe_send_text(chat_id,
                             f"⏱ **Stream resolve timeout (45s)** — `{esc(job_name)}`\n"
                             f"Network slow hai ya model busy hai. Dobara try karo ya `/watch` laga do.")
        return False
    except Exception as e:
        logger.error(f"resolve crash for {job_name}: {e}", exc_info=True)
        await safe_send_text(chat_id,
                             f"⚠️ **Resolver error:** `{esc(str(e)[:150])}`\n"
                             f"Job `{esc(job_name)}` start nahi hui. Log check karo.")
        return False

    if resolved.get("error"):
        await safe_send_text(
            chat_id,
            f"❌ **STREAM NAHI MILA**\n📌 **Target:** `{esc(job_name)}`\n{resolved['error']}\n\n"
            f"💡 Model public room mein online ho tab try karo. Live hone par `/watch {esc(url.split('/')[-1])}` "
            f"se auto-record ho jayega.")
        return False

    stream_url = resolved["url"]
    title = resolved.get("title") or job_name
    web_thumb_path = resolved.get("thumb_path")

    ext = ".m4a" if quality == "audio" else ".mp4"
    file_path = os.path.join(recorder.RECORDINGS_DIR,
                             f"{job_name}_{time.strftime('%Y%m%d_%H%M%S')}{ext}")
    ffmpeg_log_path = os.path.join(recorder.RECORDINGS_DIR, f"{job_name}_ffmpeg.log")

    # Stripchat headers (Referer/Origin) for CDN requests
    combined = dict(headers)
    if "stripchat" in stream_url.lower() or "doppiocdn" in stream_url.lower():
        ref_name = re.sub(r"[^a-zA-Z0-9_-]", "", str(title))
        combined.setdefault("Referer", f"https://stripchat.com/{ref_name}")
        combined.setdefault("Origin", "https://stripchat.com")
        combined.setdefault("User-Agent", media_utils.DEFAULT_UA)

    cmd = recorder.build_ffmpeg_command(stream_url, file_path, combined, quality)
    logger.info(f"FFmpeg start [{job_name}]: {' '.join(cmd[:22])}... -> {file_path}")

    try:
        proc, log_handle = await recorder.spawn_ffmpeg(cmd, ffmpeg_log_path)
    except FileNotFoundError:
        await safe_send_text(chat_id, "❌ **FFmpeg install nahi hai!** Dockerfile check karo.")
        return False
    except Exception as e:
        await safe_send_text(chat_id, f"❌ FFmpeg spawn fail: `{esc(str(e))}`")
        return False

    status_text = (
        f"🔴 **RECORDING STARTED**\n"
        f"📌 **Name:** `{esc(job_name)}`\n"
        f"👤 **Model:** `{esc(title)}`\n"
        f"🎛️ **Quality:** `{esc(quality.upper())}`\n"
        f"⏱ **Limit:** `{system_stats.format_duration_human(duration_limit) if duration_limit else 'Unlimited'}`\n"
        f"⏳ Buffering..."
    )
    status_msg = None
    is_photo = False
    if web_thumb_path and os.path.exists(web_thumb_path):
        try:
            status_msg = await app.send_photo(chat_id, photo=web_thumb_path, caption=status_text,
                                              reply_markup=job_control_buttons(job_name))
            is_photo = True
        except Exception as e:
            logger.debug(f"photo status fail: {e}")
    if not status_msg:
        status_msg = await safe_send_text(chat_id, status_text, reply_markup=job_control_buttons(job_name))

    active_jobs[job_name] = {
        "process": proc,
        "log_handle": log_handle,
        "file": file_path,
        "ffmpeg_log_path": ffmpeg_log_path,
        "stream_url": stream_url,
        "original_url": url,
        "title": title,
        "start_time": time.time(),
        "chat_id": chat_id,
        "status_msg_id": status_msg.id if status_msg else 0,
        "duration_limit": duration_limit,
        "headers": combined,
        "quality": quality,
        "is_photo": is_photo,
        "web_thumb_path": web_thumb_path,
        "source": source,
        "watch_chat_id": watch_chat_id,
        "monitor_task": None,
        "timer_task": None,
    }

    database.save_job({
        "job_name": job_name, "url": stream_url, "file_path": file_path,
        "chat_id": chat_id, "status_msg_id": status_msg.id if status_msg else 0,
        "start_time": active_jobs[job_name]["start_time"],
        "duration_limit": duration_limit, "headers": combined,
        "quality": quality, "status": "recording",
    })

    active_jobs[job_name]["monitor_task"] = asyncio.create_task(monitor_recording(job_name))
    if duration_limit > 0:
        active_jobs[job_name]["timer_task"] = asyncio.create_task(scheduled_stop_timer(job_name, duration_limit))
    asyncio.create_task(finalize_job(job_name))
    return True


async def monitor_recording(job_name: str):
    while job_name in active_jobs:
        job = active_jobs[job_name]
        elapsed = time.time() - job["start_time"]
        try:
            size = os.path.getsize(job["file"]) if os.path.exists(job["file"]) else 0
        except Exception:
            size = 0
        bps = ""
        if elapsed > 10 and size > 0:
            bps = f"| {system_stats.format_size(size / elapsed)}/s"
        timer_text = ""
        if job.get("duration_limit"):
            rem = max(0, job["duration_limit"] - elapsed)
            timer_text = f"\n⏱️ **Auto-Stop In:** `{system_stats.format_duration_human(rem)}`"
        text = (
            f"🔴 **RECORDING IN PROGRESS**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 **Name:** `{esc(job_name)}`\n"
            f"⏱ **Duration:** `{system_stats.format_duration_human(elapsed)}` {bps}{timer_text}\n"
            f"💾 **Size:** `{system_stats.format_size(size)}`\n"
            f"📁 **File:** `{esc(os.path.basename(job['file']))}`"
        )
        await safe_edit_message(job["chat_id"], job["status_msg_id"], text,
                                reply_markup=job_control_buttons(job_name),
                                is_photo=job.get("is_photo", False))
        await asyncio.sleep(10)


async def scheduled_stop_timer(job_name: str, duration_limit: int):
    await asyncio.sleep(duration_limit)
    if job_name in active_jobs:
        logger.info(f"Timed stop for {job_name}")
        try:
            active_jobs[job_name]["process"].terminate()
        except Exception:
            pass
        await safe_send_text(active_jobs[job_name]["chat_id"],
                             f"⏰ **Timed recording complete:** `{esc(job_name)}`. Finalizing...")


async def finalize_job(job_name: str):
    job = active_jobs.get(job_name)
    if not job:
        return
    proc = job["process"]
    try:
        returncode = await proc.wait()
    except Exception:
        returncode = -1

    if job.get("log_handle"):
        try:
            job["log_handle"].close()
        except Exception:
            pass

    if job_name not in active_jobs:
        return
    job = active_jobs[job_name]
    elapsed = time.time() - job["start_time"]
    file_path = job["file"]
    chat_id = job["chat_id"]
    web_thumb_path = job.get("web_thumb_path")

    if job.get("monitor_task"):
        job["monitor_task"].cancel()
    if job.get("timer_task"):
        job["timer_task"].cancel()

    await safe_edit_message(chat_id, job["status_msg_id"],
                            f"⚪ **RECORDING STOPPED**\n📌 `{esc(job_name)}`\n"
                            f"⏱ `{system_stats.format_duration_human(elapsed)}`\n🔍 Checking file...",
                            is_photo=job.get("is_photo", False))
    database.update_job_status(job_name, "uploading")

    failed_reason = None
    if not os.path.exists(file_path):
        failed_reason = "file not found"
    elif os.path.getsize(file_path) < 1024:
        failed_reason = "0 bytes (stream likely ended immediately)"

    if failed_reason:
        log_tail = ""
        if os.path.exists(job.get("ffmpeg_log_path", "")):
            try:
                with open(job["ffmpeg_log_path"], "r", errors="ignore") as f:
                    log_tail = f.read()[-1500:]
            except Exception:
                pass
        hint = ""
        if log_tail and ("media.mp4" in log_tail or "MOUFLON" in log_tail):
            hint = "\n🔒 **Protected playlist (MOUFLON) detected.** Public stream nahi hai — private/ticket show ke liye MOUFLON_KEYS chahiye hoti hain."
        keep_note = f"\n🧾 Log: `{job.get('ffmpeg_log_path')}`" if KEEP_FAILED_LOGS else ""
        await safe_send_text(
            chat_id,
            f"❌ **Recording Failed:** `{esc(job_name)}` — {failed_reason} (rc={returncode}){hint}"
            f"\n🪵 **Log tail:**\n`{esc(log_tail[-600:]) if log_tail else 'empty'}`{keep_note}")
        active_jobs.pop(job_name, None)
        database.remove_job(job_name)
        recorder.cleanup_job_files(job_name, file_path)
        gc.collect()
        await check_and_start_queued_job()
        return

    size = os.path.getsize(file_path)
    logger.info(f"Recording done {job_name}: {elapsed:.1f}s {size} bytes rc={returncode}")

    # ============ AD GUARD (20s / ~661KB placeholder) ============
    # If a Stripchat recording is ~20s and under ~1.5MB, it is almost surely
    # the MOUFLON AD VOD that must never reach the chat. Discard it.
    is_stripchat = ("stripchat" in (job.get("original_url") or "")
                    or "doppiocdn" in (job.get("stream_url") or ""))
    if is_stripchat and 12 < elapsed < 28 and size < 1_500_000:
        database.bump_stat("ads_blocked", 1)
        log_tail = ""
        if os.path.exists(job.get("ffmpeg_log_path", "")):
            try:
                with open(job["ffmpeg_log_path"], "r", errors="ignore") as f:
                    log_tail = f.read()[-600:]
            except Exception:
                pass
        await safe_send_text(
            chat_id,
            f"🚫 **AD BLOCKED!** `{esc(job_name)}`\n"
            f"Stripchat ne 20-sec AD placeholder bheja tha (`{system_stats.format_size(size)}`) "
            f"— real stream nahi.\n"
            f"➡️ Model **private/offline** hai ya stream rotate ho gayi.\n"
            f"💡 `/watch {esc(job.get('title', job_name))}` laga do — live hote hi auto-record hoga.\n"
            f"🪵 `{esc(log_tail[-300:]) if log_tail else 'no log'}`")
        active_jobs.pop(job_name, None)
        database.remove_job(job_name)
        recorder.cleanup_job_files(job_name, file_path)
        gc.collect()
        await check_and_start_queued_job()
        return

    if elapsed < 15 and size < 10 * 1024 * 1024:
        await safe_send_text(chat_id,
                             f"⚠️ **Short recording:** `{esc(job_name)}` sirf "
                             f"`{system_stats.format_duration_human(elapsed)}` (`{system_stats.format_size(size)}`). "
                             f"Stream band ho gayi hogi.")

    upload_mode = str(database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE)).lower()
    ok = await recorder.split_and_upload(
        get_upload_client(), chat_id, file_path, job_name, elapsed,
        web_thumb_path=web_thumb_path, upload_mode=upload_mode,
        max_size=MAX_TELEGRAM_SIZE, safe_send_text=safe_send_text,
        safe_edit_message=safe_edit_message)

    # stats
    if ok:
        database.bump_stat("recordings_total", 1)
        database.bump_stat("bytes_recorded", size)
        database.set_setting("last_recording_time", str(int(time.time())))

    # notify watcher DB
    if job.get("source") == "watch" and job.get("watch_chat_id"):
        database.update_watch_status(job.get("title", job_name), job["watch_chat_id"],
                                     "public", last_recorded_at=time.time())

    recorder.cleanup_job_files(job_name, file_path)
    active_jobs.pop(job_name, None)
    database.remove_job(job_name)
    gc.collect()
    await check_and_start_queued_job()

    # AUTO-RESTART: stream drop / short recording while model still live
    if (not job.get("duration_limit") and elapsed < 45
            and job.get("source") == "watch"):
        username = job.get("title", job_name)
        used = auto_restart_count.get(job_name, 0)
        if used < MAX_AUTO_RESTARTS and username not in active_jobs:
            await asyncio.sleep(15)
            if username in active_jobs:
                return
            check = await media_utils.resolve_stripchat(username)
            if check.get("url") and username not in active_jobs:
                auto_restart_count[job_name] = used + 1
                logger.info(f"Auto-restart #{used+1} for {username}")
                await safe_send_text(chat_id,
                                     f"🔄 **Stream drop hui thi — model abhi bhi live hai!** Restart #{used+1}...")
                await start_recording_job(chat_id, job_name, f"https://stripchat.com/{username}",
                                          headers=job.get("headers"), quality=job.get("quality"),
                                          source="watch", watch_chat_id=job.get("watch_chat_id"))
            else:
                auto_restart_count.pop(job_name, None)
    elif elapsed >= 45:
        auto_restart_count.pop(job_name, None)


async def check_and_start_queued_job():
    if len(active_jobs) >= MAX_CONCURRENT_JOBS:
        return
    queued = database.pop_queue_job()
    if not queued:
        return
    await safe_send_text(queued["chat_id"], f"▶️ **Slot free — queued `{esc(queued['job_name'])}` start...**")
    await start_recording_job(queued["chat_id"], queued["job_name"], queued["url"],
                              queued["duration_limit"], queued["headers"], queued["quality"])


async def stop_job(job_name: str):
    job = active_jobs.get(job_name)
    if not job:
        return False
    try:
        job["process"].terminate()
    except Exception:
        pass
    return True


# ============================================================
#  24/7 WATCHER LOOP
# ============================================================

async def watcher_tick():
    for w in database.get_watchlist():
        try:
            username = w["username"]
            chat_id = int(w["chat_id"])
            if not w.get("enabled", 1):
                continue
            if username in active_jobs:
                continue  # already recording

            prev = w.get("last_status", "unknown")
            fast = await media_utils.check_stripchat_status(username)
            status = fast.get("status", "unknown")

            if status in ("offline", "not_found"):
                if prev not in ("offline", "not_found", "unknown"):
                    await safe_send_text(chat_id,
                                         f"💤 `{esc(username)}` **OFFLINE ho gayi** ({status}). "
                                         f"Watchlist mein monitor jari hai...")
                database.update_watch_status(username, chat_id, status)
                continue

            # status public/private/unknown -> confirm via yt-dlp (webpage truth)
            resolved = await media_utils.resolve_stripchat(username)
            if resolved.get("error"):
                new_status = resolved.get("status", "private")
                if new_status == "private" and prev != "private":
                    await safe_send_text(
                        chat_id,
                        f"🔒 `{esc(username)}` **PRIVATE / TICKET show mein chali gayi.** "
                        f"Public room kholne par record hoga. (MOUFLON_KEYS ke bina private record nahi hota)")
                elif new_status == "offline" and prev not in ("offline", "unknown"):
                    await safe_send_text(chat_id, f"💤 `{esc(username)}` **OFFLINE ho gayi.**")
                database.update_watch_status(username, chat_id, new_status)
                continue

            # PUBLIC -> record (cooldown check)
            last_rec = w.get("last_recorded_at") or 0
            cooldown = int(database.get_setting("watch_cooldown", WATCH_COOLDOWN))
            if time.time() - last_rec < cooldown:
                database.update_watch_status(username, chat_id, "public")
                continue

            if len(active_jobs) >= MAX_CONCURRENT_JOBS:
                pos = database.add_queue_job({
                    "job_name": username, "url": f"https://stripchat.com/{username}",
                    "chat_id": chat_id, "duration_limit": 0, "headers": {},
                    "quality": str(database.get_setting("watch_quality", "best"))})
                if pos is not None:
                    await safe_send_text(
                        chat_id,
                        f"🔴 **`{esc(username)}` LIVE hai!** Bot busy hai — recording queue (#{pos}) mein daal di.")
                database.update_watch_status(username, chat_id, "public")
                continue

            auto_restart_count.pop(username, None)
            await safe_send_text(
                chat_id,
                f"🔴 **`{esc(username)}` LIVE ho gayi!** Recording auto-start ho rahi hai...\n"
                f"🖼 Preview + video upload ho kar yahin bheji jayegi.")
            ok = await start_recording_job(chat_id, username, f"https://stripchat.com/{username}",
                                           source="watch", watch_chat_id=chat_id,
                                           quality=str(database.get_setting("watch_quality", "best")))
            if ok:
                database.update_watch_status(username, chat_id, "public", last_recorded_at=time.time())
        except Exception as e:
            logger.error(f"watcher_tick error for {w.get('username')}: {e}")
            await asyncio.sleep(1)


async def watcher_loop():
    await asyncio.sleep(30)  # let the bot boot first
    while True:
        try:
            interval = int(database.get_setting("watch_interval", WATCH_INTERVAL))
            interval = max(60, interval)
            await watcher_tick()
        except Exception as e:
            logger.error(f"watcher_loop error: {e}")
        await asyncio.sleep(interval)


# ============================================================
#  KOYEB HEALTH SERVER + REAL MOUFLON PROXY
# ============================================================

async def root_handler(request):
    return web.Response(text=f"Telegram Recorder Bot V11 MOUFLON-ADFIX is running - {len(active_jobs)} active", status=200)


async def health_handler(request):
    uptime = system_stats.format_duration_human(time.time() - _BOOT_TIME)
    return web.Response(text=f"OK - {VERSION} | {len(active_jobs)} active | "
                             f"{len(database.get_queue_jobs())} queued | uptime {uptime} | "
                             f"last_update {time.time()-_last_update_time:.0f}s ago", status=200)


async def _real_mouflon_proxy(request):
    """
    Serves a DECODED live HLS playlist to ffmpeg:
      1. fetch the variant playlist (already contains psch/v2&pkey=...)
      2. if it's the AD placeholder -> 403 (never proxy the ad)
      3. if segments are MOUFLON-encrypted -> decrypt URIs with the pdkey
         from the key pool and rewrite media.mp4 placeholders to real CDN URLs
    """
    import aiohttp
    q = request.rel_url.query
    target = q.get("url", "")
    username = q.get("username", "")
    if not target:
        return web.Response(status=400, text="missing url param")
    headers = {
        "User-Agent": media_utils.DEFAULT_UA,
        "Referer": f"https://stripchat.com/{username}" if username else "https://stripchat.com/",
        "Accept": "*/*",
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(target, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return web.Response(status=502, text=f"upstream http {resp.status}")
                content = await resp.text(errors="ignore")
    except Exception as e:
        return web.Response(status=502, text=f"upstream fetch fail: {str(e)[:200]}")

    if not content or "#EXTM3U" not in content:
        return web.Response(status=502, text="not an m3u8 response")

    # AD placeholder guard — never let the 20s ad reach ffmpeg
    if "#EXT-X-MOUFLON-ADVERT" in content or ("cpa/v2" in content and "#EXT-X-ENDLIST" in content):
        return web.Response(status=403, text="AD placeholder detected - model private/offline")

    if "#EXT-X-MOUFLON:URI:" in content:
        m = re.search(r"[?&]pkey=([^&]+)", target)
        pkey = urllib.parse.unquote(m.group(1)) if m else ""
        keys = media_utils.get_mouflon_keys()
        pdkey = keys.get(pkey, "")
        if not pdkey:
            keys = media_utils.get_mouflon_keys(force=True)
            pdkey = keys.get(pkey, "")
        if not pdkey:
            logger.warning(f"mouflon proxy: no pdkey for pkey={pkey}")
            return web.Response(status=403,
                                text=f"MOUFLON encrypted playlist - pdkey missing for pkey={pkey}. /keys check karo.")
        decoded = media_utils.decode_mouflon_live_playlist(content, pdkey)
        if not decoded or "media.mp4" in decoded:
            return web.Response(status=403, text="decode produced no valid segments (wrong pdkey?)")
        return web.Response(text=decoded, content_type="application/vnd.apple.mpegurl")

    # plain live playlist -> pass through
    return web.Response(text=content, content_type="application/vnd.apple.mpegurl")


async def start_web_server():
    web_app = web.Application()
    web_app.router.add_get("/", root_handler)
    web_app.router.add_get("/health", health_handler)
    web_app.router.add_get("/mouflon_proxy", _real_mouflon_proxy)
    web_app.router.add_get("/hls_proxy", _real_mouflon_proxy)
    web_app.router.add_post("/{tail:.*}", lambda r: web.Response(text="OK", status=200))
    web_app.router.add_get("/{tail:.*}", root_handler)
    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Health server on 0.0.0.0:{PORT} (mouflon_proxy ACTIVE)")
    while True:
        await asyncio.sleep(3600)


async def delete_old_webhook():
    import aiohttp
    for _ in range(3):
        try:
            async with aiohttp.ClientSession() as sess:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    txt = await resp.text()
                    if '"ok":true' in txt.lower():
                        logger.info("deleteWebhook OK")
                        return
        except Exception as e:
            logger.warning(f"deleteWebhook fail: {e}")
        await asyncio.sleep(2)


# ============================================================
#  TELEGRAM HANDLERS
# ============================================================

# ============================================================
#  GLOBAL INCOMING-MESSAGE LOGGER (diagnostic - sabse pehle run)
#  Koyeb logs mein "[INCOMING]" dikhega toh bot updates receive
#  kar raha hai. Nahin dikhega = updates delivery problem.
# ============================================================

@app.on_message(filters.all, group=0)
async def debug_log_all(client, message: Message):
    global _last_update_time
    _last_update_time = time.time()
    try:
        txt = (message.text or message.caption or "")[:100]
        uid = message.from_user.id if message.from_user else "?"
        uname = message.from_user.username if message.from_user and message.from_user.username else ""
        chat = message.chat.id if message.chat else "?"
        ctype = message.chat.type if message.chat else "?"
        logger.info(f"[INCOMING] uid={uid} @{uname} chat={chat} type={ctype} text={txt!r}")
    except Exception:
        pass


async def update_watchdog():
    """Agar 5 min se koi update nahi aaya -> polling dead/conflicted."""
    while True:
        await asyncio.sleep(120)
        idle_secs = time.time() - _last_update_time
        if idle_secs > 300:
            logger.warning(f"⚠️ NO INCOMING UPDATES for {idle_secs:.0f}s - "
                           f"koi aur instance/webhook same bot token use kar raha hai?")


@app.on_message(filters.command(["start", "help"]))
async def start_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id,
                             f"❌ **Access Denied.** Contact owner `{OWNER_ID}`.")
        return
    keys_count = len(media_utils.get_mouflon_keys())
    text = (
        "🔥 **SUPER ADVANCED RECORDER BOT V11 (AD-FIX)**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "**🎥 Record:**\n"
        "`/record <name> <url> [duration]`\n"
        "→ `/record cute https://stripchatgirls.com/Cute_Kritika69 30m`\n"
        "→ URL paste karo direct — auto record\n\n"
        "**🤖 24/7 Auto-Watchlist:**\n"
        "`/watch <link or username>` — live hote hi auto-record + send\n"
        "`/unwatch <username>` · `/watchlist` · `/watchinterval 120`\n\n"
        "**⚙️ Control:**\n"
        "`/stop <name>` · `/status` · `/queue` · `/stats`\n"
        "`/check <username>` — abhi online hai ya nahi\n"
        "`/keys` · `/resynckeys` — MOUFLON keys status\n"
        "`/mode video|document` · `/clean`\n\n"
        f"⚙️ Mode: `{str(database.get_setting('upload_mode', DEFAULT_UPLOAD_MODE)).upper()}` | "
        f"Max jobs: `{MAX_CONCURRENT_JOBS}` | Watch: `{str(database.get_setting('watch_interval', WATCH_INTERVAL))}s`\n"
        f"🔑 Keys: `{keys_count}` — AD-proof recording {'ON ✅' if keys_count else 'OFF ⚠️ /resynckeys'} "
    )
    await safe_send_text(message.chat.id, text)


@app.on_message(filters.command("record"))
async def record_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    job_name, url, duration_limit, headers, quality = media_utils.parse_record_command(message.text)
    if not job_name or not url:
        await safe_send_text(message.chat.id,
                             "❌ **Format:** `/record <name> <url> [duration]`\n"
                             "Example: `/record kritika https://stripchatgirls.com/Cute_Kritika69 1h`")
        return
    if not re.match(r"^[a-zA-Z0-9_-]{1,35}$", job_name):
        await safe_send_text(message.chat.id, "❌ Name mein sirf letters, numbers, _ , - allowed hain.")
        return
    if job_name in active_jobs:
        await safe_send_text(message.chat.id, f"❌ `{esc(job_name)}` already active hai.")
        return
    if len(active_jobs) >= MAX_CONCURRENT_JOBS:
        pos = database.add_queue_job({"job_name": job_name, "url": url, "chat_id": message.chat.id,
                                      "duration_limit": duration_limit, "headers": headers,
                                      "quality": quality})
        if pos is None:
            await safe_send_text(message.chat.id, f"❌ `{esc(job_name)}` already queue mein hai.")
        else:
            await safe_send_text(message.chat.id,
                                 f"⏳ **Busy** — `{esc(job_name)}` queue #{pos} pe laga diya.")
        return
    ack = await safe_send_text(message.chat.id,
                               f"✅ **Recording request mila!**\n📌 `{esc(job_name)}`\n🔍 Stream resolve ho raha hai...")
    try:
        ok = await start_recording_job(message.chat.id, job_name, url, duration_limit, headers, quality)
        if not ok and ack:
            await safe_edit_message(message.chat.id, ack.id,
                                    f"❌ Recording start nahi ho payi `{esc(job_name)}`. Error upar dekho.")
    except Exception as e:
        logger.error(f"record_cmd crash: {e}", exc_info=True)
        if ack:
            try:
                await safe_edit_message(message.chat.id, ack.id,
                                        f"⚠️ **Internal error:** `{esc(str(e)[:200])}`")
            except Exception:
                pass


@app.on_message(filters.text & ~filters.command(["start", "help", "record", "watch", "unwatch",
                                                 "watchlist", "watchinterval", "check", "stop",
                                                 "status", "queue", "stats", "server", "mode",
                                                 "addsudo", "rmsudo", "sudolist", "clean",
                                                 "keys", "resynckeys", "ping"]))
async def auto_url_handler(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id,
                             f"❌ **Access Denied.** Tumhe authorize nahi kiya gaya. Contact owner `{OWNER_ID}`.")
        return
    urls = media_utils.extract_urls_from_text(message.text)
    if not urls:
        return
    url = media_utils.normalize_stream_url(urls[0])

    # INSTANT ACK - user ko turant pata chale ki bot ne message dekha
    ack = await safe_send_text(
        message.chat.id,
        f"✅ **Link mil gaya!**\n🔗 `{esc(url)}`\n🔍 Stream resolve ho raha hai, thodi der...")
    try:
        job_name, _, duration_limit, headers, quality = media_utils.parse_record_command(f"/record {message.text}")
        if not job_name:
            if ack:
                await safe_edit_message(message.chat.id, ack.id,
                                        f"❌ Is link se koi valid stream nahi mila.\n`{esc(url)}`")
            return
        if job_name in active_jobs:
            if ack:
                await safe_edit_message(message.chat.id, ack.id,
                                        f"❌ Duplicate: `{esc(job_name)}` already recording hai.")
            return
        if len(active_jobs) >= MAX_CONCURRENT_JOBS:
            pos = database.add_queue_job({"job_name": job_name, "url": url, "chat_id": message.chat.id,
                                          "duration_limit": duration_limit, "headers": headers,
                                          "quality": quality})
            if ack:
                if pos is not None:
                    await safe_edit_message(message.chat.id, ack.id,
                                            f"⏳ **Busy** — `{esc(job_name)}` queue #{pos} pe laga diya.")
                else:
                    await safe_edit_message(message.chat.id, ack.id,
                                            f"❌ `{esc(job_name)}` already queue mein hai.")
            return
        ok = await start_recording_job(message.chat.id, job_name, url, duration_limit, headers, quality)
        if not ok and ack:
            await safe_edit_message(message.chat.id, ack.id,
                                    f"❌ Recording start nahi ho payi `{esc(job_name)}`. "
                                    f"Upar wala error dekho ya `/check {esc(job_name)}` try karo.")
    except Exception as e:
        logger.error(f"auto_url_handler crash: {e}", exc_info=True)
        if ack:
            try:
                await safe_edit_message(message.chat.id, ack.id,
                                        f"⚠️ **Internal error:** `{esc(str(e)[:200])}`\nLog check karo.")
            except Exception:
                pass


@app.on_message(filters.command("watch"))
async def watch_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    arg = message.text.split(" ", 1)[1].strip() if len(message.command) > 1 else ""
    urls = media_utils.extract_urls_from_text(arg)
    if urls:
        url = media_utils.normalize_stream_url(urls[0])
        username = media_utils.extract_username_from_url(url)
    else:
        username = arg.strip().lstrip("@").strip()
        if not username:
            await safe_send_text(message.chat.id, "❌ Usage: `/watch <link ya username>`")
            return
        url = f"https://stripchat.com/{username}"
    username = re.sub(r"[^a-zA-Z0-9_@-]", "", username)[:35]
    if not username:
        await safe_send_text(message.chat.id, "❌ Invalid username.")
        return

    ok, msg = database.add_watch(url, username, message.chat.id, message.from_user.id)
    await safe_send_text(message.chat.id, msg)
    if not ok:
        return

    # immediate status check
    status_msg = await safe_send_text(message.chat.id,
                                      f"🔍 `{esc(username)}` ka status check ho raha hai...")
    resolved = await media_utils.resolve_stripchat(username)
    if resolved.get("url"):
        txt = (f"✅ **LIVE HAI!** `{esc(username)}` abhi public room mein online hai.\n"
               f"🔴 Recording shuru kar raha hoon...")
        if status_msg:
            await safe_edit_message(message.chat.id, status_msg.id, txt)
        database.update_watch_status(username, message.chat.id, "public")
        await start_recording_job(message.chat.id, username, url, source="watch",
                                  watch_chat_id=message.chat.id)
    elif resolved.get("status") == "private":
        txt = f"🔒 `{esc(username)}` abhi **PRIVATE/TICKET show** mein hai.\n⏳ Public room kholte hi auto-record hoga. Watchlist mein hai ✅"
        if status_msg:
            await safe_edit_message(message.chat.id, status_msg.id, txt)
        database.update_watch_status(username, message.chat.id, "private")
    elif resolved.get("status") == "offline":
        txt = f"💤 `{esc(username)}` abhi **OFFLINE** hai.\n⏳ Live hote hi auto-record hoga. Watchlist mein hai ✅"
        if status_msg:
            await safe_edit_message(message.chat.id, status_msg.id, txt)
        database.update_watch_status(username, message.chat.id, "offline")
    else:
        txt = f"⚠️ `{esc(username)}` — {resolved.get('error', 'status unknown')}\nWatchlist mein hai ✅"
        if status_msg:
            await safe_edit_message(message.chat.id, status_msg.id, txt)


@app.on_message(filters.command("unwatch"))
async def unwatch_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    if len(message.command) < 2:
        await safe_send_text(message.chat.id, "❌ Usage: `/unwatch <username>`")
        return
    username = message.command[1].strip().lstrip("@")
    if database.remove_watch(username, message.chat.id):
        await safe_send_text(message.chat.id, f"🗑 `{esc(username)}` watchlist se hata diya.")
    else:
        await safe_send_text(message.chat.id, f"❌ `{esc(username)}` watchlist mein nahi hai.")


@app.on_message(filters.command("watchlist"))
async def watchlist_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    watches = database.get_watches_for_chat(message.chat.id)
    if not watches:
        await safe_send_text(message.chat.id,
                             "📭 **Watchlist khali hai.**\n`/watch <link>` se models add karo — live hote hi auto-record hoga!")
        return
    lines = ["📋 **YOUR WATCHLIST:**", "━━━━━━━━━━━━━━━━━━━━"]
    for i, w in enumerate(watches, 1):
        status_icon = {"public": "🔴 LIVE", "private": "🔒 Private", "offline": "💤 Offline",
                       "not_found": "❌ 404", "unknown": "❔"}.get(w.get("last_status", "unknown"), "❔")
        active = " (recording...)" if w["username"] in active_jobs else ""
        enabled = "" if w.get("enabled", 1) else " ⏸"
        lines.append(f"{i}. `{esc(w['username'])}` — {status_icon}{enabled}{active}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("Buttons se pause/resume ya remove karo, ya `/unwatch <username>`")
    await safe_send_text(message.chat.id, "\n".join(lines))

    # per-watch buttons (one message per watch, compact)
    for w in watches[:10]:
        try:
            await app.send_message(
                message.chat.id,
                f"`{esc(w['username'])}` — {w.get('last_status', 'unknown')}",
                reply_markup=watch_buttons(w["username"], bool(w.get("enabled", 1))))
        except Exception:
            pass


@app.on_message(filters.command("watchinterval"))
async def watchinterval_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    if len(message.command) < 2 or not message.command[1].isdigit():
        cur = database.get_setting("watch_interval", WATCH_INTERVAL)
        await safe_send_text(message.chat.id, f"⏱ Current interval: `{cur}s`\nUsage: `/watchinterval 120` (min 60)")
        return
    val = max(60, int(message.command[1]))
    database.set_setting("watch_interval", val)
    await safe_send_text(message.chat.id, f"✅ Watch interval -> `{val}s`")


@app.on_message(filters.command("check"))
async def check_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    arg = message.text.split(" ", 1)[1].strip() if len(message.command) > 1 else ""
    urls = media_utils.extract_urls_from_text(arg)
    if urls:
        url = media_utils.normalize_stream_url(urls[0])
        username = media_utils.extract_username_from_url(url) or url
    else:
        username = arg.strip().lstrip("@")
        url = f"https://stripchat.com/{username}"
    if not username:
        await safe_send_text(message.chat.id, "❌ Usage: `/check <username ya link>`")
        return

    status_msg = await safe_send_text(message.chat.id, f"🔍 `{esc(username)}` check ho raha hai...")
    resolved = await media_utils.resolve_stripchat(username)
    thumb = resolved.get("thumb_path")
    if resolved.get("url"):
        txt = (f"🔴 **`{esc(username)}` — LIVE (public room)**\n"
               f"✅ Recording possible hai. `/record {esc(username)} {esc(url)}` ya `/watch {esc(username)}` use karo.")
        if thumb and os.path.exists(thumb):
            try:
                await app.send_photo(message.chat.id, photo=thumb, caption=txt)
                if status_msg:
                    await status_msg.delete()
                return
            except Exception:
                pass
        if status_msg:
            await safe_edit_message(message.chat.id, status_msg.id, txt)
    else:
        txt = f"📊 **`{esc(username)}`**: {resolved.get('error', 'unknown')}"
        if thumb and os.path.exists(thumb):
            try:
                await app.send_photo(message.chat.id, photo=thumb, caption=txt)
                if status_msg:
                    await status_msg.delete()
                return
            except Exception:
                pass
        if status_msg:
            await safe_edit_message(message.chat.id, status_msg.id, txt)


@app.on_message(filters.command("stop"))
async def stop_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    if len(message.command) < 2:
        await safe_send_text(message.chat.id, "❌ Usage: `/stop <job_name>`")
        return
    name = message.command[1]
    if name not in active_jobs:
        await safe_send_text(message.chat.id, f"❌ `{esc(name)}` active nahi hai.")
        return
    await stop_job(name)
    await safe_send_text(message.chat.id, f"🛑 `{esc(name)}` stop ho raha hai...")


@app.on_message(filters.command("status"))
async def status_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    if not active_jobs:
        await safe_send_text(message.chat.id, "📭 **Koi recording active nahi.**")
        return
    text = "📊 **ACTIVE RECORDINGS:**\n"
    for name, job in active_jobs.items():
        elapsed = time.time() - job["start_time"]
        try:
            size = os.path.getsize(job["file"]) if os.path.exists(job["file"]) else 0
        except Exception:
            size = 0
        text += f"🔴 `{esc(name)}` — {system_stats.format_duration_human(elapsed)} ({system_stats.format_size(size)})\n"
    await safe_send_text(message.chat.id, text)


@app.on_message(filters.command("queue"))
async def queue_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    queued = database.get_queue_jobs()
    if not queued:
        await safe_send_text(message.chat.id, "📭 **Queue khali hai.**")
        return
    text = "⏳ **PENDING QUEUE:**\n"
    for i, job in enumerate(queued, 1):
        text += f"#{i} — `{esc(job['job_name'])}`\n"
    await safe_send_text(message.chat.id, text)


@app.on_message(filters.command(["stats", "server"]))
async def stats_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE)
    text = system_stats.get_system_stats_text(len(active_jobs), len(database.get_queue_jobs()),
                                              upload_mode, IS_PREMIUM_SESSION, MAX_CONCURRENT_JOBS)
    total_recs = database.get_stat("recordings_total")
    total_bytes = database.get_stat("bytes_recorded")
    text += (f"━━━━━━━━━━━━━━━━━━━━\n"
             f"🎬 **Total Recordings:** `{int(total_recs)}`\n"
             f"💽 **Total Data:** `{system_stats.format_size(total_bytes)}`\n"
             f"👁 **Watched Models:** `{len(database.get_watchlist())}`")
    await safe_send_text(message.chat.id, text, reply_markup=stats_buttons())


@app.on_message(filters.command("mode"))
async def mode_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    if len(message.command) < 2 or message.command[1].lower() not in ("video", "document"):
        cur = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE).upper()
        await safe_send_text(message.chat.id, f"⚙️ Current: `{cur}` | Use: `/mode video` ya `/mode document`")
        return
    database.set_setting("upload_mode", message.command[1].lower())
    await safe_send_text(message.chat.id, f"✅ Upload mode -> `{message.command[1].upper()}`")


@app.on_message(filters.command("clean"))
async def clean_cmd(client, message: Message):
    if message.from_user.id != OWNER_ID and OWNER_ID != 0:
        await safe_send_text(message.chat.id, "❌ Only owner.")
        return
    freed = 0
    for d in (recorder.RECORDINGS_DIR, recorder.SPLITS_DIR):
        try:
            for f in os.listdir(d):
                p = os.path.join(d, f)
                try:
                    freed += os.path.getsize(p)
                    os.remove(p)
                except Exception:
                    pass
        except Exception:
            pass
    await safe_send_text(message.chat.id, f"🧹 Cleanup done — `{system_stats.format_size(freed)}` free hua.")


@app.on_message(filters.command("ping"))
async def ping_cmd(client, message: Message):
    """Quick alive check - har user ke liye (auth nahi chahiye)."""
    try:
        me = await app.get_me()
        uptime = time.time() - _BOOT_TIME
        idle_secs = time.time() - _last_update_time
        await safe_send_text(
            message.chat.id,
            f"🏓 **PONG! Bot alive hai** ✅\n"
            f"🤖 @{me.username}\n"
            f"⏱ Uptime: `{system_stats.format_duration_human(uptime)}`\n"
            f"📥 Last update: `{idle_secs:.0f}s` pehle\n"
            f"🔴 Active jobs: `{len(active_jobs)}` | ⏳ Queue: `{len(database.get_queue_jobs())}`\n"
            f"🔑 Keys: `{len(media_utils.get_mouflon_keys())}`\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"💡 Agar link pe respond nahi karta:\n"
            f"1️⃣ Group mein ho? BotFather → `/setprivacy` → **Disable** karo\n"
            f"2️⃣ DM mein bhejo\n"
            f"3️⃣ Koyeb logs mein `[INCOMING]` check karo")
    except Exception as e:
        logger.error(f"ping error: {e}")
        try:
            await safe_send_text(message.chat.id, f"⚠️ Ping fail: `{esc(str(e)[:150])}`")
        except Exception:
            pass


@app.on_message(filters.command("keys"))
async def keys_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    keys = media_utils.get_mouflon_keys()
    total = len(keys)
    sync_url = media_utils.MOUFLON_SYNC_URL
    text = (
        f"🔑 **MOUFLON KEYS STATUS**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 Loaded keys: `{total}`\n"
        f"🌐 Sync source: `{esc(sync_url)}`\n"
        f"⏱ TTL: `{media_utils.KEY_SYNC_TTL}s`\n\n"
        f"{'✅ AD-proof recording active — real video milega!' if total else '⚠️ Koi key nahi — encrypted streams fail honge!'}\n\n"
        f"`/resynckeys` se force refresh karo."
    )
    await safe_send_text(message.chat.id, text, reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Force Re-Sync", callback_data="resync_keys")]]))


@app.on_message(filters.command("resynckeys"))
async def resynckeys_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    msg = await safe_send_text(message.chat.id, "🔄 Keys re-sync ho rahi hain...")
    keys = media_utils.get_mouflon_keys(force=True)
    if msg:
        await safe_edit_message(message.chat.id, msg.id,
                                f"✅ Re-sync done! `{len(keys)}` keys loaded.")
    else:
        await safe_send_text(message.chat.id, f"✅ Re-sync done! `{len(keys)}` keys loaded.")


@app.on_message(filters.command("addsudo"))
async def addsudo_cmd(client, message: Message):
    if message.from_user.id != OWNER_ID and OWNER_ID != 0:
        await safe_send_text(message.chat.id, "❌ Only owner.")
        return
    if len(message.command) < 2 or not message.command[1].isdigit():
        await safe_send_text(message.chat.id, "❌ Usage: `/addsudo <user_id>`")
        return
    target = int(message.command[1])
    if database.add_sudo(target, message.from_user.id):
        await safe_send_text(message.chat.id, f"✅ User `{target}` authorized.")
    else:
        await safe_send_text(message.chat.id, "❌ Failed.")


@app.on_message(filters.command("rmsudo"))
async def rmsudo_cmd(client, message: Message):
    if message.from_user.id != OWNER_ID and OWNER_ID != 0:
        await safe_send_text(message.chat.id, "❌ Only owner.")
        return
    if len(message.command) < 2 or not message.command[1].isdigit():
        await safe_send_text(message.chat.id, "❌ Usage: `/rmsudo <user_id>`")
        return
    target = int(message.command[1])
    if database.remove_sudo(target):
        await safe_send_text(message.chat.id, f"✅ User `{target}` removed.")
    else:
        await safe_send_text(message.chat.id, "❌ Not found.")


@app.on_message(filters.command("sudolist"))
async def sudolist_cmd(client, message: Message):
    if not check_auth(message.from_user.id):
        await safe_send_text(message.chat.id, "❌ Access Denied.")
        return
    sudos = set(database.get_sudo_users()) | set(ENV_SUDO_USERS)
    if OWNER_ID != 0:
        sudos.add(OWNER_ID)
    text = "🔐 **AUTHORIZED USERS:**\n"
    for uid in sorted(sudos):
        role = "👑 Owner" if uid == OWNER_ID else "👤 Sudo"
        text += f"{role}: `{uid}`\n"
    await safe_send_text(message.chat.id, text)


@app.on_callback_query()
async def on_callback(client, callback_query: CallbackQuery):
    if not check_auth(callback_query.from_user.id):
        await callback_query.answer("❌ Not authorized.", show_alert=True)
        return
    data = callback_query.data or ""
    try:
        if data.startswith("stop:"):
            name = data.split(":", 1)[1]
            if name in active_jobs:
                await callback_query.answer(f"🛑 Stopping {name}...")
                await stop_job(name)
            else:
                await callback_query.answer("❌ Not active.", show_alert=True)

        elif data.startswith("status:"):
            name = data.split(":", 1)[1]
            job = active_jobs.get(name)
            if job:
                elapsed = time.time() - job["start_time"]
                try:
                    size = os.path.getsize(job["file"]) if os.path.exists(job["file"]) else 0
                except Exception:
                    size = 0
                text = (f"🔴 **RECORDING**\n📌 `{esc(name)}`\n"
                        f"⏱ `{system_stats.format_duration_human(elapsed)}`\n"
                        f"💾 `{system_stats.format_size(size)}`")
                await safe_edit_message(job["chat_id"], job["status_msg_id"], text,
                                        reply_markup=job_control_buttons(name),
                                        is_photo=job.get("is_photo", False))
                await callback_query.answer("📊 Refreshed!")
            else:
                await callback_query.answer("📭 Not recording.", show_alert=True)

        elif data.startswith("cancel:"):
            name = data.split(":", 1)[1]
            job = active_jobs.get(name)
            if job:
                try:
                    job["process"].terminate()
                except Exception:
                    pass
                if job.get("monitor_task"):
                    job["monitor_task"].cancel()
                if job.get("timer_task"):
                    job["timer_task"].cancel()
                recorder.cleanup_job_files(name, job["file"])
                active_jobs.pop(name, None)
                database.remove_job(name)
                gc.collect()
                await safe_edit_message(job["chat_id"], job["status_msg_id"],
                                        f"🗑 **CANCELLED** `{esc(name)}`",
                                        is_photo=job.get("is_photo", False))
                await callback_query.answer("🗑 Cancelled!")
                await check_and_start_queued_job()
            elif database.remove_queue_job(name):
                await callback_query.answer(f"🗑 Queued {name} removed!", show_alert=True)
            else:
                await callback_query.answer("❌ Not found.", show_alert=True)

        elif data == "refresh_stats":
            upload_mode = database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE)
            text = system_stats.get_system_stats_text(len(active_jobs), len(database.get_queue_jobs()),
                                                      upload_mode, IS_PREMIUM_SESSION, MAX_CONCURRENT_JOBS)
            total_recs = database.get_stat("recordings_total")
            total_bytes = database.get_stat("bytes_recorded")
            text += (f"━━━━━━━━━━━━━━━━━━━━\n"
                     f"🎬 **Total Recordings:** `{int(total_recs)}`\n"
                     f"💽 **Total Data:** `{system_stats.format_size(total_bytes)}`\n"
                     f"👁 **Watched Models:** `{len(database.get_watchlist())}`")
            await safe_edit_message(callback_query.message.chat.id, callback_query.message.id,
                                    text, reply_markup=stats_buttons())
            await callback_query.answer("🔄 Updated!")

        elif data == "resync_keys":
            keys = media_utils.get_mouflon_keys(force=True)
            await callback_query.message.edit_text(
                f"✅ Keys re-synced! `{len(keys)}` keys loaded.",
                parse_mode=enums.ParseMode.MARKDOWN)
            await callback_query.answer("🔄 Done!")

        elif data.startswith("wtog:"):
            username = data.split(":", 1)[1]
            w = next((x for x in database.get_watches_for_chat(callback_query.message.chat.id)
                      if x["username"] == username), None)
            if w:
                new_state = not bool(w.get("enabled", 1))
                database.set_watch_enabled(username, callback_query.message.chat.id, new_state)
                await callback_query.message.edit_reply_markup(watch_buttons(username, new_state))
                await callback_query.answer("⏸ Paused" if not new_state else "▶️ Resumed")
            else:
                await callback_query.answer("❌ Not found.", show_alert=True)

        elif data.startswith("wdel:"):
            username = data.split(":", 1)[1]
            if database.remove_watch(username, callback_query.message.chat.id):
                try:
                    await callback_query.message.delete()
                except Exception:
                    pass
                await callback_query.answer("🗑 Removed!")
            else:
                await callback_query.answer("❌ Not found.", show_alert=True)
    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            await callback_query.answer("⚠️ Error.", show_alert=True)
        except Exception:
            pass


# ============================================================
#  RECOVERY + MAIN
# ============================================================

async def recover_interrupted_jobs():
    interrupted = database.get_all_active_jobs()
    if not interrupted:
        return
    logger.info(f"{len(interrupted)} saved jobs - recovering...")
    for job in interrupted:
        file_path = job.get("file_path", "")
        if os.path.exists(file_path) and os.path.getsize(file_path) > 1024:
            await safe_send_text(job["chat_id"], f"🔄 **Recovery:** `{esc(job['job_name'])}` file mili, upload ho rahi hai...")
            try:
                await recorder.split_and_upload(
                    get_upload_client(), job["chat_id"], file_path, job["job_name"], 0,
                    upload_mode=str(database.get_setting("upload_mode", DEFAULT_UPLOAD_MODE)).lower(),
                    max_size=MAX_TELEGRAM_SIZE, safe_send_text=safe_send_text,
                    safe_edit_message=safe_edit_message)
            except Exception as e:
                logger.error(f"Recovery upload fail {job['job_name']}: {e}")
            recorder.cleanup_job_files(job["job_name"], file_path)
        database.remove_job(job["job_name"])


async def main():
    recorder.cleanup_old_files(max_age_hours=24)
    web_task = asyncio.create_task(start_web_server())
    await delete_old_webhook()
    await app.start()
    if user_app and IS_PREMIUM_SESSION:
        try:
            await user_app.start()
            logger.info("💎 Premium userbot started (4GB uploads)")
        except Exception as e:
            logger.error(f"Userbot start failed: {e}")

    # boot notification -> owner ko pata chale bot online hai
    try:
        me = await app.get_me()
        logger.info(f"Bot @{me.username} online")
        # webhook status check - agar koi webhook set hai toh polling updates nahi lega
        try:
            import aiohttp
            async with aiohttp.ClientSession() as sess:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo"
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    info = await resp.json()
                    result = info.get("result", {})
                    logger.info(f"getWebhookInfo: url={result.get('url')!r} "
                                f"pending={result.get('pending_update_count')} "
                                f"last_error={str(result.get('last_error_message'))[:80]}")
        except Exception as e:
            logger.warning(f"getWebhookInfo fail: {e}")
        if OWNER_ID:
            keys_count = len(media_utils.get_mouflon_keys())
            await safe_send_text(
                OWNER_ID,
                f"✅ **BOT ONLINE** (@{me.username})\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔑 MOUFLON keys: `{keys_count}`\n"
                f"⚙️ Max jobs: `{MAX_CONCURRENT_JOBS}` | Watch: `{str(database.get_setting('watch_interval', WATCH_INTERVAL))}s`\n"
                f"🚀 Ready! Link bhejo ya `/watch <link>` karo.")
    except Exception as e:
        logger.warning(f"Boot notification failed (owner check): {e}")

    watch_task = asyncio.create_task(watcher_loop())
    asyncio.create_task(update_watchdog())  # diagnostics: no-update alert
    logger.info(f"Bot started - PORT {PORT} | max jobs {MAX_CONCURRENT_JOBS} | "
                f"watch interval {database.get_setting('watch_interval', WATCH_INTERVAL)}s")
    await recover_interrupted_jobs()
    await idle()
    watch_task.cancel()
    await app.stop()
    if user_app and IS_PREMIUM_SESSION:
        try:
            await user_app.stop()
        except Exception:
            pass
    web_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
