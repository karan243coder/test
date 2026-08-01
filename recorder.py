"""
recorder.py - Super Advanced V10 Recording Engine
FFmpeg spawn, monitoring, metadata, thumbnails, Telegram upload
with progress, auto-splitting and cleanup.
"""

import os
import re
import json
import time
import asyncio
import logging
import subprocess
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

RECORDINGS_DIR = os.getenv("RECORDINGS_DIR", "recordings")
SPLITS_DIR = os.getenv("SPLITS_DIR", "splits")
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SPLITS_DIR, exist_ok=True)

FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BIN", "ffprobe")

# ------------------------------------------------------------
# FFmpeg command
# ------------------------------------------------------------

def build_ffmpeg_command(input_url: str, output_path: str,
                         headers: Optional[Dict[str, str]] = None,
                         quality: str = "best") -> List[str]:
    headers = headers or {}
    cmd = [
        FFMPEG, "-y",
        "-hide_banner",
        "-loglevel", "warning",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
        "-allowed_extensions", "ALL",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "30",
        "-reconnect_on_network_error", "1",
        "-reconnect_on_http_error", "4xx,5xx",
        "-rw_timeout", "60000000",           # 60s (fixes 1s cutoffs)
        "-timeout", "60000000",
        "-fflags", "+genpts+discardcorrupt+igndts",
        "-live_start_index", "-3",
        "-analyzeduration", "10000000",
        "-probesize", "10000000",
        "-http_persistent", "0",
    ]

    header_str = ""
    has_ua = False
    for k, v in headers.items():
        if k.lower() == "user-agent":
            cmd.extend(["-user_agent", v])
            has_ua = True
        else:
            header_str += f"{k}: {v}\r\n"
    if header_str:
        cmd.extend(["-headers", header_str])
    if not has_ua:
        cmd.extend(["-user_agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"])

    cmd.extend(["-i", input_url])

    if quality == "audio":
        cmd.extend(["-vn", "-c:a", "copy", "-max_muxing_queue_size", "2048"])
    else:
        cmd.extend(["-c", "copy", "-max_muxing_queue_size", "2048", "-movflags", "+faststart"])
    cmd.append(output_path)
    return cmd


async def spawn_ffmpeg(cmd: List[str], log_path: str):
    log_handle = open(log_path, "wb")
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=log_handle, stderr=subprocess.STDOUT)
    return proc, log_handle


# ------------------------------------------------------------
# Metadata & thumbnails
# ------------------------------------------------------------

async def get_video_metadata(file_path: str) -> Dict[str, int]:
    meta = {"duration": 0, "width": 0, "height": 0}
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return meta
    try:
        proc = await asyncio.create_subprocess_exec(
            FFPROBE, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration:format=duration",
            "-of", "json", file_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            streams = data.get("streams", [])
            if streams:
                s0 = streams[0]
                meta["width"] = int(s0.get("width", 0) or 0)
                meta["height"] = int(s0.get("height", 0) or 0)
                dur = s0.get("duration", "0")
                if dur and dur != "N/A":
                    meta["duration"] = int(float(dur))
            if not meta["duration"]:
                dur = data.get("format", {}).get("duration", "0")
                if dur and dur != "N/A":
                    meta["duration"] = int(float(dur))
    except Exception as e:
        logger.debug(f"ffprobe failed: {e}")

    if meta["duration"] == 0 or meta["width"] == 0:
        try:
            proc = await asyncio.create_subprocess_exec(
                FFMPEG, "-i", file_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await proc.communicate()
            out = stderr.decode("utf-8", errors="ignore")
            dm = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
            if dm and meta["duration"] == 0:
                h, m, s = int(dm.group(1)), int(dm.group(2)), float(dm.group(3))
                meta["duration"] = int(h * 3600 + m * 60 + s)
            wm = re.search(r",\s*(\d{3,4})x(\d{3,4})\s*(?:\[|,)", out)
            if wm and meta["width"] == 0:
                meta["width"] = int(wm.group(1))
                meta["height"] = int(wm.group(2))
        except Exception as e:
            logger.debug(f"ffmpeg fallback failed: {e}")
    return meta


async def generate_thumbnail(file_path: str, job_name: str, duration: int = 0,
                             web_thumb_path: Optional[str] = None) -> Optional[str]:
    if web_thumb_path and os.path.exists(web_thumb_path) and os.path.getsize(web_thumb_path) > 500:
        return web_thumb_path
    if not os.path.exists(file_path):
        return None
    seek = 5 if duration >= 5 else 1
    thumb_path = os.path.join(RECORDINGS_DIR, f"{job_name}_thumb.jpg")
    try:
        if os.path.exists(thumb_path):
            os.remove(thumb_path)
    except Exception:
        pass
    cmd = [FFMPEG, "-y", "-ss", str(seek), "-i", file_path, "-vframes", "1", "-q:v", "2", thumb_path]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        await proc.wait()
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            return thumb_path
    except Exception as e:
        logger.error(f"Thumbnail error: {e}")
    return None


# ------------------------------------------------------------
# Upload with live progress
# ------------------------------------------------------------

async def upload_with_progress(client, chat_id: int, file_path: str, caption: str,
                               progress_msg=None, job_name: str = "",
                               elapsed: float = 0, web_thumb_path: Optional[str] = None,
                               upload_mode: str = "video") -> bool:
    from pyrogram import enums as _enums
    from pyrogram.errors import FloodWait

    total_size = os.path.getsize(file_path)
    last_update = [0.0]
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
        txt = f"📤 **UPLOADING**\n`{bar}` **{percent}%**\n💾 `{_fmt_size(current)} / {_fmt_size(total)}`"
        if progress_msg:
            try:
                await progress_msg.edit_text(txt, parse_mode=_enums.ParseMode.MARKDOWN)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
            except Exception:
                try:
                    await progress_msg.edit_text(txt, parse_mode=_enums.ParseMode.DISABLED)
                except Exception:
                    pass

    meta = await get_video_metadata(file_path)
    duration = meta.get("duration", 0) or int(elapsed) or 1
    width = meta.get("width", 0)
    height = meta.get("height", 0)

    thumb_path = None
    if not file_path.endswith((".mp3", ".m4a")):
        thumb_path = await generate_thumbnail(file_path, job_name, duration, web_thumb_path=web_thumb_path)

    plain_caption = f"{job_name} | {_fmt_duration(duration)} | {_fmt_size(total_size)}"

    for attempt in range(3):
        try:
            if upload_mode == "video" and not file_path.endswith((".mp3", ".m4a")):
                await client.send_video(
                    chat_id=chat_id, video=file_path, caption=caption, duration=duration,
                    width=width, height=height, thumb=thumb_path, supports_streaming=True,
                    progress=progress_cb, parse_mode=_enums.ParseMode.MARKDOWN)
            else:
                await client.send_document(
                    chat_id=chat_id, document=file_path, caption=caption,
                    thumb=thumb_path, progress=progress_cb, parse_mode=_enums.ParseMode.MARKDOWN)
            return True
        except FloodWait as e:
            await asyncio.sleep(e.value + 2)
        except Exception as e:
            err = str(e)
            if "ENTITY_BOUNDS_INVALID" in err or "can't parse" in err.lower():
                try:
                    if upload_mode == "video" and not file_path.endswith((".mp3", ".m4a")):
                        await client.send_video(
                            chat_id=chat_id, video=file_path, caption=plain_caption,
                            duration=duration, width=width, height=height, thumb=thumb_path,
                            supports_streaming=True, progress=progress_cb,
                            parse_mode=_enums.ParseMode.DISABLED)
                    else:
                        await client.send_document(
                            chat_id=chat_id, document=file_path, caption=plain_caption,
                            thumb=thumb_path, progress=progress_cb,
                            parse_mode=_enums.ParseMode.DISABLED)
                    return True
                except Exception as e2:
                    logger.error(f"Upload fallback failed: {e2}")
            logger.error(f"Upload attempt {attempt + 1} error: {e}")
            await asyncio.sleep(2)
    return False


async def split_and_upload(client, chat_id: int, file_path: str, job_name: str,
                           elapsed: float, web_thumb_path: Optional[str] = None,
                           upload_mode: str = "video",
                           max_size: int = 1900 * 1024 * 1024,
                           safe_send_text=None, safe_edit_message=None) -> bool:
    size = os.path.getsize(file_path)
    if size <= max_size:
        prog = await safe_send_text(chat_id, f"📤 **Preparing Upload:** `{job_name}` (`{_fmt_size(size)}`)...")
        caption = f"✅ **{job_name}**\n⏱ **Duration:** `{_fmt_duration(elapsed)}`\n💾 **Size:** `{_fmt_size(size)}`"
        ok = await upload_with_progress(client, chat_id, file_path, caption, progress_msg=prog,
                                        job_name=job_name, elapsed=elapsed,
                                        web_thumb_path=web_thumb_path, upload_mode=upload_mode)
        if prog:
            try:
                await prog.delete()
            except Exception:
                pass
        if not ok:
            await safe_send_text(chat_id, f"❌ Upload failed for `{job_name}`. File locally saved.")
        return ok

    await safe_send_text(chat_id, f"⚠️ **Size `{_fmt_size(size)}` limit se zyada**\n✂️ Splitting...")
    bitrate = size / elapsed if elapsed > 0 else 5 * 1024 * 1024
    segment_time = int((max_size * 0.9) / bitrate)
    segment_time = max(600, min(segment_time, 3600))
    ext = ".mp4" if not file_path.endswith((".mp3", ".m4a")) else os.path.splitext(file_path)[1]
    split_pattern = os.path.join(SPLITS_DIR, f"{job_name}_%03d{ext}")

    for f in os.listdir(SPLITS_DIR):
        if f.startswith(job_name):
            try:
                os.remove(os.path.join(SPLITS_DIR, f))
            except Exception:
                pass

    cmd = [FFMPEG, "-y", "-i", file_path, "-c", "copy", "-f", "segment",
           "-segment_time", str(segment_time), "-reset_timestamps", "1", split_pattern]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    await proc.wait()
    parts = sorted(f for f in os.listdir(SPLITS_DIR) if f.startswith(job_name))
    if not parts:
        await safe_send_text(chat_id, f"❌ Splitting failed: `{file_path}`")
        return False

    split_prog = await safe_send_text(chat_id, f"✂️ **{len(parts)} parts ban gaye.** Uploading...")
    for idx, part in enumerate(parts, 1):
        part_path = os.path.join(SPLITS_DIR, part)
        prog = await safe_send_text(chat_id, f"📤 **Segment {idx}/{len(parts)}:** `{part}`")
        if prog:
            await upload_with_progress(
                client, chat_id, part_path,
                f"✅ **{job_name}** — Part {idx}/{len(parts)}\n⏱ `{_fmt_duration(elapsed)}`",
                progress_msg=prog, job_name=f"{job_name}_p{idx}",
                elapsed=elapsed / len(parts), web_thumb_path=web_thumb_path,
                upload_mode=upload_mode)
            try:
                await prog.delete()
            except Exception:
                pass
        await asyncio.sleep(1.5)
    await safe_send_text(chat_id, f"✅ **Saare {len(parts)} parts upload ho gaye:** `{job_name}`")
    if split_prog:
        try:
            await split_prog.delete()
        except Exception:
            pass
    return True


# ------------------------------------------------------------
# Cleanup
# ------------------------------------------------------------

def cleanup_job_files(job_name: str, file_path: Optional[str] = None):
    removed = 0
    for p in [file_path, os.path.join(RECORDINGS_DIR, f"{job_name}_ffmpeg.log")]:
        if p and os.path.exists(p):
            try:
                os.remove(p)
                removed += 1
            except Exception:
                pass
    for d in (RECORDINGS_DIR, SPLITS_DIR):
        try:
            for f in os.listdir(d):
                if f.startswith(f"{job_name}_") and f.endswith((".jpg", ".mp4", ".mp3", ".m4a", ".ts", ".mkv", ".log")):
                    try:
                        os.remove(os.path.join(d, f))
                        removed += 1
                    except Exception:
                        pass
        except Exception:
            pass
    logger.info(f"Cleanup '{job_name}' — {removed} files removed.")


def cleanup_old_files(max_age_hours: int = 24):
    """Remove stray files older than N hours (crash leftovers)."""
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for d in (RECORDINGS_DIR, SPLITS_DIR):
        try:
            for f in os.listdir(d):
                p = os.path.join(d, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                        removed += 1
                except Exception:
                    pass
        except Exception:
            pass
    if removed:
        logger.info(f"Old-file cleanup: {removed} files removed.")


def _fmt_size(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.2f} TB"


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
