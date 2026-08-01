"""
database.py - Super Advanced V10 Persistence Layer (SQLite)
Tables:
  - sudo_users      : authorized users
  - settings        : key-value bot config
  - active_jobs     : running/recording jobs (crash recovery)
  - job_queue       : pending jobs when concurrency limit reached
  - watchlist       : 24/7 auto-record models
  - bot_stats       : recording counters
"""

import sqlite3
import json
import time
import os
import logging
from typing import Dict, Any, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "recorder.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sudo_users (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at REAL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_jobs (
            job_name TEXT PRIMARY KEY,
            url TEXT,
            file_path TEXT,
            chat_id INTEGER,
            status_msg_id INTEGER,
            start_time REAL,
            duration_limit INTEGER,
            headers_json TEXT,
            quality TEXT,
            status TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_queue (
            job_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name TEXT UNIQUE,
            url TEXT,
            chat_id INTEGER,
            duration_limit INTEGER,
            headers_json TEXT,
            quality TEXT,
            added_time REAL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            url TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            added_by INTEGER,
            added_at REAL,
            last_status TEXT DEFAULT 'unknown',
            last_checked REAL DEFAULT 0,
            last_recorded_at REAL DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            UNIQUE(username, chat_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_stats (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {DB_PATH}")


# ---------- SUDO / AUTHORIZATION ----------

def add_sudo(user_id: int, added_by: int = 0) -> bool:
    try:
        conn = get_connection()
        conn.execute(
            "INSERT OR REPLACE INTO sudo_users (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (int(user_id), int(added_by), time.time()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"add_sudo error: {e}")
        return False


def remove_sudo(user_id: int) -> bool:
    try:
        conn = get_connection()
        cur = conn.execute("DELETE FROM sudo_users WHERE user_id = ?", (int(user_id),))
        conn.commit()
        rows = cur.rowcount
        conn.close()
        return rows > 0
    except Exception as e:
        logger.error(f"remove_sudo error: {e}")
        return False


def get_sudo_users() -> Set[int]:
    try:
        conn = get_connection()
        rows = conn.execute("SELECT user_id FROM sudo_users").fetchall()
        conn.close()
        return {int(r["user_id"]) for r in rows}
    except Exception as e:
        logger.error(f"get_sudo_users error: {e}")
        return set()


def is_sudo(user_id: int, owner_id: int = 0, env_sudo_list: Optional[List[int]] = None) -> bool:
    if owner_id == 0:
        return True
    if int(user_id) == int(owner_id):
        return True
    if env_sudo_list and int(user_id) in env_sudo_list:
        return True
    return int(user_id) in get_sudo_users()


# ---------- SETTINGS ----------

def set_setting(key: str, value: Any) -> bool:
    try:
        conn = get_connection()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                     (str(key), str(value)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"set_setting {key} error: {e}")
        return False


def get_setting(key: str, default: Any = None) -> Any:
    try:
        conn = get_connection()
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (str(key),)).fetchone()
        conn.close()
        return row["value"] if row else default
    except Exception as e:
        logger.error(f"get_setting {key} error: {e}")
        return default


# ---------- ACTIVE JOBS (crash recovery) ----------

def save_job(job_data: Dict[str, Any]) -> bool:
    try:
        conn = get_connection()
        conn.execute("""
            INSERT OR REPLACE INTO active_jobs
            (job_name, url, file_path, chat_id, status_msg_id, start_time, duration_limit, headers_json, quality, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            job_data["job_name"],
            job_data.get("url", ""),
            job_data.get("file_path", ""),
            int(job_data.get("chat_id", 0)),
            int(job_data.get("status_msg_id", 0)),
            float(job_data.get("start_time", time.time())),
            int(job_data.get("duration_limit", 0) or 0),
            json.dumps(job_data.get("headers", {})),
            str(job_data.get("quality", "best")),
            str(job_data.get("status", "recording"))
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"save_job error: {e}")
        return False


def remove_job(job_name: str) -> bool:
    try:
        conn = get_connection()
        conn.execute("DELETE FROM active_jobs WHERE job_name = ?", (str(job_name),))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"remove_job error: {e}")
        return False


def get_all_active_jobs() -> List[Dict[str, Any]]:
    try:
        conn = get_connection()
        rows = conn.execute("SELECT * FROM active_jobs").fetchall()
        conn.close()
        out = []
        for row in rows:
            out.append({
                "job_name": row["job_name"],
                "url": row["url"],
                "file_path": row["file_path"],
                "chat_id": row["chat_id"],
                "status_msg_id": row["status_msg_id"],
                "start_time": row["start_time"],
                "duration_limit": row["duration_limit"],
                "headers": json.loads(row["headers_json"] or "{}"),
                "quality": row["quality"],
                "status": row["status"]
            })
        return out
    except Exception as e:
        logger.error(f"get_all_active_jobs error: {e}")
        return []


def update_job_status(job_name: str, status: str) -> bool:
    try:
        conn = get_connection()
        conn.execute("UPDATE active_jobs SET status = ? WHERE job_name = ?", (str(status), str(job_name)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"update_job_status error: {e}")
        return False


# ---------- JOB QUEUE ----------

def add_queue_job(job_data: Dict[str, Any]) -> Optional[int]:
    try:
        conn = get_connection()
        conn.execute("""
            INSERT INTO job_queue (job_name, url, chat_id, duration_limit, headers_json, quality, added_time)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            job_data["job_name"],
            job_data.get("url", ""),
            int(job_data.get("chat_id", 0)),
            int(job_data.get("duration_limit", 0) or 0),
            json.dumps(job_data.get("headers", {})),
            str(job_data.get("quality", "best")),
            time.time()
        ))
        conn.commit()
        count = conn.execute("SELECT COUNT(*) as c FROM job_queue").fetchone()["c"]
        conn.close()
        return count
    except sqlite3.IntegrityError:
        return None
    except Exception as e:
        logger.error(f"add_queue_job error: {e}")
        return None


def pop_queue_job() -> Optional[Dict[str, Any]]:
    try:
        conn = get_connection()
        row = conn.execute("SELECT * FROM job_queue ORDER BY job_id ASC LIMIT 1").fetchone()
        if not row:
            conn.close()
            return None
        conn.execute("DELETE FROM job_queue WHERE job_id = ?", (row["job_id"],))
        conn.commit()
        conn.close()
        return {
            "job_name": row["job_name"],
            "url": row["url"],
            "chat_id": row["chat_id"],
            "duration_limit": row["duration_limit"],
            "headers": json.loads(row["headers_json"] or "{}"),
            "quality": row["quality"]
        }
    except Exception as e:
        logger.error(f"pop_queue_job error: {e}")
        return None


def get_queue_jobs() -> List[Dict[str, Any]]:
    try:
        conn = get_connection()
        rows = conn.execute("SELECT * FROM job_queue ORDER BY job_id ASC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_queue_jobs error: {e}")
        return []


def remove_queue_job(job_name: str) -> bool:
    try:
        conn = get_connection()
        cur = conn.execute("DELETE FROM job_queue WHERE job_name = ?", (str(job_name),))
        conn.commit()
        rows = cur.rowcount
        conn.close()
        return rows > 0
    except Exception as e:
        logger.error(f"remove_queue_job error: {e}")
        return False


# ---------- WATCHLIST (24/7 auto-record) ----------

def add_watch(url: str, username: str, chat_id: int, added_by: int = 0) -> Tuple[bool, str]:
    """Returns (ok, msg)."""
    try:
        conn = get_connection()
        exists = conn.execute(
            "SELECT id FROM watchlist WHERE username = ? AND chat_id = ?",
            (username, int(chat_id))).fetchone()
        if exists:
            conn.close()
            return False, f"`{username}` pehle se watchlist mein hai."
        conn.execute("""
            INSERT INTO watchlist (username, url, chat_id, added_by, added_at, last_status, last_checked)
            VALUES (?, ?, ?, ?, ?, 'unknown', 0)
        """, (username, url, int(chat_id), int(added_by), time.time()))
        conn.commit()
        conn.close()
        return True, f"`{username}` watchlist mein add ho gaya ✅"
    except Exception as e:
        logger.error(f"add_watch error: {e}")
        return False, f"Add watch fail: {e}"


def remove_watch(username: str, chat_id: int) -> bool:
    try:
        conn = get_connection()
        cur = conn.execute("DELETE FROM watchlist WHERE username = ? AND chat_id = ?",
                           (username, int(chat_id)))
        conn.commit()
        rows = cur.rowcount
        conn.close()
        return rows > 0
    except Exception as e:
        logger.error(f"remove_watch error: {e}")
        return False


def get_watchlist() -> List[Dict[str, Any]]:
    try:
        conn = get_connection()
        rows = conn.execute("SELECT * FROM watchlist ORDER BY added_at ASC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_watchlist error: {e}")
        return []


def get_watches_for_chat(chat_id: int) -> List[Dict[str, Any]]:
    try:
        conn = get_connection()
        rows = conn.execute("SELECT * FROM watchlist WHERE chat_id = ? ORDER BY added_at ASC",
                            (int(chat_id),)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_watches_for_chat error: {e}")
        return []


def update_watch_status(username: str, chat_id: int, status: str, last_recorded_at: float = None):
    try:
        conn = get_connection()
        if last_recorded_at is not None:
            conn.execute("""
                UPDATE watchlist SET last_status = ?, last_checked = ?, last_recorded_at = ?
                WHERE username = ? AND chat_id = ?
            """, (status, time.time(), last_recorded_at, username, int(chat_id)))
        else:
            conn.execute("""
                UPDATE watchlist SET last_status = ?, last_checked = ?
                WHERE username = ? AND chat_id = ?
            """, (status, time.time(), username, int(chat_id)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"update_watch_status error: {e}")


def set_watch_enabled(username: str, chat_id: int, enabled: bool) -> bool:
    try:
        conn = get_connection()
        cur = conn.execute("UPDATE watchlist SET enabled = ? WHERE username = ? AND chat_id = ?",
                           (1 if enabled else 0, username, int(chat_id)))
        conn.commit()
        rows = cur.rowcount
        conn.close()
        return rows > 0
    except Exception as e:
        logger.error(f"set_watch_enabled error: {e}")
        return False


# ---------- BOT STATS ----------

def bump_stat(key: str, amount: float = 1):
    try:
        conn = get_connection()
        row = conn.execute("SELECT value FROM bot_stats WHERE key = ?", (key,)).fetchone()
        cur = float(row["value"]) if row else 0.0
        conn.execute("INSERT OR REPLACE INTO bot_stats (key, value) VALUES (?, ?)",
                     (key, str(cur + amount)))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"bump_stat error: {e}")


def get_stat(key: str, default: float = 0.0) -> float:
    try:
        conn = get_connection()
        row = conn.execute("SELECT value FROM bot_stats WHERE key = ?", (key,)).fetchone()
        conn.close()
        return float(row["value"]) if row else default
    except Exception as e:
        logger.error(f"get_stat error: {e}")
        return default


def get_all_stats() -> Dict[str, float]:
    try:
        conn = get_connection()
        rows = conn.execute("SELECT * FROM bot_stats").fetchall()
        conn.close()
        return {r["key"]: float(r["value"]) for r in rows}
    except Exception as e:
        logger.error(f"get_all_stats error: {e}")
        return {}
