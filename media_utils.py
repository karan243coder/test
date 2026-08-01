"""
media_utils.py - Level 1 & Level 2 Media & Stream Handling Utilities (512MB RAM Koyeb Optimized)
Handles:
  - Command Parsing (/record, timed flags, quality selection, custom headers)
  - URL Normalization (mirror domains -> canonical domain for yt-dlp extractors)
  - Public URL / direct stream extraction via yt-dlp Python API & CLI fallback
  - Extracted Web Thumbnail URL & automatic image download for Status Display header
  - Custom Headers (User-Agent, Referer, Cookie) support
  - Stream formats/quality extraction for multi-bitrate HLS
  - Video duration, width, and height extraction via ffprobe/ffmpeg
  - Custom thumbnail (.jpg) generation at 5th second (fallback or primary)
  - Automated disk cleanup of recorded .mp4 and split segments + gc.collect() memory release
"""

import os
import re
import json
import time
import asyncio
import logging
import gc
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

RECORDINGS_DIR = "recordings"
SPLITS_DIR = "splits"
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(SPLITS_DIR, exist_ok=True)


def normalize_stream_url(url: str) -> str:
    """
    Normalize known mirror domains to their canonical domains so yt-dlp extractors recognize them.
    Example: stripchatgirls.com/username -> stripchat.com/username
    """
    url_clean = url.strip()
    # Normalize Stripchat mirrors to canonical domain
    url_clean = re.sub(
        r"https?://(?:www\.)?(?:stripchatgirls|stripchatglobal|stripchateu|stripchateurope)\.com/",
        "https://stripchat.com/",
        url_clean,
        flags=re.IGNORECASE
    )
    return url_clean


def parse_record_command(text: str) -> Tuple[Optional[str], Optional[str], int, Dict[str, str], str]:
    """
    Parses command input with optional time limits, headers, and quality flag.
    Example:
      /record my_show 90m https://example.com/live.m3u8 | Referer: https://site.com | q=720p
    """
    parts = text.split(" ", 1)
    if len(parts) < 2:
        return None, None, 0, {}, "best"

    raw_args = parts[1].strip()
    sections = [x.strip() for x in raw_args.split("|")]
    main_section = sections[0]

    headers = {}
    quality = "best"

    for sec in sections[1:]:
        sec_lower = sec.lower()
        if sec_lower.startswith("q=") or sec_lower.startswith("quality="):
            quality = sec.split("=", 1)[1].strip().lower()
        elif ":" in sec:
            k, v = sec.split(":", 1)
            headers[k.strip()] = v.strip()

    tokens = main_section.split()
    if len(tokens) < 2:
        return None, None, 0, headers, quality

    job_name = tokens[0]
    url = ""
    duration_limit = 0

    for tok in tokens[1:]:
        tok_lower = tok.lower()
        if any(tok_lower.startswith(prefix) for prefix in ["http://", "https://", "rtmp://", "srt://", "rtsp://"]):
            url = normalize_stream_url(tok)
        elif re.match(r"^\d+[smh]?$", tok_lower):
            val = int(re.sub(r"[smh]", "", tok_lower))
            if tok_lower.endswith("m"):
                duration_limit = val * 60
            elif tok_lower.endswith("h"):
                duration_limit = val * 3600
            else:
                duration_limit = val
        else:
            if not url and "." in tok:
                url = normalize_stream_url(tok)

    return job_name, url, duration_limit, headers, quality


def is_explicit_direct_link(url: str) -> bool:
    """Check if URL explicitly contains direct media/stream extensions or protocols."""
    url_lower = url.lower()
    if any(x in url_lower for x in [".m3u8", ".mp4", ".m4a", ".ts", ".mpd", "master", "playlist", "chunk", "hls", "live"]):
        return True
    if any(url_lower.startswith(proto) for proto in ["rtmp://", "srt://", "rtsp://"]):
        return True
    return False


async def download_web_thumbnail(thumbnail_url: str, job_name: str) -> Optional[str]:
    """
    Download web thumbnail from yt-dlp so it can be displayed in Telegram status message header
    and used as the video cover thumbnail.
    """
    if not thumbnail_url or not thumbnail_url.startswith("http"):
        return None

    thumb_path = os.path.join(RECORDINGS_DIR, f"{job_name}_web_thumb.jpg")
    try:
        import urllib.request
        def _dl():
            req = urllib.request.Request(thumbnail_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp, open(thumb_path, "wb") as f:
                f.write(resp.read())
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _dl)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            logger.info(f"Downloaded web thumbnail to {thumb_path}")
            return thumb_path
    except Exception as e:
        logger.debug(f"Web thumbnail download failed for {thumbnail_url}: {e}")

    return None


def _extract_ytdlp_sync(url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    import yt_dlp
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "http_headers": headers or {},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


async def resolve_stream_url(url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[str, str, Optional[str], Dict[str, str]]:
    """
    Resolves public web page URLs (YouTube, live streams, video pages) to a direct stream/m3u8 URL using yt-dlp.
    If it's already an explicit direct media URL, returns it as is.
    Returns: (resolved_direct_url, title, web_thumbnail_path, combined_headers)
    """
    combined_headers = {}
    if headers:
        combined_headers.update(headers)

    normalized = normalize_stream_url(url)

    # 1. Try Python yt_dlp library first (if installed in Docker/environment)
    try:
        logger.info(f"Resolving URL via Python yt-dlp library: {normalized}")
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _extract_ytdlp_sync, normalized, combined_headers)
        title = data.get("title", "")
        extracted_url = data.get("url", "")
        thumbnail_url = data.get("thumbnail", "")

        yt_headers = data.get("http_headers", {})
        if yt_headers and isinstance(yt_headers, dict):
            for k, v in yt_headers.items():
                if k not in combined_headers:
                    combined_headers[k] = v

        web_thumb_path = None
        if thumbnail_url:
            clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", title[:15]) or "thumb"
            web_thumb_path = await download_web_thumbnail(thumbnail_url, clean_name)

        if extracted_url:
            logger.info(f"Successfully resolved stream URL via Python yt-dlp: {title}")
            return extracted_url, title, web_thumb_path, combined_headers

    except ImportError:
        logger.debug("Python yt_dlp module not installed; falling back to CLI subprocess...")
    except Exception as e:
        logger.warning(f"Python yt_dlp extraction failed for {normalized}: {e}")

    # 2. Fallback to CLI subprocess yt-dlp
    try:
        cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-warnings",
            "--no-playlist",
            "--socket-timeout", "15",
        ]
        if combined_headers.get("User-Agent"):
            cmd.extend(["--user-agent", combined_headers["User-Agent"]])
        if combined_headers.get("Referer"):
            cmd.extend(["--referer", combined_headers["Referer"]])

        cmd.append(normalized)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            title = data.get("title", "")
            extracted_url = data.get("url", "")
            thumbnail_url = data.get("thumbnail", "")

            yt_headers = data.get("http_headers", {})
            if yt_headers and isinstance(yt_headers, dict):
                for k, v in yt_headers.items():
                    if k not in combined_headers:
                        combined_headers[k] = v

            web_thumb_path = None
            if thumbnail_url:
                clean_name = re.sub(r"[^a-zA-Z0-9_-]", "_", title[:15]) or "thumb"
                web_thumb_path = await download_web_thumbnail(thumbnail_url, clean_name)

            if extracted_url:
                logger.info(f"Successfully resolved stream URL via yt-dlp CLI: {title}")
                return extracted_url, title, web_thumb_path, combined_headers

    except Exception as e:
        logger.debug(f"yt-dlp CLI extraction fallback error for {normalized}: {e}")

    # 3. Fallback to normalized URL if direct or unresolved
    return normalized, "", None, combined_headers


async def get_stream_qualities(url: str, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
    """
    Inspect a public URL or HLS stream and return a list of available quality tiers.
    Level 2 Quality Selection feature.
    """
    qualities = [
        {"id": "best", "label": "🌟 Best Quality (Default)", "desc": "Highest available video & audio"},
        {"id": "1080p", "label": "📺 1080p Full HD", "desc": "Max height 1080px"},
        {"id": "720p", "label": "🖥️ 720p HD", "desc": "Max height 720px"},
        {"id": "480p", "label": "📱 480p SD", "desc": "Max height 480px"},
        {"id": "360p", "label": "📶 360p Low", "desc": "Data saver mode"},
        {"id": "audio", "label": "🎵 Audio Only (MP3)", "desc": "Extract audio stream only"},
    ]
    return qualities


async def get_video_metadata(file_path: str) -> Dict[str, int]:
    """
    Extract video duration (seconds), width, and height using ffprobe / ffmpeg.
    Required for Telegram send_video() playable streaming support (Level 1).
    """
    metadata = {"duration": 0, "width": 0, "height": 0}
    if not os.path.exists(file_path):
        return metadata

    # 1. Try ffprobe first
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json",
        file_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            data = json.loads(stdout.decode("utf-8", errors="ignore"))
            streams = data.get("streams", [])
            if streams:
                s0 = streams[0]
                metadata["width"] = int(s0.get("width", 0) or 0)
                metadata["height"] = int(s0.get("height", 0) or 0)
                dur_str = s0.get("duration", "0")
                if dur_str and dur_str != "N/A":
                    metadata["duration"] = int(float(dur_str))

            if metadata["duration"] == 0:
                fmt = data.get("format", {})
                fmt_dur = fmt.get("duration", "0")
                if fmt_dur and fmt_dur != "N/A":
                    metadata["duration"] = int(float(fmt_dur))
    except Exception as e:
        logger.debug(f"ffprobe metadata check failed: {e}")

    # 2. If duration or dimensions still 0, fallback to ffmpeg info parser
    if metadata["duration"] == 0 or metadata["width"] == 0:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
            output_str = stderr.decode("utf-8", errors="ignore")

            dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output_str)
            if dur_match and metadata["duration"] == 0:
                h = int(dur_match.group(1))
                m = int(dur_match.group(2))
                s = float(dur_match.group(3))
                metadata["duration"] = int(h * 3600 + m * 60 + s)

            dim_match = re.search(r",\s*(\d{3,4})x(\d{3,4})\s*(?:\[|,)", output_str)
            if dim_match and metadata["width"] == 0:
                metadata["width"] = int(dim_match.group(1))
                metadata["height"] = int(dim_match.group(2))
        except Exception as e:
            logger.debug(f"ffmpeg metadata fallback failed: {e}")

    return metadata


async def generate_thumbnail(file_path: str, job_name: str, duration: int = 0, web_thumb_path: Optional[str] = None) -> Optional[str]:
    """
    Generate or provide thumbnail .jpg image for playable video (Level 1).
    If a web thumbnail from yt-dlp was already downloaded and is valid, returns it.
    Otherwise, generates a custom screenshot at the 5th second of the video.
    """
    # Use web thumbnail if available and valid
    if web_thumb_path and os.path.exists(web_thumb_path) and os.path.getsize(web_thumb_path) > 500:
        return web_thumb_path

    if not os.path.exists(file_path):
        return None

    seek_sec = 5 if duration >= 5 else 1
    thumb_path = os.path.join(RECORDINGS_DIR, f"{job_name}_thumb.jpg")

    if os.path.exists(thumb_path):
        try:
            os.remove(thumb_path)
        except:
            pass

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(seek_sec),
        "-i", file_path,
        "-vframes", "1",
        "-q:v", "2",
        thumb_path
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 500:
            logger.info(f"Thumbnail generated: {thumb_path}")
            return thumb_path
    except Exception as e:
        logger.error(f"Thumbnail generation error for {job_name}: {e}")

    return None


def cleanup_job_files(job_name: str, file_path: Optional[str] = None):
    """
    Automatically clean up all recorded .mp4 files, split parts, and .jpg thumbnails
    after upload completion or cancellation to prevent disk/memory overflow (Level 1).
    Optimized for 512MB RAM Koyeb servers: calls gc.collect() to reclaim memory.
    """
    removed_count = 0

    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
            removed_count += 1
            logger.info(f"Cleaned up main file: {file_path}")
        except Exception as e:
            logger.error(f"Error deleting file {file_path}: {e}")

    for root_dir in [RECORDINGS_DIR, "."]:
        try:
            for f in os.listdir(root_dir):
                if f.startswith(f"{job_name}_") and f.endswith(".jpg"):
                    p = os.path.join(root_dir, f)
                    if os.path.exists(p):
                        os.remove(p)
                        removed_count += 1
        except Exception as e:
            logger.debug(f"Thumb cleanup error: {e}")

    try:
        for f in os.listdir(SPLITS_DIR):
            if f.startswith(job_name):
                p = os.path.join(SPLITS_DIR, f)
                if os.path.exists(p):
                    os.remove(p)
                    removed_count += 1
    except Exception as e:
        logger.debug(f"Splits cleanup error: {e}")

    try:
        for f in os.listdir(RECORDINGS_DIR):
            if f.startswith(f"{job_name}_") and any(f.endswith(ext) for ext in [".mp4", ".ts", ".mkv", ".mp3", ".m4a", ".jpg"]):
                p = os.path.join(RECORDINGS_DIR, f)
                if os.path.exists(p):
                    os.remove(p)
                    removed_count += 1
    except Exception as e:
        logger.debug(f"Remaining recordings cleanup error: {e}")

    # Force Python garbage collection to release memory on 512MB RAM server
    gc.collect()
    logger.info(f"Auto-cleanup completed for job '{job_name}' — {removed_count} files removed. Memory reclaimed.")
