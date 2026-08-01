"""
database.py - Level 3 Persistence Layer using SQLite
Stores:
  - Sudo users & authorized admins
  - Active / interrupted jobs for auto-recovery after container restart
  - Pending job queue when concurrency limit is reached
  - Bot configurations & upload settings
"""

import sqlite3
import json
import time
import os
import logging
from typing import Dict, Any, List, Optional, Set

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DB_PATH", "recorder.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize all SQLite tables if they do not exist."""
    conn = get_connection()
    cursor = conn.cursor()

    # Table for sudo / authorized users
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sudo_users (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            added_at REAL
        )
    """)

    # Table for bot settings (key-value store)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Table for active recording jobs (for persistence/recovery)
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

    # Table for pending job queue
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

    conn.commit()
    conn.close()
    logger.info(f"Database initialized at {DB_PATH}")


# ---------- SUDO / AUTHORIZATION ----------

def add_sudo(user_id: int, added_by: int = 0) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO sudo_users (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (int(user_id), int(added_by), time.time())
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error adding sudo user {user_id}: {e}")
        return False


def remove_sudo(user_id: int) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sudo_users WHERE user_id = ?", (int(user_id),))
        rows = cursor.rowcount
        conn.commit()
        conn.close()
        return rows > 0
    except Exception as e:
        logger.error(f"Error removing sudo user {user_id}: {e}")
        return False


def get_sudo_users() -> Set[int]:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM sudo_users")
        rows = cursor.fetchall()
        conn.close()
        return {int(row["user_id"]) for row in rows}
    except Exception as e:
        logger.error(f"Error fetching sudo users: {e}")
        return set()


def is_sudo(user_id: int, owner_id: int = 0, env_sudo_list: Optional[List[int]] = None) -> bool:
    """
    Check if a user is authorized.
    Returns True if user_id matches OWNER_ID, is in env SUDO_USERS, is in DB sudo_users, or if owner_id == 0.
    """
    if owner_id == 0:
        # If no owner is configured in .env, default open or log warning
        return True
    if int(user_id) == int(owner_id):
        return True
    if env_sudo_list and int(user_id) in env_sudo_list:
        return True
    return int(user_id) in get_sudo_users()


# ---------- SETTINGS MANAGEMENT ----------

def set_setting(key: str, value: Any) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (str(key), str(value))
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error setting {key}: {e}")
        return False


def get_setting(key: str, default: Any = None) -> Any:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (str(key),))
        row = cursor.fetchone()
        conn.close()
        return row["value"] if row else default
    except Exception as e:
        logger.error(f"Error getting setting {key}: {e}")
        return default


# ---------- ACTIVE JOBS PERSISTENCE ----------

def save_job(job_data: Dict[str, Any]) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
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
        logger.error(f"Error saving job {job_data.get('job_name')}: {e}")
        return False


def remove_job(job_name: str) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM active_jobs WHERE job_name = ?", (str(job_name),))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error removing job {job_name}: {e}")
        return False


def get_all_active_jobs() -> List[Dict[str, Any]]:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM active_jobs")
        rows = cursor.fetchall()
        conn.close()
        results = []
        for row in rows:
            results.append({
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
        return results
    except Exception as e:
        logger.error(f"Error fetching active jobs: {e}")
        return []


def update_job_status(job_name: str, status: str) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE active_jobs SET status = ? WHERE job_name = ?", (str(status), str(job_name)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error updating job status for {job_name}: {e}")
        return False


# ---------- JOB QUEUE (CONCURRENCY CONTROL) ----------

def add_queue_job(job_data: Dict[str, Any]) -> Optional[int]:
    """Add a job to pending queue. Returns 1-based queue position."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
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
        cursor.execute("SELECT COUNT(*) as count FROM job_queue")
        pos = cursor.fetchone()["count"]
        conn.close()
        return pos
    except sqlite3.IntegrityError:
        logger.warning(f"Job name {job_data.get('job_name')} already in queue.")
        return None
    except Exception as e:
        logger.error(f"Error adding queue job: {e}")
        return None


def pop_queue_job() -> Optional[Dict[str, Any]]:
    """Pop the oldest job from queue to start recording."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM job_queue ORDER BY job_id ASC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
        job_id = row["job_id"]
        cursor.execute("DELETE FROM job_queue WHERE job_id = ?", (job_id,))
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
        logger.error(f"Error popping queue job: {e}")
        return None


def get_queue_jobs() -> List[Dict[str, Any]]:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM job_queue ORDER BY job_id ASC")
        rows = cursor.fetchall()
        conn.close()
        results = []
        for row in rows:
            results.append({
                "job_id": row["job_id"],
                "job_name": row["job_name"],
                "url": row["url"],
                "chat_id": row["chat_id"],
                "duration_limit": row["duration_limit"],
                "headers": json.loads(row["headers_json"] or "{}"),
                "quality": row["quality"],
                "added_time": row["added_time"]
            })
        return results
    except Exception as e:
        logger.error(f"Error fetching queue jobs: {e}")
        return []


def remove_queue_job(job_name: str) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM job_queue WHERE job_name = ?", (str(job_name),))
        rows = cursor.rowcount
        conn.commit()
        conn.close()
        return rows > 0
    except Exception as e:
        logger.error(f"Error removing queue job {job_name}: {e}")
        return False
