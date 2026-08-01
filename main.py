"""
V4 FINAL - Koyeb + Github Ready
- Port 8080 health check server (Koyeb ke liye must)
- API_ID/HASH se No 50MB limit (2GB/4GB tak)
- >1.9GB auto split
- FloodWait protection (bot hang nahi hoga)
- Docker + Koyeb ready

ONLY FOR YOUR OWN CONTENT OR WITH WRITTEN CONSENT
"""

import os
import re
import asyncio
import time
import logging
from datetime import datetime
from dotenv import load_dotenv

from pyrogram import Client, filters, idle
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from aiohttp import web

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "8080"))  # Koyeb 8080 pe check karta hai

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise ValueError("API_ID, API_HASH, BOT_TOKEN .env me daal - my.telegram.org se")

RECORDINGS_DIR = "recordings"
SPLITS_DIR = "splits"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SPLITS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

active_jobs = {}
MAX_TELEGRAM_SIZE = 1900 * 1024 * 1024  # 1.9GB safe

def format_size(b):
    if b < 1024: return f"{b} B"
    if b < 1024*1024: return f"{b/1024:.1f} KB"
    if b < 1024*1024*1024: return f"{b/(1024*1024):.2f} MB"
    return f"{b/(1024*1024*1024):.2f} GB"

def format_duration(s):
    h = int(s // 3600); m = int((s % 3600) // 60); sec = int(s % 60)
    return f"{h}h {m}m {sec}s" if h>0 else f"{m}m {sec}s"

def is_direct_media_link(url: str):
    url_lower = url.lower()
    if any(x in url_lower for x in [".m3u8", ".mp4", ".m4a", ".ts", ".mpd", "master", "playlist", "chunk", "hls", "live"]):
        return True
    if url_lower.startswith("rtmp://") or url_lower.startswith("srt://"):
        return True
    return False

# ---------- FLOODWAIT SAFE WRAPPERS ----------
async def safe_edit_text(chat_id, msg_id, text):
    for attempt in range(5):
        try:
            await app.edit_message_text(chat_id, msg_id, text)
            return True
        except FloodWait as e:
            logger.warning(f"FloodWait edit {e.value}s - waiting")
            await asyncio.sleep(e.value + 1)
        except Exception as ex:
            logger.error(f"Edit fail: {ex}")
            # agar message same hai to error aata hai, ignore
            if "MESSAGE_NOT_MODIFIED" in str(ex):
                return True
            return False
    return False

async def safe_send_text(chat_id, text):
    for attempt in range(5):
        try:
            return await app.send_message(chat_id, text)
        except FloodWait as e:
            logger.warning(f"FloodWait send {e.value}s")
            await asyncio.sleep(e.value + 1)
        except Exception as ex:
            logger.error(f"Send fail: {ex}")
            await asyncio.sleep(1)
    return None

# Pyrogram Client
app = Client(
    "recorder_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir="."
)

# ---------- HEALTH SERVER FOR KOYEB (PORT 8080) ----------
async def health_handler(request):
    active = len(active_jobs)
    return web.Response(text=f"Bot Alive - {active} active recordings", status=200)

async def root_handler(request):
    return web.Response(text="Telegram Recorder Bot V4 is Running - Use /record in Telegram", status=200)

async def start_web_server():
    web_app = web.Application()
    web_app.router.add_get("/", root_handler)
    web_app.router.add_get("/health", health_handler)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Health server started on 0.0.0.0:{PORT}")
    # Keep running
    while True:
        await asyncio.sleep(3600)

# ---------- RECORDING LOGIC ----------
async def monitor_recording(job_name):
    while job_name in active_jobs:
        job = active_jobs[job_name]
        elapsed = time.time() - job['start_time']
        try:
            size = os.path.getsize(job['file']) if os.path.exists(job['file']) else 0
        except:
            size = 0
        
        bps = f"| {format_size(size/elapsed)}/s" if elapsed>10 and size>0 else ""
        text = (
            f"🔴 RECORDING: {job_name}\n"
            f"⏱ {format_duration(elapsed)} {bps}\n"
            f"💾 {format_size(size)} (No limit)\n"
            f"📁 {os.path.basename(job['file'])}\n"
            f"Stop: /stop {job_name}"
        )
        await safe_edit_text(job['chat_id'], job['status_msg_id'], text)
        await asyncio.sleep(10)

async def upload_with_progress(chat_id, file_path, caption, progress_msg=None):
    total = os.path.getsize(file_path)
    last_update = [0]
    last_percent = [-1]

    async def progress_cb(current, total_size):
        now = time.time()
        percent = int(current * 100 / total_size)
        if now - last_update[0] < 2 and percent == last_percent[0]:
            return
        last_update[0] = now
        last_percent[0] = percent
        bar_len = 12
        filled = int(bar_len * current / total_size)
        bar = "█" * filled + "░" * (bar_len - filled)
        txt = (
            f"📤 {os.path.basename(file_path)}\n"
            f"{bar} {percent}%\n"
            f"{format_size(current)} / {format_size(total_size)}"
        )
        if progress_msg:
            try:
                await progress_msg.edit_text(txt)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except:
                pass

    for attempt in range(3):
        try:
            await app.send_document(
                chat_id=chat_id,
                document=file_path,
                caption=caption,
                progress=progress_cb
            )
            return True
        except FloodWait as e:
            logger.warning(f"FloodWait upload {e.value}s")
            await asyncio.sleep(e.value + 2)
        except Exception as e:
            logger.error(f"Upload error {e}")
            await asyncio.sleep(2)
    return False

async def split_and_upload(chat_id, file_path, job_name, elapsed):
    size = os.path.getsize(file_path)
    
    if size <= MAX_TELEGRAM_SIZE:
        prog = await safe_send_text(chat_id, f"📤 Preparing {job_name}... {format_size(size)}")
        if prog:
            await upload_with_progress(chat_id, file_path,
                f"✅ {job_name}\nDuration: {format_duration(elapsed)}\nSize: {format_size(size)}", prog)
            try:
                await prog.delete()
            except:
                pass
        return

    await safe_send_text(chat_id, f"⚠️ {format_size(size)} >1.9GB - Auto split ho raha hai...")

    bitrate = size / elapsed if elapsed>0 else (5*1024*1024)
    segment_time = int((MAX_TELEGRAM_SIZE * 0.9) / bitrate)
    segment_time = max(600, min(segment_time, 3600))

    split_pattern = f"{SPLITS_DIR}/{job_name}_%03d.mp4"
    for f in os.listdir(SPLITS_DIR):
        if f.startswith(job_name):
            try: os.remove(os.path.join(SPLITS_DIR, f))
            except: pass

    cmd = [
        "ffmpeg", "-y",
        "-i", file_path,
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_time),
        "-reset_timestamps", "1",
        split_pattern
    ]
    split_prog = await safe_send_text(chat_id, f"✂️ Splitting {job_name} - {format_duration(segment_time)}/part")
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await proc.wait()

    parts = sorted([f for f in os.listdir(SPLITS_DIR) if f.startswith(job_name)])
    if not parts:
        await safe_send_text(chat_id, f"❌ Split fail, file yahi hai: {file_path}")
        return

    if split_prog:
        await safe_edit_text(split_prog.chat.id, split_prog.id, f"✂️ {len(parts)} parts ban gaye, uploading...")

    for idx, part in enumerate(parts, 1):
        part_path = os.path.join(SPLITS_DIR, part)
        prog = await safe_send_text(chat_id, f"📤 Part {idx}/{len(parts)}: {part} {format_size(os.path.getsize(part_path))}")
        if prog:
            await upload_with_progress(chat_id, part_path,
                f"✅ {job_name} Part {idx}/{len(parts)}\nTotal: {format_duration(elapsed)}", prog)
            try: await prog.delete()
            except: pass
        await asyncio.sleep(1.5)  # FloodWait se bachne ke liye delay

    await safe_send_text(chat_id, f"✅ All {len(parts)} parts done - {job_name} - {format_size(size)}")

async def run_ffmpeg_and_auto_send(job_name):
    job = active_jobs.get(job_name)
    if not job:
        return
    proc = job['process']
    await proc.wait()

    if job_name not in active_jobs:
        return

    job = active_jobs[job_name]
    elapsed = time.time() - job['start_time']
    file_path = job['file']

    if job.get('monitor_task'):
        job['monitor_task'].cancel()

    await safe_edit_text(job['chat_id'], job['status_msg_id'],
        f"⚪ OFFLINE: {job_name}\nTotal: {format_duration(elapsed)}\nCheck & upload...")

    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024:
        await safe_send_text(job['chat_id'], f"❌ {job_name} 0 byte - link expire ya offline")
        active_jobs.pop(job_name, None)
        return

    await split_and_upload(job['chat_id'], file_path, job_name, elapsed)
    active_jobs.pop(job_name, None)

# ---------- TELEGRAM COMMANDS ----------
@app.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    await safe_send_text(message.chat.id,
        "🔥 V4 Koyeb Ready Bot\n\n"
        "/record <name> <direct_m3u8>\n"
        "Ex: /record my_show https://your-direct-link.m3u8\n"
        "/status\n"
        "/stop <name>\n\n"
        "Health: 0.0.0.0:8080 /health\n"
        "FloodWait protected + Auto split >1.9GB\n"
        "⚠️ Public webpage link nahi chalega, direct HLS chahiye"
    )

@app.on_message(filters.command("help"))
async def help_cmd(client, message: Message):
    await start_cmd(client, message)

@app.on_message(filters.command("record"))
async def record_cmd(client, message: Message):
    if len(message.command) < 3:
        await safe_send_text(message.chat.id,
            "❌ Format: /record <name> <direct_m3u8>\nEx: /record test https://link.m3u8")
        return

    job_name = message.command[1]
    hls_url = message.command[2]

    if not re.match(r'^[a-zA-Z0-9_-]{1,30}$', job_name):
        await safe_send_text(message.chat.id, "❌ Name me a-z 0-9 _ - only")
        return

    if not is_direct_media_link(hls_url):
        await safe_send_text(message.chat.id,
            "⚠️ Ye direct HLS link nahi hai (jaise webpage). Direct .m3u8 link de.\n"
            "Dost ke liye - uske broadcaster dashboard wala HLS lo with consent."
        )
        return

    if job_name in active_jobs:
        await safe_send_text(message.chat.id, f"❌ {job_name} already recording")
        return

    file_path = f"{RECORDINGS_DIR}/{job_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-rw_timeout", "15000000",
        "-i", hls_url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        file_path
    ]

    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        await safe_send_text(message.chat.id, "❌ ffmpeg not found")
        return

    status_msg = await safe_send_text(message.chat.id, f"🔴 STARTED: {job_name}\n0s | 0 KB")
    if not status_msg:
        status_msg = message

    active_jobs[job_name] = {
        'process': proc,
        'file': file_path,
        'url': hls_url,
        'start_time': time.time(),
        'chat_id': message.chat.id,
        'status_msg_id': status_msg.id,
        'monitor_task': None
    }

    active_jobs[job_name]['monitor_task'] = asyncio.create_task(monitor_recording(job_name))
    asyncio.create_task(run_ffmpeg_and_auto_send(job_name))

@app.on_message(filters.command("status"))
async def status_cmd(client, message: Message):
    if not active_jobs:
        await safe_send_text(message.chat.id, "📭 No active")
        return
    text = "📊 ACTIVE:\n\n"
    for name, job in active_jobs.items():
        elapsed = time.time() - job['start_time']
        try: size = os.path.getsize(job['file'])
        except: size = 0
        text += f"🔴 {name} - {format_duration(elapsed)} - {format_size(size)}\n"
    await safe_send_text(message.chat.id, text)

@app.on_message(filters.command("stop"))
async def stop_cmd(client, message: Message):
    if len(message.command) < 2:
        await safe_send_text(message.chat.id, "Use: /stop <name>")
        return
    name = message.command[1]
    if name not in active_jobs:
        await safe_send_text(message.chat.id, f"❌ {name} not active")
        return
    active_jobs[name]['process'].terminate()
    await safe_send_text(message.chat.id, f"🛑 {name} stopping...")

async def main():
    # Start web + bot together
    web_task = asyncio.create_task(start_web_server())
    await app.start()
    logger.info("Bot started - Koyeb port 8080 health OK")
    await idle()
    await app.stop()
    web_task.cancel()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
