from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone


def size_text(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    n = float(max(0, value))
    for unit in units:
        if n < 1024 or unit == units[-1]: return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024

@dataclass
class LiveProgress:
    platform: str
    title: str
    started_at: datetime
    bytes_written: int = 0
    reconnects: int = 0
    status: str = "Resolving public stream"

    def render(self) -> str:
        elapsed = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
        h, r = divmod(max(0, elapsed), 3600); m, s = divmod(r, 60)
        return (
            f"🔴 <b>LIVE RECORDER</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🎥 <b>Platform:</b> {self.platform}\n"
            f"📺 <b>Room:</b> <code>{self.title[:80]}</code>\n"
            f"⚙️ <b>Status:</b> {self.status}\n"
            f"⏱ <b>Recorded:</b> {h:02}:{m:02}:{s:02}\n"
            f"📦 <b>Saved:</b> {size_text(self.bytes_written)}\n"
            f"🔁 <b>Reconnects:</b> {self.reconnects}\n\n"
            f"Use /livestatus or /livestop"
        )
