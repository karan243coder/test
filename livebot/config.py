from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    owner_ids: frozenset[int]
    max_concurrent_recordings: int
    max_record_minutes: int
    max_disk_gb: int
    progress_interval: int
    recordings_dir: Path
    log_channel_id: int

    @classmethod
    def from_env(cls) -> "Settings":
        owners = frozenset(int(v) for v in os.getenv("OWNER_IDS", "").replace(",", " ").split() if v.isdigit())
        result = cls(
            api_id=_int("API_ID", 0), api_hash=os.getenv("API_HASH", ""), bot_token=os.getenv("BOT_TOKEN", ""),
            owner_ids=owners, max_concurrent_recordings=max(1, _int("MAX_CONCURRENT_RECORDINGS", 1)),
            max_record_minutes=max(1, min(_int("MAX_RECORD_MINUTES", 120), 720)),
            max_disk_gb=max(1, _int("MAX_DISK_GB", 12)), progress_interval=max(2, _int("PROGRESS_INTERVAL_SECONDS", 4)),
            recordings_dir=Path(os.getenv("RECORDINGS_DIR", "./recordings")).resolve(),
            log_channel_id=_int("LOG_CHANNEL_ID", 0),
        )
        if not result.api_id or not result.api_hash or not result.bot_token:
            raise RuntimeError("Set API_ID, API_HASH and BOT_TOKEN in .env before starting the bot.")
        result.recordings_dir.mkdir(parents=True, exist_ok=True)
        return result
