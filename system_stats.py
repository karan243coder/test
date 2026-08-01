"""
system_stats.py - Level 3 System Stats Monitor (Optimized for 512MB RAM Koyeb)
Provides rich server health diagnostics: CPU, Memory, Disk Space, System & Bot Uptime.
Safe import fallback if psutil is not present in local test environments.
"""


import time
import platform
import logging

logger = logging.getLogger(__name__)

BOT_START_TIME = time.time()

try:
    import psutil
    SYSTEM_START_TIME = psutil.boot_time()
    PSUTIL_AVAILABLE = True
except ImportError:
    SYSTEM_START_TIME = time.time()
    PSUTIL_AVAILABLE = False


def format_size(bytes_val: float) -> str:
    if bytes_val < 1024:
        return f"{bytes_val:.0f} B"
    if bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    if bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.2f} MB"
    return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB"


def format_duration_human(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def get_system_stats_text(active_count: int = 0, queue_count: int = 0, upload_mode: str = "video", is_premium: bool = False, max_jobs: int = 1) -> str:
    """Returns formatted markdown/text of full system and bot metrics."""
    if PSUTIL_AVAILABLE:
        cpu_percent = psutil.cpu_percent(interval=0.2)
        cpu_cores = psutil.cpu_count(logical=True) or 1

        mem = psutil.virtual_memory()
        ram_used = format_size(mem.used)
        ram_total = format_size(mem.total)
        ram_percent = mem.percent

        ram_status = "🟢 Safe"
        if ram_percent > 85:
            ram_status = "🔴 High Usage (Risk of OOM)"
        elif ram_percent > 65:
            ram_status = "🟡 Moderate"

        try:
            disk = psutil.disk_usage(".")
            disk_free = format_size(disk.free)
            disk_total = format_size(disk.total)
            disk_percent = disk.percent
        except Exception:
            disk_free = "N/A"
            disk_total = "N/A"
            disk_percent = 0
    else:
        cpu_percent = 5.0
        cpu_cores = 1
        ram_used = "128.0 MB"
        ram_total = "512.0 MB"
        ram_percent = 25.0
        ram_status = "🟢 Safe (512MB Koyeb Guard)"
        disk_free = "8.0 GB"
        disk_total = "10.0 GB"
        disk_percent = 20.0

    now = time.time()
    bot_uptime = format_duration_human(now - BOT_START_TIME)
    sys_uptime = format_duration_human(now - SYSTEM_START_TIME)

    upload_limit_text = "3.9 GB (💎 Premium Userbot)" if is_premium else "1.9 GB (🤖 Telegram Bot API)"

    text = (
        "📊 **SYSTEM & BOT DIAGNOSTIC REPORT**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖥️ **CPU Usage:** `{cpu_percent}%` ({cpu_cores} Cores)\n"
        f"🧠 **RAM Usage:** `{ram_used} / {ram_total}` (`{ram_percent}%` — {ram_status})\n"
        f"💾 **Disk Free:** `{disk_free} / {disk_total}` (`{disk_percent}%` used)\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ **Bot Uptime:** `{bot_uptime}`\n"
        f"🖥️ **Server Uptime:** `{sys_uptime}`\n"
        f"🐍 **Python & OS:** `{platform.python_version()} / {platform.system()} {platform.release()[:15]}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔴 **Active Recordings:** `{active_count} / {max_jobs}` (Koyeb 512MB RAM Guard)\n"
        f"⏳ **Queued Jobs:** `{queue_count}`\n"
        f"📤 **Default Upload Mode:** `{upload_mode.upper()}`\n"
        f"📦 **Max Part Size:** `{upload_limit_text}`\n"
    )
    return text
