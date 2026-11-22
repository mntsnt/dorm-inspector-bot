"""
Database Module for Dorm Inspector Bot (Multi-Tenant Platform)
Supports both universal multi-dorm management and backward-compatible single dorm access.
Async SQLite storage using python's sqlite3 engine with thread pooling.
Provides 100% persistence across restarts, ACID transactions, and idempotency.
"""

import asyncio
from datetime import datetime
import logging
from pathlib import Path
import random
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import config

logger = logging.getLogger(__name__)

DB_PATH = config.DB_PATH

class _DBConnectionContext:
    def __init__(self, db_path: Optional[Path] = None):
        target_path = db_path if db_path is not None else DB_PATH
        self.conn = sqlite3.connect(str(target_path), timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")

    def __enter__(self) -> sqlite3.Connection:
        self.conn.__enter__()
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.conn.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.conn.close()

def _get_connection(db_path: Optional[Path] = None):
    """Create and configure a managed SQLite connection context."""
    return _DBConnectionContext(db_path)

# ---------------------------------------------------------------------------
# Database Schema Initialization & Auto-Migration
# ---------------------------------------------------------------------------
def _init_db_sync(db_path: Optional[Path] = None) -> None:
    """Synchronous schema initialization with automatic column migrations."""
    target_path = db_path if db_path is not None else DB_PATH
    with _get_connection(target_path) as conn:
        cursor = conn.cursor()

        # 1. Dorms Table (Each Telegram Group / Topic is a Dorm)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dorms (
                dorm_id INTEGER PRIMARY KEY,
                topic_id INTEGER,
                admin_id INTEGER NOT NULL,
                dorm_name TEXT NOT NULL,
                fine_amount INTEGER DEFAULT 100,
                start_date TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 2. Users Table (Tracks global chat_id from /start for private DMs)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                chat_id INTEGER,
                username TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 3. Dorm Members Table (Scoping members to specific dorms)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dorm_members (
                dorm_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                chat_id INTEGER,
                username TEXT,
                is_admin INTEGER DEFAULT 0,
                pair_number INTEGER,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (dorm_id, user_id)
            );
        """)

        # 4. Dorm Pairs Table (Configured cleaning pairs per dorm)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dorm_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dorm_id INTEGER NOT NULL,
                pair_number INTEGER NOT NULL,
                cleaner1_id INTEGER NOT NULL,
                cleaner2_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(dorm_id, pair_number)
            );
        """)

        # 5. Weeks Table (Multi-tenant weekly records)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS weeks (
                week_start TEXT NOT NULL,
                dorm_id INTEGER DEFAULT 0,
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (dorm_id, week_start)
            );
        """)

        # Migration check: Ensure dorm_id exists on legacy weeks table
        cursor.execute("PRAGMA table_info(weeks);")
        week_cols = [r["name"] for r in cursor.fetchall()]
        if "dorm_id" not in week_cols:
            cursor.execute("ALTER TABLE weeks ADD COLUMN dorm_id INTEGER DEFAULT 0;")

        # 6. Fines Ledger Table (Multi-tenant fines ledger)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dorm_id INTEGER DEFAULT 0,
                week_start TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'unpaid',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at TIMESTAMP,
                marked_paid_by INTEGER,
                UNIQUE(dorm_id, week_start, user_id)
            );
        """)

        # Migration check: Ensure dorm_id exists on legacy fines table
        cursor.execute("PRAGMA table_info(fines);")
        fine_cols = [r["name"] for r in cursor.fetchall()]
        if "dorm_id" not in fine_cols:
            cursor.execute("ALTER TABLE fines ADD COLUMN dorm_id INTEGER DEFAULT 0;")

        # 7. System Settings Key-Value Store
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
    logger.info("Database schema initialized and verified successfully at %s", target_path)

async def init_db(db_path: Optional[Path] = None) -> None:
    """Async wrapper for initializing the database."""
    await asyncio.to_thread(_init_db_sync, db_path)

def _resolve_dorm_id(dorm_id: Optional[int]) -> int:
    """Resolve target dorm_id, falling back to config.GROUP_ID for backward compatibility."""
    if dorm_id is not None and dorm_id != 0:
        return dorm_id
    return getattr(config, "GROUP_ID", 0)

# ---------------------------------------------------------------------------
# Dorm Platform Operations
# ---------------------------------------------------------------------------
def _create_or_update_dorm_sync(
    dorm_id: int,
    admin_id: int,
    dorm_name: str,
    topic_id: Optional[int] = None,
    fine_amount: int = 100,
    start_date: Optional[str] = None,
    status: str = "active"
) -> Dict[str, Any]:
    if not start_date:
        start_date = getattr(config, "START_DATE_STR", "2026-11-23")

    with _get_connection() as conn:
        conn.execute("""
            INSERT INTO dorms (dorm_id, topic_id, admin_id, dorm_name, fine_amount, start_date, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(dorm_id) DO UPDATE SET
                topic_id = COALESCE(excluded.topic_id, dorms.topic_id),
                admin_id = excluded.admin_id,
                dorm_name = excluded.dorm_name,
                fine_amount = excluded.fine_amount,
                start_date = COALESCE(excluded.start_date, dorms.start_date),
                status = excluded.status,
                updated_at = CURRENT_TIMESTAMP;
        """, (dorm_id, topic_id, admin_id, dorm_name, fine_amount, start_date, status))
        conn.commit()

        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorms WHERE dorm_id = ?", (dorm_id,))
        return dict(cursor.fetchone())

async def create_or_update_dorm(
    dorm_id: int,
    admin_id: int,
    dorm_name: str,
    topic_id: Optional[int] = None,
    fine_amount: int = 100,
    start_date: Optional[str] = None,
    status: str = "active"
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _create_or_update_dorm_sync, dorm_id, admin_id, dorm_name, topic_id, fine_amount, start_date, status
    )

def _get_dorm_sync(dorm_id: int) -> Optional[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorms WHERE dorm_id = ?", (dorm_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

async def get_dorm(dorm_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_dorm_sync, resolved_id)

def _get_all_active_dorms_sync() -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorms WHERE status = 'active'")
        return [dict(r) for r in cursor.fetchall()]

async def get_all_active_dorms() -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_get_all_active_dorms_sync)

def _update_dorm_status_sync(dorm_id: int, status: str) -> None:
    with _get_connection() as conn:
        conn.execute("UPDATE dorms SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (status, dorm_id))
        conn.commit()

async def update_dorm_status(dorm_id: int, status: str) -> None:
    await asyncio.to_thread(_update_dorm_status_sync, dorm_id, status)

def _update_dorm_settings_sync(
    dorm_id: int,
    dorm_name: Optional[str] = None,
    fine_amount: Optional[int] = None,
    topic_id: Optional[int] = None,
    start_date: Optional[str] = None,
) -> None:
    with _get_connection() as conn:
        if dorm_name is not None:
            conn.execute("UPDATE dorms SET dorm_name = ?, updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (dorm_name, dorm_id))
        if fine_amount is not None:
            conn.execute("UPDATE dorms SET fine_amount = ?, updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (fine_amount, dorm_id))
        if topic_id is not None:
            conn.execute("UPDATE dorms SET topic_id = ?, updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (topic_id, dorm_id))
        if start_date is not None:
            conn.execute("UPDATE dorms SET start_date = ?, updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (start_date, dorm_id))
        conn.commit()

async def update_dorm_settings(
    dorm_id: int,
    dorm_name: Optional[str] = None,
    fine_amount: Optional[int] = None,
    topic_id: Optional[int] = None,
    start_date: Optional[str] = None,
) -> None:
    await asyncio.to_thread(_update_dorm_settings_sync, dorm_id, dorm_name, fine_amount, topic_id, start_date)

# ---------------------------------------------------------------------------
# User & Member Management Operations
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
        # Also sync chat_id to any dorm_members records for this user
        if chat_id:
            conn.execute("UPDATE dorm_members SET chat_id = ? WHERE user_id = ?", (chat_id, user_id))
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

def _add_dorm_member_sync(
    dorm_id: int,
    user_id: int,
    name: str,
    chat_id: Optional[int] = None,
    username: Optional[str] = None,
    is_admin: int = 0
) -> None:
    with _get_connection() as conn:
        # First upsert into global users table
        conn.execute("""
            INSERT INTO users (user_id, name, chat_id, username, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                name = excluded.name,
                chat_id = COALESCE(excluded.chat_id, users.chat_id),
                username = COALESCE(excluded.username, users.username),
                updated_at = CURRENT_TIMESTAMP;
        """, (user_id, name, chat_id, username))

        # Check existing chat_id from users table if not passed
        if not chat_id:
            c = conn.cursor()
            c.execute("SELECT chat_id FROM users WHERE user_id = ?", (user_id,))
            r = c.fetchone()
            if r and r["chat_id"]:
                chat_id = r["chat_id"]

        # Insert or update in dorm_members
        conn.execute("""
            INSERT INTO dorm_members (dorm_id, user_id, name, chat_id, username, is_admin, joined_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(dorm_id, user_id) DO UPDATE SET
                name = excluded.name,
                chat_id = COALESCE(excluded.chat_id, dorm_members.chat_id),
                username = COALESCE(excluded.username, dorm_members.username),
                is_admin = CASE WHEN excluded.is_admin = 1 THEN 1 ELSE dorm_members.is_admin END;
        """, (dorm_id, user_id, name, chat_id, username, is_admin))
        conn.commit()

async def add_dorm_member(
    dorm_id: int,
    user_id: int,
    name: str,
    chat_id: Optional[int] = None,
    username: Optional[str] = None,
    is_admin: int = 0
) -> None:
    await asyncio.to_thread(_add_dorm_member_sync, dorm_id, user_id, name, chat_id, username, is_admin)

def _remove_dorm_member_sync(dorm_id: int, user_id: int) -> None:
    with _get_connection() as conn:
        conn.execute("DELETE FROM dorm_members WHERE dorm_id = ? AND user_id = ?", (dorm_id, user_id))
        conn.commit()

async def remove_dorm_member(dorm_id: int, user_id: int) -> None:
    await asyncio.to_thread(_remove_dorm_member_sync, dorm_id, user_id)

def _get_dorm_members_sync(dorm_id: int) -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorm_members WHERE dorm_id = ? ORDER BY joined_at ASC", (dorm_id,))
        return [dict(r) for r in cursor.fetchall()]

async def get_dorm_members(dorm_id: Optional[int] = None) -> List[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_dorm_members_sync, resolved_id)

def _get_dorm_member_sync(dorm_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorm_members WHERE dorm_id = ? AND user_id = ?", (dorm_id, user_id))
        row = cursor.fetchone()
        return dict(row) if row else None

async def get_dorm_member(dorm_id: Optional[int], user_id: int) -> Optional[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_dorm_member_sync, resolved_id, user_id)

def _get_dorm_member_by_name_sync(dorm_id: int, name: str) -> Optional[Dict[str, Any]]:
    target_norm = name.strip().lower()
    if target_norm in ("hasan", "hassen", "hasen"):
        target_norm = "hassan"

    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorm_members WHERE dorm_id = ?", (dorm_id,))
        for row in cursor.fetchall():
            row_norm = row["name"].strip().lower()
            if row_norm in ("hasan", "hassen", "hasen"):
                row_norm = "hassan"
            if row_norm == target_norm:
                return dict(row)
        return None

async def get_dorm_member_by_name(dorm_id: Optional[int], name: str) -> Optional[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_dorm_member_by_name_sync, resolved_id, name)

def _get_user_dorms_sync(user_id: int) -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT d.*, m.is_admin AS user_is_admin, m.name AS member_name
            FROM dorms d
            JOIN dorm_members m ON d.dorm_id = m.dorm_id
            WHERE m.user_id = ? AND d.status = 'active';
        """, (user_id,))
        return [dict(r) for r in cursor.fetchall()]

async def get_user_dorms(user_id: int) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_get_user_dorms_sync, user_id)

# ---------------------------------------------------------------------------
# Pairs Management Operations
# ---------------------------------------------------------------------------
def _set_dorm_pairs_sync(dorm_id: int, pairs: List[Tuple[int, int]]) -> None:
    with _get_connection() as conn:
        conn.execute("DELETE FROM dorm_pairs WHERE dorm_id = ?", (dorm_id,))
        for idx, (c1, c2) in enumerate(pairs, start=1):
            conn.execute("""
                INSERT INTO dorm_pairs (dorm_id, pair_number, cleaner1_id, cleaner2_id)
                VALUES (?, ?, ?, ?);
            """, (dorm_id, idx, c1, c2))
            conn.execute("UPDATE dorm_members SET pair_number = ? WHERE dorm_id = ? AND user_id IN (?, ?)", (idx, dorm_id, c1, c2))
        conn.commit()

async def set_dorm_pairs(dorm_id: int, pairs: List[Tuple[int, int]]) -> None:
    await asyncio.to_thread(_set_dorm_pairs_sync, dorm_id, pairs)

def _get_dorm_pairs_sync(dorm_id: int) -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorm_pairs WHERE dorm_id = ? ORDER BY pair_number ASC", (dorm_id,))
        return [dict(r) for r in cursor.fetchall()]

async def get_dorm_pairs(dorm_id: Optional[int] = None) -> List[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_dorm_pairs_sync, resolved_id)

def _auto_generate_dorm_pairs_sync(dorm_id: int) -> List[Tuple[int, int]]:
    """Automatically pair all registered dorm members."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM dorm_members WHERE dorm_id = ? ORDER BY joined_at ASC", (dorm_id,))
        members = [r["user_id"] for r in cursor.fetchall()]

    if len(members) < 2:
        return []

    pairs: List[Tuple[int, int]] = []
    for i in range(0, len(members) - 1, 2):
        pairs.append((members[i], members[i + 1]))

    # If odd member count, pair last person with the first person
    if len(members) % 2 != 0:
        pairs.append((members[-1], members[0]))

    _set_dorm_pairs_sync(dorm_id, pairs)
    return pairs

async def auto_generate_dorm_pairs(dorm_id: int) -> List[Tuple[int, int]]:
    return await asyncio.to_thread(_auto_generate_dorm_pairs_sync, dorm_id)

# ---------------------------------------------------------------------------
# Week Duty Operations (Scoped to dorm_id)
# ---------------------------------------------------------------------------
def _get_or_create_week_sync(
    week_start: str,
    week_number: int,
    cleaner1_id: int,
    cleaner2_id: int,
    eligible_approver_ids: List[int],
    dorm_id: int = 0
) -> Dict[str, Any]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM weeks WHERE dorm_id = ? AND week_start = ?", (dorm_id, week_start))
        row = cursor.fetchone()
        if row:
            return dict(row)

        # Pick approver at random from eligible members
        approver_id = random.choice(eligible_approver_ids) if eligible_approver_ids else cleaner1_id
        cursor.execute("""
            INSERT INTO weeks (
                dorm_id, week_start, week_number, cleaner1_id, cleaner2_id, approver_id, final_status
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending');
        """, (dorm_id, week_start, week_number, cleaner1_id, cleaner2_id, approver_id))
        conn.commit()

        cursor.execute("SELECT * FROM weeks WHERE dorm_id = ? AND week_start = ?", (dorm_id, week_start))
        return dict(cursor.fetchone())

async def get_or_create_week(
    week_start: str,
    week_number: int,
    cleaner1_id: int,
    cleaner2_id: int,
    eligible_approver_ids: List[int],
    dorm_id: Optional[int] = None
) -> Dict[str, Any]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(
        _get_or_create_week_sync, week_start, week_number, cleaner1_id, cleaner2_id, eligible_approver_ids, resolved_id
    )

def _get_week_sync(week_start: str, dorm_id: int = 0) -> Optional[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM weeks WHERE dorm_id = ? AND week_start = ?", (dorm_id, week_start))
        row = cursor.fetchone()
        return dict(row) if row else None

async def get_week(week_start: str, dorm_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_week_sync, week_start, resolved_id)

def _mark_announced_sync(week_start: str, dorm_id: int = 0) -> None:
    now_iso = config.get_current_time().isoformat()
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET announced = 1, announced_at = ?
            WHERE dorm_id = ? AND week_start = ?;
        """, (now_iso, dorm_id, week_start))
        conn.commit()

async def mark_announced(week_start: str, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_mark_announced_sync, week_start, resolved_id)

def _mark_reminder_sent_sync(week_start: str, dorm_id: int = 0) -> None:
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET reminder_sent = 1
            WHERE dorm_id = ? AND week_start = ?;
        """, (dorm_id, week_start))
        conn.commit()

async def mark_reminder_sent(week_start: str, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_mark_reminder_sent_sync, week_start, resolved_id)

def _mark_done_reported_sync(week_start: str, dorm_id: int = 0) -> None:
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET done_reported = 1
            WHERE dorm_id = ? AND week_start = ?;
        """, (dorm_id, week_start))
        conn.commit()

async def mark_done_reported(week_start: str, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_mark_done_reported_sync, week_start, resolved_id)

def _submit_photo_sync(week_start: str, file_id: str, user_id: int, dorm_id: int = 0) -> None:
    now_iso = config.get_current_time().isoformat()
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET photo_file_id = ?,
                photo_submitted_at = ?,
                photo_submitted_by = ?,
                photo_status = 'submitted'
            WHERE dorm_id = ? AND week_start = ?;
        """, (file_id, now_iso, user_id, dorm_id, week_start))
        conn.commit()

async def submit_photo(week_start: str, file_id: str, user_id: int, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_submit_photo_sync, week_start, file_id, user_id, resolved_id)

def _update_photo_status_sync(week_start: str, status: str, final_status: Optional[str] = None, dorm_id: int = 0) -> None:
    now_iso = config.get_current_time().isoformat()
    with _get_connection() as conn:
        if final_status:
            conn.execute("""
                UPDATE weeks
                SET photo_status = ?,
                    final_status = ?,
                    approver_final_decision = CASE WHEN ? = 'approved' THEN 'cleaned' ELSE approver_final_decision END,
                    approver_decided_at = ?
                WHERE dorm_id = ? AND week_start = ?;
            """, (status, final_status, status, now_iso, dorm_id, week_start))
        else:
            conn.execute("""
                UPDATE weeks
                SET photo_status = ?
                WHERE dorm_id = ? AND week_start = ?;
            """, (status, dorm_id, week_start))
        conn.commit()

async def update_photo_status(week_start: str, status: str, final_status: Optional[str] = None, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_update_photo_status_sync, week_start, status, final_status, resolved_id)

def _set_check_sent_sync(week_start: str, timeout_deadline_iso: str, dorm_id: int = 0) -> None:
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET check_sent = 1,
                timeout_deadline = ?
            WHERE dorm_id = ? AND week_start = ?;
        """, (timeout_deadline_iso, dorm_id, week_start))
        conn.commit()

async def set_check_sent(week_start: str, timeout_deadline_iso: str, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_set_check_sent_sync, week_start, timeout_deadline_iso, resolved_id)

def _record_cleaner_response_sync(week_start: str, cleaner_id: int, answer: str, dorm_id: int = 0) -> None:
    now_iso = config.get_current_time().isoformat()
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT cleaner1_id, cleaner2_id FROM weeks WHERE dorm_id = ? AND week_start = ?", (dorm_id, week_start))
        row = cursor.fetchone()
        if not row:
            return

        if cleaner_id == row["cleaner1_id"]:
            conn.execute("""
                UPDATE weeks
                SET cleaner1_response = ?, cleaners_answered_at = ?
                WHERE dorm_id = ? AND week_start = ?;
            """, (answer, now_iso, dorm_id, week_start))
        elif cleaner_id == row["cleaner2_id"]:
            conn.execute("""
                UPDATE weeks
                SET cleaner2_response = ?, cleaners_answered_at = ?
                WHERE dorm_id = ? AND week_start = ?;
            """, (answer, now_iso, dorm_id, week_start))
        conn.commit()

async def record_cleaner_response(week_start: str, cleaner_id: int, answer: str, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_record_cleaner_response_sync, week_start, cleaner_id, answer, resolved_id)

def _record_approver_decision_sync(week_start: str, decision: str, final_status: str, dorm_id: int = 0) -> None:
    now_iso = config.get_current_time().isoformat()
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET approver_final_decision = ?,
                approver_decided_at = ?,
                final_status = ?
            WHERE dorm_id = ? AND week_start = ?;
        """, (decision, now_iso, final_status, dorm_id, week_start))
        conn.commit()

async def record_approver_decision(week_start: str, decision: str, final_status: str, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_record_approver_decision_sync, week_start, decision, final_status, resolved_id)

def _mark_timeout_processed_sync(week_start: str, decision: str, final_status: str, dorm_id: int = 0) -> None:
    now_iso = config.get_current_time().isoformat()
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET timeout_processed = 1,
                approver_final_decision = ?,
                approver_decided_at = ?,
                final_status = ?
            WHERE dorm_id = ? AND week_start = ?;
        """, (decision, now_iso, final_status, dorm_id, week_start))
        conn.commit()

async def mark_timeout_processed(week_start: str, decision: str, final_status: str, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_mark_timeout_processed_sync, week_start, decision, final_status, resolved_id)

def _get_history_sync(limit: int = 8, dorm_id: int = 0) -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM weeks
            WHERE dorm_id = ?
            ORDER BY week_start DESC
            LIMIT ?;
        """, (dorm_id, limit))
        return [dict(r) for r in cursor.fetchall()]

async def get_history(limit: int = 8, dorm_id: Optional[int] = None) -> List[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_history_sync, limit, resolved_id)

# ---------------------------------------------------------------------------
# Fines Ledger Operations (Scoped to dorm_id)
# ---------------------------------------------------------------------------
def _create_fines_sync(
    week_start: str,
    cleaner1_id: int,
    cleaner1_name: str,
    cleaner2_id: int,
    cleaner2_name: str,
    fine_each: int,
    dorm_id: int = 0
) -> bool:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR IGNORE INTO fines (dorm_id, week_start, user_id, user_name, amount, status)
            VALUES (?, ?, ?, ?, ?, 'unpaid');
        """, (dorm_id, week_start, cleaner1_id, cleaner1_name, fine_each))

        cursor.execute("""
            INSERT OR IGNORE INTO fines (dorm_id, week_start, user_id, user_name, amount, status)
            VALUES (?, ?, ?, ?, ?, 'unpaid');
        """, (dorm_id, week_start, cleaner2_id, cleaner2_name, fine_each))

        conn.commit()
    logger.info("Fines recorded for dorm %s, week %s: %s & %s (%d ETB each)", dorm_id, week_start, cleaner1_name, cleaner2_name, fine_each)
    return True

async def create_fines_for_week(
    week_start: str,
    cleaner1_id: int,
    cleaner1_name: str,
    cleaner2_id: int,
    cleaner2_name: str,
    fine_each: int,
    dorm_id: Optional[int] = None
) -> bool:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(
        _create_fines_sync, week_start, cleaner1_id, cleaner1_name, cleaner2_id, cleaner2_name, fine_each, resolved_id
    )

def _get_unpaid_fines_summary_sync(dorm_id: int = 0) -> List[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, user_name, SUM(amount) AS total_unpaid, COUNT(*) AS unpaid_count
            FROM fines
            WHERE dorm_id = ? AND status = 'unpaid'
            GROUP BY user_id, user_name
            ORDER BY total_unpaid DESC;
        """, (dorm_id,))
        return [dict(r) for r in cursor.fetchall()]

async def get_unpaid_fines_summary(dorm_id: Optional[int] = None) -> List[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_unpaid_fines_summary_sync, resolved_id)

def _mark_fines_paid_sync(user_id: int, admin_id: int, dorm_id: int = 0) -> int:
    now_iso = config.get_current_time().isoformat()
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE fines
            SET status = 'paid',
                paid_at = ?,
                marked_paid_by = ?
            WHERE dorm_id = ? AND user_id = ? AND status = 'unpaid';
        """, (now_iso, admin_id, dorm_id, user_id))
        count = cursor.rowcount
        conn.commit()
        return count

async def mark_fines_paid(user_id: int, admin_id: int, dorm_id: Optional[int] = None) -> int:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_mark_fines_paid_sync, user_id, admin_id, resolved_id)

def _get_user_unpaid_total_sync(user_id: int, dorm_id: int = 0) -> int:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM fines
            WHERE dorm_id = ? AND user_id = ? AND status = 'unpaid';
        """, (dorm_id, user_id))
        row = cursor.fetchone()
        return int(row["total"]) if row else 0

async def get_user_unpaid_total(user_id: int, dorm_id: Optional[int] = None) -> int:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_user_unpaid_total_sync, user_id, resolved_id)

# ---------------------------------------------------------------------------
# Key-Value Settings & Milestone Operations
# ---------------------------------------------------------------------------
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

async def get_connection_status(dorm_id: Optional[int] = None) -> tuple:
    """Return (connected_count, total_count, connected_members, pending_members)."""
    resolved_id = _resolve_dorm_id(dorm_id)
    members = await get_dorm_members(resolved_id)
    if not members:
        # Fallback to config.MEMBERS for backward compatibility
        connected_ids = set(await get_connected_user_ids())
        connected = [m for m in config.MEMBERS if m.user_id in connected_ids]
        pending = [m for m in config.MEMBERS if m.user_id not in connected_ids]
        return len(connected), len(config.MEMBERS), connected, pending

    connected_ids = set(await get_connected_user_ids())
    connected_list = [m for m in members if m["user_id"] in connected_ids]
    pending_list = [m for m in members if m["user_id"] not in connected_ids]
    return len(connected_list), len(members), connected_list, pending_list

def _update_member_id_sync(old_id: int, new_id: int) -> None:
    with _get_connection() as conn:
        conn.execute("UPDATE users SET user_id = ? WHERE user_id = ?;", (new_id, old_id))
        conn.execute("UPDATE dorm_members SET user_id = ? WHERE user_id = ?;", (new_id, old_id))
        conn.execute("UPDATE dorm_pairs SET cleaner1_id = ? WHERE cleaner1_id = ?;", (new_id, old_id))
        conn.execute("UPDATE dorm_pairs SET cleaner2_id = ? WHERE cleaner2_id = ?;", (new_id, old_id))
        conn.execute("UPDATE weeks SET cleaner1_id = ? WHERE cleaner1_id = ?;", (new_id, old_id))
        conn.execute("UPDATE weeks SET cleaner2_id = ? WHERE cleaner2_id = ?;", (new_id, old_id))
        conn.execute("UPDATE weeks SET approver_id = ? WHERE approver_id = ?;", (new_id, old_id))
        conn.execute("UPDATE weeks SET photo_submitted_by = ? WHERE photo_submitted_by = ?;", (new_id, old_id))
        conn.execute("UPDATE fines SET user_id = ? WHERE user_id = ?;", (new_id, old_id))
        conn.commit()

async def update_member_id_in_db(old_id: int, new_id: int) -> None:
    await asyncio.to_thread(_update_member_id_sync, old_id, new_id)

# ---------------------------------------------------------------------------
# Initial Dorm Seeding (Smooth Migration of Existing Setup)
# ---------------------------------------------------------------------------
def _seed_default_dorm_sync() -> None:
    """Seed the default dorm from config/members.json into dorms and dorm_members tables."""
    group_id = getattr(config, "GROUP_ID", -1002378440774)
    admin_id = getattr(config, "ADMIN_ID", 6163528558)
    topic_id = getattr(config, "TOPIC_ID", 2031)
    start_date = getattr(config, "START_DATE_STR", "2026-11-23")
    fine_amount = getattr(config, "FINE_TOTAL", 100)

    if not group_id or not admin_id:
        return

    with _get_connection() as conn:
        # Upsert dorm so topic_id and status are always correct and active
        conn.execute("""
            INSERT INTO dorms (dorm_id, topic_id, admin_id, dorm_name, fine_amount, start_date, status, updated_at)
            VALUES (?, ?, ?, 'Dorm Cleaning Schedule', ?, ?, 'active', CURRENT_TIMESTAMP)
            ON CONFLICT(dorm_id) DO UPDATE SET
                topic_id = COALESCE(excluded.topic_id, dorms.topic_id),
                status = 'active',
                updated_at = CURRENT_TIMESTAMP;
        """, (group_id, topic_id, admin_id, fine_amount, start_date))

        # Seed pre-configured members from config.MEMBERS
        for m in getattr(config, "MEMBERS", []):
            is_adm = 1 if m.user_id in (admin_id, 111111111, 6163528558) else 0
            conn.execute("""
                INSERT INTO dorm_members (dorm_id, user_id, name, is_admin)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(dorm_id, user_id) DO UPDATE SET
                    name = excluded.name,
                    is_admin = CASE WHEN excluded.is_admin = 1 THEN 1 ELSE dorm_members.is_admin END;
            """, (group_id, m.user_id, m.name, is_adm))

        # Seed pairs from config.PAIRS if none exist for this dorm
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) as cnt FROM dorm_pairs WHERE dorm_id = ?", (group_id,))
        pair_cnt = cursor.fetchone()["cnt"]
        if pair_cnt == 0:
            pairs = getattr(config, "PAIRS", [])
            for idx, (p1, p2) in enumerate(pairs, start=1):
                conn.execute("""
                    INSERT OR IGNORE INTO dorm_pairs (dorm_id, pair_number, cleaner1_id, cleaner2_id)
                    VALUES (?, ?, ?, ?);
                """, (group_id, idx, p1.user_id, p2.user_id))
                conn.execute(
                    "UPDATE dorm_members SET pair_number = ? WHERE dorm_id = ? AND user_id IN (?, ?)",
                    (idx, group_id, p1.user_id, p2.user_id)
                )

        # Update legacy records in weeks and fines to have group_id
        conn.execute("UPDATE weeks SET dorm_id = ? WHERE dorm_id = 0", (group_id,))
        conn.execute("UPDATE fines SET dorm_id = ? WHERE dorm_id = 0", (group_id,))

        conn.commit()
    logger.info("Default dorm %s automatically seeded into multi-tenant tables.", group_id)

async def seed_default_dorm() -> None:
    await asyncio.to_thread(_seed_default_dorm_sync)
