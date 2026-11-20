"""
Database Module for Dorm Inspector Bot
Async SQLite storage using python's sqlite3 engine with thread pooling.
Provides 100% persistence across restarts, ACID transactions, and idempotency.
"""

import asyncio
from datetime import datetime
import logging
from pathlib import Path
import random
import sqlite3
from typing import Any, Dict, List, Optional

from config import DB_PATH, get_current_time

logger = logging.getLogger(__name__)

def _get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Create and configure a SQLite connection."""
    target_path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(str(target_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

# ---------------------------------------------------------------------------
# Database Schema Initialization
# ---------------------------------------------------------------------------
def _init_db_sync(db_path: Optional[Path] = None) -> None:
    """Synchronous schema initialization."""
    target_path = db_path if db_path is not None else DB_PATH
    with _get_connection(target_path) as conn:
        cursor = conn.cursor()

        # Users table (tracks chat_id from /start to allow private DMs)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                chat_id INTEGER,
                username TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Weeks table (records duty state, photo, check answers, and decisions)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS weeks (
                week_start TEXT PRIMARY KEY,
                week_number INTEGER NOT NULL,
                cleaner1_id INTEGER NOT NULL,
                cleaner2_id INTEGER NOT NULL,
                approver_id INTEGER NOT NULL,
                announced INTEGER DEFAULT 0,
                announced_at TIMESTAMP,
                reminder_sent INTEGER DEFAULT 0,
                done_reported INTEGER DEFAULT 0,
                photo_file_id TEXT,
                photo_submitted_at TIMESTAMP,
                photo_submitted_by INTEGER,
                photo_status TEXT DEFAULT 'none',
                check_sent INTEGER DEFAULT 0,
                cleaner1_response TEXT,
                cleaner2_response TEXT,
                cleaners_answered_at TIMESTAMP,
                approver_final_decision TEXT,
                approver_decided_at TIMESTAMP,
                final_status TEXT DEFAULT 'pending',
                timeout_deadline TIMESTAMP,
                timeout_processed INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Fines ledger table (strict uniqueness on week_start and user_id prevents duplicate fines)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'unpaid',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP,
                marked_paid_by INTEGER,
                UNIQUE(week_start, user_id)
            );
        """)

        # System settings key-value store (e.g. tracking one-off milestones like all members connected)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
    logger.info("Database initialized successfully at %s", target_path)

async def init_db(db_path: Optional[Path] = None) -> None:
    """Async wrapper for initializing the database."""
    await asyncio.to_thread(_init_db_sync, db_path)

# ---------------------------------------------------------------------------
# User Management Operations
# ---------------------------------------------------------------------------
def _upsert_user_sync(user_id: int, name: str, chat_id: Optional[int], username: Optional[str]) -> None:
    with _get_connection() as conn:
        conn.execute("""
            INSERT INTO users (user_id, name, chat_id, username, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                chat_id = COALESCE(excluded.chat_id, users.chat_id),
                username = COALESCE(excluded.username, users.username),
                updated_at = CURRENT_TIMESTAMP;
        """, (user_id, name, chat_id, username))
        conn.commit()

async def upsert_user(user_id: int, name: str, chat_id: Optional[int] = None, username: Optional[str] = None) -> None:
    await asyncio.to_thread(_upsert_user_sync, user_id, name, chat_id, username)

def _get_user_chat_id_sync(user_id: int) -> Optional[int]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row["chat_id"] if row and row["chat_id"] else None

async def get_user_chat_id(user_id: int) -> Optional[int]:
    return await asyncio.to_thread(_get_user_chat_id_sync, user_id)

def _get_connected_user_ids_sync() -> List[int]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE chat_id IS NOT NULL")
        return [int(row["user_id"]) for row in cursor.fetchall()]

async def get_connected_user_ids() -> List[int]:
    return await asyncio.to_thread(_get_connected_user_ids_sync)

def _get_setting_sync(key: str) -> Optional[str]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None

async def get_setting(key: str) -> Optional[str]:
    return await asyncio.to_thread(_get_setting_sync, key)

def _set_setting_sync(key: str, value: str) -> None:
    with _get_connection() as conn:
        conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP;
        """, (key, value))
        conn.commit()

async def set_setting(key: str, value: str) -> None:
    await asyncio.to_thread(_set_setting_sync, key, value)

async def get_connection_status() -> tuple:
    """Return (connected_count, total_count, connected_members, pending_members)."""
    import config
    connected_ids = set(await get_connected_user_ids())
    connected = [m for m in config.MEMBERS if m.user_id in connected_ids]
    pending = [m for m in config.MEMBERS if m.user_id not in connected_ids]
    return len(connected), len(config.MEMBERS), connected, pending

# ---------------------------------------------------------------------------
# Week Duty Operations
# ---------------------------------------------------------------------------
def _get_or_create_week_sync(
    week_start: str,
    week_number: int,
    cleaner1_id: int,
    cleaner2_id: int,
    eligible_approver_ids: List[int]
) -> Dict[str, Any]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM weeks WHERE week_start = ?", (week_start,))
        row = cursor.fetchone()
        if row:
            return dict(row)

        # Pick approver at random from eligible members
        approver_id = random.choice(eligible_approver_ids)
        cursor.execute("""
            INSERT INTO weeks (
                week_start, week_number, cleaner1_id, cleaner2_id, approver_id, final_status
            ) VALUES (?, ?, ?, ?, ?, 'pending');
        """, (week_start, week_number, cleaner1_id, cleaner2_id, approver_id))
        conn.commit()

        cursor.execute("SELECT * FROM weeks WHERE week_start = ?", (week_start,))
        return dict(cursor.fetchone())

async def get_or_create_week(
    week_start: str,
    week_number: int,
    cleaner1_id: int,
    cleaner2_id: int,
    eligible_approver_ids: List[int]
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _get_or_create_week_sync, week_start, week_number, cleaner1_id, cleaner2_id, eligible_approver_ids
    )

def _get_week_sync(week_start: str) -> Optional[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM weeks WHERE week_start = ?", (week_start,))
        row = cursor.fetchone()
        return dict(row) if row else None

async def get_week(week_start: str) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_get_week_sync, week_start)

def _mark_announced_sync(week_start: str) -> None:
    now_iso = get_current_time().isoformat()
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET announced = 1, announced_at = ?
            WHERE week_start = ?;
        """, (now_iso, week_start))
        conn.commit()

async def mark_announced(week_start: str) -> None:
    await asyncio.to_thread(_mark_announced_sync, week_start)

def _mark_reminder_sent_sync(week_start: str) -> None:
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET reminder_sent = 1
            WHERE week_start = ?;
        """, (week_start,))
        conn.commit()

async def mark_reminder_sent(week_start: str) -> None:
    await asyncio.to_thread(_mark_reminder_sent_sync, week_start)

def _mark_done_reported_sync(week_start: str) -> None:
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET done_reported = 1
            WHERE week_start = ?;
        """, (week_start,))
        conn.commit()

async def mark_done_reported(week_start: str) -> None:
    await asyncio.to_thread(_mark_done_reported_sync, week_start)

def _submit_photo_sync(week_start: str, file_id: str, user_id: int) -> None:
    now_iso = get_current_time().isoformat()
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET photo_file_id = ?,
                photo_submitted_at = ?,
                photo_submitted_by = ?,
                photo_status = 'submitted'
            WHERE week_start = ?;
        """, (file_id, now_iso, user_id, week_start))
        conn.commit()

async def submit_photo(week_start: str, file_id: str, user_id: int) -> None:
    await asyncio.to_thread(_submit_photo_sync, week_start, file_id, user_id)

def _update_photo_status_sync(week_start: str, status: str, final_status: Optional[str] = None) -> None:
    now_iso = get_current_time().isoformat()
    with _get_connection() as conn:
        if final_status:
            conn.execute("""
                UPDATE weeks
                SET photo_status = ?,
                    final_status = ?,
                    approver_final_decision = CASE WHEN ? = 'approved' THEN 'cleaned' ELSE approver_final_decision END,
                    approver_decided_at = ?
                WHERE week_start = ?;
            """, (status, final_status, status, now_iso, week_start))
        else:
            conn.execute("""
                UPDATE weeks
                SET photo_status = ?
                WHERE week_start = ?;
            """, (status, week_start))
        conn.commit()

async def update_photo_status(week_start: str, status: str, final_status: Optional[str] = None) -> None:
    await asyncio.to_thread(_update_photo_status_sync, week_start, status, final_status)

def _set_check_sent_sync(week_start: str, timeout_deadline_iso: str) -> None:
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET check_sent = 1,
                timeout_deadline = ?
            WHERE week_start = ?;
        """, (timeout_deadline_iso, week_start))
        conn.commit()

async def set_check_sent(week_start: str, timeout_deadline_iso: str) -> None:
    await asyncio.to_thread(_set_check_sent_sync, week_start, timeout_deadline_iso)

def _record_cleaner_response_sync(week_start: str, cleaner_id: int, answer: str) -> None:
    now_iso = get_current_time().isoformat()
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT cleaner1_id, cleaner2_id FROM weeks WHERE week_start = ?", (week_start,))
        row = cursor.fetchone()
        if not row:
            return

        if cleaner_id == row["cleaner1_id"]:
            conn.execute("""
                UPDATE weeks
                SET cleaner1_response = ?, cleaners_answered_at = ?
                WHERE week_start = ?;
            """, (answer, now_iso, week_start))
        elif cleaner_id == row["cleaner2_id"]:
            conn.execute("""
                UPDATE weeks
                SET cleaner2_response = ?, cleaners_answered_at = ?
                WHERE week_start = ?;
            """, (answer, now_iso, week_start))
        conn.commit()

async def record_cleaner_response(week_start: str, cleaner_id: int, answer: str) -> None:
    await asyncio.to_thread(_record_cleaner_response_sync, week_start, cleaner_id, answer)

def _record_approver_decision_sync(week_start: str, decision: str, final_status: str) -> None:
    now_iso = get_current_time().isoformat()
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET approver_final_decision = ?,
                approver_decided_at = ?,
                final_status = ?
            WHERE week_start = ?;
        """, (decision, now_iso, final_status, week_start))
        conn.commit()

async def record_approver_decision(week_start: str, decision: str, final_status: str) -> None:
    await asyncio.to_thread(_record_approver_decision_sync, week_start, decision, final_status)

def _mark_timeout_processed_sync(week_start: str, decision: str, final_status: str) -> None:
    now_iso = get_current_time().isoformat()
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET timeout_processed = 1,
                approver_final_decision = ?,
                approver_decided_at = ?,
                final_status = ?
            WHERE week_start = ?;
        """, (decision, now_iso, final_status, week_start))
        conn.commit()

async def mark_timeout_processed(week_start: str, decision: str, final_status: str) -> None:
    await asyncio.to_thread(_mark_timeout_processed_sync, week_start, decision, final_status)

def _get_history_sync(limit: int = 8) -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM weeks
            ORDER BY week_start DESC
            LIMIT ?;
        """, (limit,))
        return [dict(r) for r in cursor.fetchall()]

async def get_history(limit: int = 8) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_get_history_sync, limit)

# ---------------------------------------------------------------------------
# Fines Ledger Operations
# ---------------------------------------------------------------------------
def _create_fines_sync(
    week_start: str,
    cleaner1_id: int,
    cleaner1_name: str,
    cleaner2_id: int,
    cleaner2_name: str,
    fine_each: int
) -> bool:
    """Insert fines for both cleaners. Uses INSERT OR IGNORE for idempotence."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO fines (week_start, user_id, user_name, amount, status)
            VALUES (?, ?, ?, ?, 'unpaid');
        """, (week_start, cleaner1_id, cleaner1_name, fine_each))

        cursor.execute("""
            INSERT OR IGNORE INTO fines (week_start, user_id, user_name, amount, status)
            VALUES (?, ?, ?, ?, 'unpaid');
        """, (week_start, cleaner2_id, cleaner2_name, fine_each))

        conn.commit()
    logger.info("Fines recorded for week %s: %s & %s (%d ETB each)", week_start, cleaner1_name, cleaner2_name, fine_each)
    return True

async def create_fines_for_week(
    week_start: str,
    cleaner1_id: int,
    cleaner1_name: str,
    cleaner2_id: int,
    cleaner2_name: str,
    fine_each: int
) -> bool:
    return await asyncio.to_thread(
        _create_fines_sync, week_start, cleaner1_id, cleaner1_name, cleaner2_id, cleaner2_name, fine_each
    )

def _get_unpaid_fines_summary_sync() -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, user_name, SUM(amount) AS total_unpaid, COUNT(*) AS unpaid_count
            FROM fines
            WHERE status = 'unpaid'
            GROUP BY user_id, user_name
            ORDER BY total_unpaid DESC;
        """)
        return [dict(r) for r in cursor.fetchall()]

async def get_unpaid_fines_summary() -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_get_unpaid_fines_summary_sync)

def _mark_fines_paid_sync(user_id: int, admin_id: int) -> int:
    now_iso = get_current_time().isoformat()
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE fines
            SET status = 'paid',
                paid_at = ?,
                marked_paid_by = ?
            WHERE user_id = ? AND status = 'unpaid';
        """, (now_iso, admin_id, user_id))
        count = cursor.rowcount
        conn.commit()
        return count

async def mark_fines_paid(user_id: int, admin_id: int) -> int:
    return await asyncio.to_thread(_mark_fines_paid_sync, user_id, admin_id)

def _get_user_unpaid_total_sync(user_id: int) -> int:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM fines
            WHERE user_id = ? AND status = 'unpaid';
        """, (user_id,))
        row = cursor.fetchone()
        return int(row["total"]) if row else 0

async def get_user_unpaid_total(user_id: int) -> int:
    return await asyncio.to_thread(_get_user_unpaid_total_sync, user_id)

def _update_member_id_sync(old_id: int, new_id: int) -> None:
    with _get_connection() as conn:
        conn.execute("UPDATE users SET user_id = ? WHERE user_id = ?;", (new_id, old_id))
        conn.execute("UPDATE weeks SET cleaner1_id = ? WHERE cleaner1_id = ?;", (new_id, old_id))
        conn.execute("UPDATE weeks SET cleaner2_id = ? WHERE cleaner2_id = ?;", (new_id, old_id))
        conn.execute("UPDATE weeks SET approver_id = ? WHERE approver_id = ?;", (new_id, old_id))
        conn.execute("UPDATE weeks SET photo_submitted_by = ? WHERE photo_submitted_by = ?;", (new_id, old_id))
        conn.execute("UPDATE fines SET user_id = ? WHERE user_id = ?;", (new_id, old_id))
        conn.commit()

async def update_member_id_in_db(old_id: int, new_id: int) -> None:
    await asyncio.to_thread(_update_member_id_sync, old_id, new_id)
