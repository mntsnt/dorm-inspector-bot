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
# Cryptographically Secure Dorm Code Utilities
# ---------------------------------------------------------------------------
DORM_CODE_CHARSET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

def is_weak_dorm_code(code: Optional[str]) -> bool:
    """Check if a dorm code is missing, sequential, or easily guessable."""
    if not code:
        return True
    clean = str(code).strip().upper()
    if clean in ("DORM-101", "101", "DORM-0001", "0001", "DORM-102", "102"):
        return True
    # If it's just DORM- followed by simple digits (e.g. DORM-101, DORM-4077)
    if clean.startswith("DORM-") and clean[5:].isdigit():
        return True
    if clean.isdigit():
        return True
    # If total length is too short (< 8 chars)
    if len(clean) < 8:
        return True
    return False

def generate_secure_dorm_code(prefix: str = "DORM") -> str:
    """Generate a high-entropy, human-readable cryptographically secure dorm code (e.g. DORM-7K9P-4X2M)."""
    import secrets
    part1 = "".join(secrets.choice(DORM_CODE_CHARSET) for _ in range(4))
    part2 = "".join(secrets.choice(DORM_CODE_CHARSET) for _ in range(4))
    return f"{prefix}-{part1}-{part2}"

def _generate_unique_dorm_code_sync(cursor, dorm_id: Optional[int] = None) -> str:
    """Generate a collision-free, cryptographically unguessable dorm code."""
    while True:
        code = generate_secure_dorm_code("DORM")
        cursor.execute("SELECT 1 FROM dorms WHERE UPPER(dorm_code) = UPPER(?)", (code,))
        if not cursor.fetchone():
            return code

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
                ai_api_key TEXT,
                dorm_code TEXT,
                cleaners_per_week INTEGER DEFAULT 2,
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
                tsidat_balance INTEGER DEFAULT 0,
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
                cleaner3_id INTEGER DEFAULT 0,
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
                cleaner3_id INTEGER DEFAULT 0,
                approver_id INTEGER NOT NULL,
                announced INTEGER DEFAULT 0,
                announced_at TIMESTAMP,
                reminder_sent INTEGER DEFAULT 0,
                done_reported INTEGER DEFAULT 0,
                photo_file_id TEXT,
                photo_submitted_at TIMESTAMP,
                photo_submitted_by INTEGER,
                photo_status TEXT DEFAULT 'none',
                rating INTEGER DEFAULT 5,
                review_text TEXT,
                check_sent INTEGER DEFAULT 0,
                cleaner1_response TEXT,
                cleaner2_response TEXT,
                cleaner3_response TEXT,
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

        # Migration check: Ensure dorm_id, rating, review_text, cleaner3_id exist on weeks table
        cursor.execute("PRAGMA table_info(weeks);")
        week_cols = [r["name"] for r in cursor.fetchall()]
        if "dorm_id" not in week_cols:
            cursor.execute("ALTER TABLE weeks ADD COLUMN dorm_id INTEGER DEFAULT 0;")
        if "rating" not in week_cols:
            cursor.execute("ALTER TABLE weeks ADD COLUMN rating INTEGER DEFAULT 5;")
        if "review_text" not in week_cols:
            cursor.execute("ALTER TABLE weeks ADD COLUMN review_text TEXT;")
        if "cleaner3_id" not in week_cols:
            cursor.execute("ALTER TABLE weeks ADD COLUMN cleaner3_id INTEGER DEFAULT 0;")
        if "cleaner3_response" not in week_cols:
            cursor.execute("ALTER TABLE weeks ADD COLUMN cleaner3_response TEXT;")

        # Migration check: Ensure cleaner3_id exists on dorm_pairs table
        cursor.execute("PRAGMA table_info(dorm_pairs);")
        dp_cols = [r["name"] for r in cursor.fetchall()]
        if "cleaner3_id" not in dp_cols:
            cursor.execute("ALTER TABLE dorm_pairs ADD COLUMN cleaner3_id INTEGER DEFAULT 0;")

        # Migration check: Ensure tsidat_balance exists on dorm_members table
        cursor.execute("PRAGMA table_info(dorm_members);")
        dm_cols = [r["name"] for r in cursor.fetchall()]
        if "tsidat_balance" not in dm_cols:
            cursor.execute("ALTER TABLE dorm_members ADD COLUMN tsidat_balance INTEGER DEFAULT 0;")

        # Migration check: Ensure ai_api_key, dorm_code, cleaners_per_week exist on dorms table
        cursor.execute("PRAGMA table_info(dorms);")
        dorm_cols = [r["name"] for r in cursor.fetchall()]
        if "ai_api_key" not in dorm_cols:
            cursor.execute("ALTER TABLE dorms ADD COLUMN ai_api_key TEXT;")
        if "dorm_code" not in dorm_cols:
            cursor.execute("ALTER TABLE dorms ADD COLUMN dorm_code TEXT;")
        if "cleaners_per_week" not in dorm_cols:
            cursor.execute("ALTER TABLE dorms ADD COLUMN cleaners_per_week INTEGER DEFAULT 2;")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_dorms_dorm_code ON dorms(dorm_code);")

        # Assign unique cryptographically secure dorm codes to any dorms missing one or having weak codes
        cursor.execute("SELECT dorm_id, dorm_code FROM dorms;")
        existing_dorms = cursor.fetchall()
        for r in existing_dorms:
            d_id = r["dorm_id"]
            cur_code = r["dorm_code"]
            if is_weak_dorm_code(cur_code):
                new_code = _generate_unique_dorm_code_sync(cursor, d_id)
                cursor.execute("UPDATE dorms SET dorm_code = ? WHERE dorm_id = ?", (new_code, d_id))

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

        # 8. Peer Swap Requests Table (One-week temporary duty swaps)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS swap_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dorm_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                requester_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 9. AI Chat History Table (Conversational memory across group and private chats)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dorm_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_history_chat ON ai_chat_history(chat_id, id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_history_dorm ON ai_chat_history(dorm_id, id);")

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
    status: str = "active",
    dorm_code: Optional[str] = None,
    cleaners_per_week: int = 2
) -> Dict[str, Any]:
    if not start_date:
        start_date = getattr(config, "START_DATE_STR", "2026-11-23")

    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT dorm_code FROM dorms WHERE dorm_id = ?", (dorm_id,))
        existing = cursor.fetchone()
        if not dorm_code:
            if existing and existing["dorm_code"] and not is_weak_dorm_code(existing["dorm_code"]):
                dorm_code = existing["dorm_code"]
            else:
                dorm_code = _generate_unique_dorm_code_sync(cursor, dorm_id)

        conn.execute("""
            INSERT INTO dorms (dorm_id, topic_id, admin_id, dorm_name, fine_amount, start_date, status, dorm_code, cleaners_per_week, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(dorm_id) DO UPDATE SET
                topic_id = COALESCE(excluded.topic_id, dorms.topic_id),
                admin_id = excluded.admin_id,
                dorm_name = excluded.dorm_name,
                fine_amount = excluded.fine_amount,
                start_date = COALESCE(excluded.start_date, dorms.start_date),
                status = excluded.status,
                dorm_code = COALESCE(dorms.dorm_code, excluded.dorm_code),
                cleaners_per_week = COALESCE(excluded.cleaners_per_week, dorms.cleaners_per_week, 2),
                updated_at = CURRENT_TIMESTAMP;
        """, (dorm_id, topic_id, admin_id, dorm_name, fine_amount, start_date, status, dorm_code, cleaners_per_week))
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
    status: str = "active",
    dorm_code: Optional[str] = None,
    cleaners_per_week: int = 2
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _create_or_update_dorm_sync, dorm_id, admin_id, dorm_name, topic_id, fine_amount, start_date, status, dorm_code, cleaners_per_week
    )

def _get_dorm_by_code_sync(code: str) -> Optional[Dict[str, Any]]:
    raw = (code or "").replace("`", "").replace("'", "").replace('"', '').replace("#", "").strip()
    clean = raw.upper()
    if not clean:
        return None

    compact = clean.replace("DORM-", "").replace("DORM", "").replace("-", "").replace(" ", "")
    with_prefix = f"DORM-{compact}" if not clean.startswith("DORM-") else clean

    with _get_connection() as conn:
        cursor = conn.cursor()

        # If database is completely empty, auto-seed default dorm first
        cursor.execute("SELECT COUNT(*) AS cnt FROM dorms;")
        if cursor.fetchone()["cnt"] == 0:
            _seed_default_dorm_sync()

        # 1. Direct match by exact code or with DORM- prefix
        cursor.execute("""
            SELECT * FROM dorms
            WHERE UPPER(dorm_code) = ? OR UPPER(dorm_code) = ?
            LIMIT 1
        """, (clean, with_prefix))
        row = cursor.fetchone()
        if row:
            return dict(row)

        # 2. Match compact stripped characters (ignores hyphens, spaces, and casing)
        if compact:
            cursor.execute("""
                SELECT * FROM dorms
                WHERE REPLACE(REPLACE(REPLACE(UPPER(dorm_code), 'DORM-', ''), 'DORM', ''), '-', '') = ?
                LIMIT 1
            """, (compact,))
            row = cursor.fetchone()
            if row:
                return dict(row)

        # 3. Match code without DORM- prefix (e.g. user entered 'E58A-9P45')
        cursor.execute("""
            SELECT * FROM dorms
            WHERE UPPER(dorm_code) = ? OR UPPER(dorm_code) = ?
            LIMIT 1
        """, (f"DORM-{clean}", f"DORM-{raw}"))
        row = cursor.fetchone()
        if row:
            return dict(row)

        # 4. Check fallback to default dorm for legacy/common code ("DORM-101", "101", "DEFAULT")
        if clean in ("DORM-101", "101", "DORM101", "DEFAULT") or compact in ("101", "DEFAULT"):
            default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
            cursor.execute("SELECT * FROM dorms WHERE dorm_id = ?", (default_gid,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            cursor.execute("SELECT * FROM dorms WHERE status = 'active' LIMIT 1")
            row = cursor.fetchone()
            if row:
                return dict(row)

        # 5. Direct match if code is numeric dorm_id (for internal/admin convenience)
        try:
            num_id = int(code.strip())
            cursor.execute("SELECT * FROM dorms WHERE dorm_id = ? LIMIT 1", (num_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        except ValueError:
            pass

        return None

async def get_dorm_by_code(code: str) -> Optional[Dict[str, Any]]:
    """Look up a dorm by its human-readable dorm code (e.g. 'DORM-101' or '101')."""
    return await asyncio.to_thread(_get_dorm_by_code_sync, code)

def _set_dorm_code_sync(dorm_id: int, new_code: str) -> Tuple[bool, str]:
    clean = (new_code or "").strip().upper()
    if len(clean) < 3 or len(clean) > 24:
        return False, "Dorm code must be between 3 and 24 characters."
    import re
    if not re.match(r"^[A-Z0-9\-_]+$", clean):
        return False, "Dorm code can only contain letters, numbers, hyphens, and underscores."
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT dorm_id FROM dorms WHERE UPPER(dorm_code) = ? AND dorm_id != ?", (clean, dorm_id))
        if cursor.fetchone():
            return False, f"Dorm code '{clean}' is already taken by another dorm."
        cursor.execute("UPDATE dorms SET dorm_code = ?, updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (clean, dorm_id))
        conn.commit()
        return True, clean

async def set_dorm_code(dorm_id: int, new_code: str) -> Tuple[bool, str]:
    """Set or update a custom dorm code for a dorm."""
    return await asyncio.to_thread(_set_dorm_code_sync, dorm_id, new_code)

def _get_dorm_sync(dorm_id: int) -> Optional[Dict[str, Any]]:
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    group_variants = (default_gid, getattr(config, "GROUP_ID", 0), 0, -2378440774, 2378440774)
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorms WHERE dorm_id = ?", (dorm_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        if dorm_id in group_variants:
            _seed_default_dorm_sync()
            cursor.execute("SELECT * FROM dorms WHERE dorm_id = ?", (dorm_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            cursor.execute("SELECT * FROM dorms WHERE dorm_id = ?", (default_gid,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        return None

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
    cleaners_per_week: Optional[int] = None,
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
        if cleaners_per_week is not None:
            conn.execute("UPDATE dorms SET cleaners_per_week = ?, updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (cleaners_per_week, dorm_id))
        conn.commit()

async def update_dorm_settings(
    dorm_id: int,
    dorm_name: Optional[str] = None,
    fine_amount: Optional[int] = None,
    topic_id: Optional[int] = None,
    start_date: Optional[str] = None,
    cleaners_per_week: Optional[int] = None,
) -> None:
    await asyncio.to_thread(_update_dorm_settings_sync, dorm_id, dorm_name, fine_amount, topic_id, start_date, cleaners_per_week)

def _set_dorm_ai_key_sync(dorm_id: int, api_key: Optional[str]) -> None:
    with _get_connection() as conn:
        conn.execute("UPDATE dorms SET ai_api_key = ?, updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (api_key, dorm_id))
        conn.commit()

async def set_dorm_ai_key(dorm_id: int, api_key: Optional[str]) -> None:
    await asyncio.to_thread(_set_dorm_ai_key_sync, dorm_id, api_key)

def _get_dorm_ai_key_sync(dorm_id: int) -> Optional[str]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT ai_api_key FROM dorms WHERE dorm_id = ?", (dorm_id,))
        row = cursor.fetchone()
        if row and row["ai_api_key"]:
            return row["ai_api_key"].strip()
        return None

async def get_dorm_ai_key(dorm_id: int) -> Optional[str]:
    return await asyncio.to_thread(_get_dorm_ai_key_sync, dorm_id)

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
        # Also sync chat_id and username to any dorm_members records for this user
        if chat_id:
            conn.execute("UPDATE dorm_members SET chat_id = ? WHERE user_id = ?", (chat_id, user_id))
        if username:
            conn.execute("UPDATE dorm_members SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()

async def upsert_user(user_id: int, name: str, chat_id: Optional[int] = None, username: Optional[str] = None) -> None:
    await asyncio.to_thread(_upsert_user_sync, user_id, name, chat_id, username)

def _update_member_name_sync(user_id: int, new_name: str, dorm_id: Optional[int] = None) -> None:
    with _get_connection() as conn:
        conn.execute("UPDATE users SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?", (new_name, user_id))
        if dorm_id:
            conn.execute("UPDATE dorm_members SET name = ? WHERE user_id = ? AND dorm_id = ?", (new_name, user_id, dorm_id))
        else:
            conn.execute("UPDATE dorm_members SET name = ? WHERE user_id = ?", (new_name, user_id))
        conn.commit()

async def update_member_name(user_id: int, new_name: str, dorm_id: Optional[int] = None) -> None:
    await asyncio.to_thread(_update_member_name_sync, user_id, new_name, dorm_id)

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
) -> Tuple[bool, str]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        # Enforce strict single-dorm membership:
        # Check if user is already an active member of another active dorm
        cursor.execute("""
            SELECT d.dorm_id, d.dorm_name
            FROM dorm_members dm
            JOIN dorms d ON dm.dorm_id = d.dorm_id
            WHERE dm.user_id = ? AND dm.dorm_id != ? AND d.status = 'active';
        """, (user_id, dorm_id))
        existing = cursor.fetchone()
        if existing:
            return False, f"You are already a member of {existing['dorm_name']}."

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
        return True, "Success"

async def add_dorm_member(
    dorm_id: int,
    user_id: int,
    name: str,
    chat_id: Optional[int] = None,
    username: Optional[str] = None,
    is_admin: int = 0
) -> Tuple[bool, str]:
    return await asyncio.to_thread(_add_dorm_member_sync, dorm_id, user_id, name, chat_id, username, is_admin)

def _remove_dorm_member_sync(dorm_id: int, user_id: int) -> None:
    with _get_connection() as conn:
        conn.execute("DELETE FROM dorm_members WHERE dorm_id = ? AND user_id = ?", (dorm_id, user_id))
        conn.commit()

async def remove_dorm_member(dorm_id: int, user_id: int) -> None:
    await asyncio.to_thread(_remove_dorm_member_sync, dorm_id, user_id)

def _leave_dorm_sync(dorm_id: int, user_id: int) -> Tuple[bool, str]:
    """Cleanly remove a member from a dorm with duty checks and pair maintenance."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorm_members WHERE dorm_id = ? AND user_id = ?", (dorm_id, user_id))
        member = cursor.fetchone()
        if not member:
            return False, "You are not a member of this dorm."

        cursor.execute("SELECT * FROM dorms WHERE dorm_id = ?", (dorm_id,))
        dorm = cursor.fetchone()
        dorm_name = dorm["dorm_name"] if dorm else "Dorm"

        # Check if user is currently scheduled for cleaning duty this week
        cursor.execute("""
            SELECT * FROM weeks 
            WHERE dorm_id = ? AND (cleaner1_id = ? OR cleaner2_id = ? OR cleaner3_id = ?) 
              AND final_status = 'pending'
            ORDER BY week_start DESC LIMIT 1;
        """, (dorm_id, user_id, user_id, user_id))
        active_week = cursor.fetchone()
        if active_week:
            return False, "You cannot leave while scheduled on duty this week. Complete cleaning or swap duty (/swap) first."

        # Delete member from dorm_members
        conn.execute("DELETE FROM dorm_members WHERE dorm_id = ? AND user_id = ?", (dorm_id, user_id))

        # Check remaining members
        cursor.execute("SELECT * FROM dorm_members WHERE dorm_id = ? ORDER BY joined_at ASC", (dorm_id,))
        remaining = cursor.fetchall()

        if not remaining:
            conn.execute("UPDATE dorms SET status = 'inactive', updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (dorm_id,))
        elif member["is_admin"] == 1:
            cursor.execute("SELECT * FROM dorm_members WHERE dorm_id = ? AND is_admin = 1", (dorm_id,))
            has_other_admin = cursor.fetchone()
            if not has_other_admin:
                new_admin_id = remaining[0]["user_id"]
                conn.execute("UPDATE dorm_members SET is_admin = 1 WHERE dorm_id = ? AND user_id = ?", (dorm_id, new_admin_id))
                conn.execute("UPDATE dorms SET admin_id = ?, updated_at = CURRENT_TIMESTAMP WHERE dorm_id = ?", (new_admin_id, dorm_id))

        # Cleanup dorm_pairs
        cursor.execute("SELECT * FROM dorm_pairs WHERE dorm_id = ? AND (cleaner1_id = ? OR cleaner2_id = ? OR cleaner3_id = ?)", (dorm_id, user_id, user_id, user_id))
        for p in cursor.fetchall():
            p_num = p["pair_number"]
            c1 = p["cleaner1_id"] if p["cleaner1_id"] != user_id else 0
            c2 = p["cleaner2_id"] if p["cleaner2_id"] != user_id else 0
            c3 = p.get("cleaner3_id", 0) if p.get("cleaner3_id", 0) != user_id else 0
            if c1 == 0 and c2 == 0 and c3 == 0:
                conn.execute("DELETE FROM dorm_pairs WHERE dorm_id = ? AND pair_number = ?", (dorm_id, p_num))
            else:
                conn.execute("UPDATE dorm_pairs SET cleaner1_id = ?, cleaner2_id = ?, cleaner3_id = ? WHERE dorm_id = ? AND pair_number = ?", (c1, c2, c3, dorm_id, p_num))

        conn.commit()
        return True, f"Successfully left {dorm_name}."

async def leave_dorm(dorm_id: int, user_id: int) -> Tuple[bool, str]:
    return await asyncio.to_thread(_leave_dorm_sync, dorm_id, user_id)

def _get_dorm_members_sync(dorm_id: int) -> List[Dict[str, Any]]:
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT dm.dorm_id, dm.user_id, dm.name, dm.chat_id, 
                   COALESCE(dm.username, u.username) AS username, 
                   dm.is_admin, dm.pair_number, dm.joined_at
            FROM dorm_members dm
            LEFT JOIN users u ON dm.user_id = u.user_id
            WHERE dm.dorm_id = ?
            ORDER BY dm.joined_at ASC
        """, (dorm_id,))
        rows = cursor.fetchall()
        if rows:
            return [dict(r) for r in rows]
        if dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0):
            _seed_default_dorm_sync()
            cursor.execute("""
                SELECT dm.dorm_id, dm.user_id, dm.name, dm.chat_id, 
                       COALESCE(dm.username, u.username) AS username, 
                       dm.is_admin, dm.pair_number, dm.joined_at
                FROM dorm_members dm
                LEFT JOIN users u ON dm.user_id = u.user_id
                WHERE dm.dorm_id = ?
                ORDER BY dm.joined_at ASC
            """, (dorm_id,))
            return [dict(r) for r in cursor.fetchall()]
        return []

async def get_dorm_members(dorm_id: Optional[int] = None) -> List[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_dorm_members_sync, resolved_id)

def _get_dorm_member_sync(dorm_id: int, user_id: int) -> Optional[Dict[str, Any]]:
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT dm.dorm_id, dm.user_id, dm.name, dm.chat_id, 
                   COALESCE(dm.username, u.username) AS username, 
                   dm.is_admin, dm.pair_number, dm.joined_at
            FROM dorm_members dm
            LEFT JOIN users u ON dm.user_id = u.user_id
            WHERE dm.dorm_id = ? AND dm.user_id = ?
        """, (dorm_id, user_id))
        row = cursor.fetchone()
        if row:
            return dict(row)

        if (dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)) and config.is_member(user_id):
            cm = config.get_member(user_id)
            if cm:
                _seed_default_dorm_sync()
                cursor.execute("""
                    SELECT dm.dorm_id, dm.user_id, dm.name, dm.chat_id, 
                           COALESCE(dm.username, u.username) AS username, 
                           dm.is_admin, dm.pair_number, dm.joined_at
                    FROM dorm_members dm
                    LEFT JOIN users u ON dm.user_id = u.user_id
                    WHERE dm.dorm_id = ? AND dm.user_id = ?
                """, (dorm_id, user_id))
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return {
                    "dorm_id": dorm_id,
                    "user_id": cm.user_id,
                    "name": cm.name,
                    "is_admin": 1 if cm.user_id in (config.ADMIN_ID, 6163528558) else 0,
                    "pair_number": None,
                    "chat_id": cm.user_id,
                    "username": None
                }
        return None

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
def _set_dorm_pairs_sync(dorm_id: int, pairs: List[Any]) -> None:
    with _get_connection() as conn:
        conn.execute("DELETE FROM dorm_pairs WHERE dorm_id = ?", (dorm_id,))
        for idx, p in enumerate(pairs, start=1):
            c1 = p[0] if len(p) > 0 else 0
            c2 = p[1] if len(p) > 1 else 0
            c3 = p[2] if len(p) > 2 else 0
            conn.execute("""
                INSERT INTO dorm_pairs (dorm_id, pair_number, cleaner1_id, cleaner2_id, cleaner3_id)
                VALUES (?, ?, ?, ?, ?);
            """, (dorm_id, idx, c1, c2, c3))
            active_ids = [uid for uid in (c1, c2, c3) if uid]
            if active_ids:
                qmarks = ",".join("?" for _ in active_ids)
                conn.execute(f"UPDATE dorm_members SET pair_number = ? WHERE dorm_id = ? AND user_id IN ({qmarks})", (idx, dorm_id, *active_ids))
        conn.commit()

async def set_dorm_pairs(dorm_id: int, pairs: List[Any]) -> None:
    await asyncio.to_thread(_set_dorm_pairs_sync, dorm_id, pairs)

def _get_dorm_pairs_sync(dorm_id: int) -> List[Dict[str, Any]]:
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM dorm_pairs WHERE dorm_id = ? ORDER BY pair_number ASC", (dorm_id,))
        rows = cursor.fetchall()
        if rows:
            return [dict(r) for r in rows]
        if dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0):
            _seed_default_dorm_sync()
            cursor.execute("SELECT * FROM dorm_pairs WHERE dorm_id = ? ORDER BY pair_number ASC", (dorm_id,))
            return [dict(r) for r in cursor.fetchall()]
        return []

async def get_dorm_pairs(dorm_id: Optional[int] = None) -> List[Dict[str, Any]]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(_get_dorm_pairs_sync, resolved_id)

def _auto_generate_dorm_pairs_sync(dorm_id: int) -> List[Tuple[int, ...]]:
    """Automatically group registered dorm members into 1, 2, or 3 cleaners per week based on dorm settings."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM dorm_members WHERE dorm_id = ? ORDER BY joined_at ASC", (dorm_id,))
        members = [r["user_id"] for r in cursor.fetchall()]
        cursor.execute("SELECT cleaners_per_week FROM dorms WHERE dorm_id = ?", (dorm_id,))
        d_row = cursor.fetchone()
        team_size = d_row["cleaners_per_week"] if (d_row and d_row["cleaners_per_week"]) else 2

    if not members:
        return []

    if team_size not in (1, 2, 3):
        team_size = 2

    if len(members) < team_size:
        team_size = max(1, len(members))

    teams: List[Tuple[int, ...]] = []
    if team_size == 1:
        for u in members:
            teams.append((u, 0, 0))
    elif team_size == 2:
        for i in range(0, len(members) - 1, 2):
            teams.append((members[i], members[i + 1], 0))
        if len(members) % 2 != 0:
            teams.append((members[-1], members[0], 0))
    elif team_size == 3:
        for i in range(0, len(members), 3):
            chunk = members[i:i+3]
            if len(chunk) == 3:
                teams.append((chunk[0], chunk[1], chunk[2]))
            elif len(chunk) == 2:
                teams.append((chunk[0], chunk[1], members[0]))
            elif len(chunk) == 1:
                first = members[0]
                second = members[1] if len(members) > 1 else 0
                teams.append((chunk[0], first, second))

    _set_dorm_pairs_sync(dorm_id, teams)
    return teams

async def auto_generate_dorm_pairs(dorm_id: int) -> List[Tuple[int, ...]]:
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
    dorm_id: int = 0,
    cleaner3_id: int = 0
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
                dorm_id, week_start, week_number, cleaner1_id, cleaner2_id, cleaner3_id, approver_id, final_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending');
        """, (dorm_id, week_start, week_number, cleaner1_id, cleaner2_id, cleaner3_id, approver_id))
        conn.commit()

        cursor.execute("SELECT * FROM weeks WHERE dorm_id = ? AND week_start = ?", (dorm_id, week_start))
        return dict(cursor.fetchone())

async def get_or_create_week(
    week_start: str,
    week_number: int,
    cleaner1_id: int,
    cleaner2_id: int,
    eligible_approver_ids: List[int],
    dorm_id: Optional[int] = None,
    cleaner3_id: int = 0
) -> Dict[str, Any]:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(
        _get_or_create_week_sync, week_start, week_number, cleaner1_id, cleaner2_id, eligible_approver_ids, resolved_id, cleaner3_id
    )
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

def _update_photo_status_sync(
    week_start: str,
    status: str,
    final_status: Optional[str] = None,
    dorm_id: int = 0,
    rating: Optional[int] = None,
    review_text: Optional[str] = None
) -> None:
    now_iso = config.get_current_time().isoformat()
    with _get_connection() as conn:
        if final_status:
            conn.execute("""
                UPDATE weeks
                SET photo_status = ?,
                    final_status = ?,
                    rating = COALESCE(?, rating, 5),
                    review_text = COALESCE(?, review_text),
                    approver_final_decision = CASE WHEN ? = 'approved' THEN 'cleaned' ELSE approver_final_decision END,
                    approver_decided_at = ?
                WHERE dorm_id = ? AND week_start = ?;
            """, (status, final_status, rating, review_text, status, now_iso, dorm_id, week_start))
        else:
            conn.execute("""
                UPDATE weeks
                SET photo_status = ?
                WHERE dorm_id = ? AND week_start = ?;
            """, (status, dorm_id, week_start))
        conn.commit()

async def update_photo_status(
    week_start: str,
    status: str,
    final_status: Optional[str] = None,
    dorm_id: Optional[int] = None,
    rating: Optional[int] = None,
    review_text: Optional[str] = None
) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_update_photo_status_sync, week_start, status, final_status, resolved_id, rating, review_text)

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
        cursor.execute("SELECT cleaner1_id, cleaner2_id, cleaner3_id FROM weeks WHERE dorm_id = ? AND week_start = ?", (dorm_id, week_start))
        row = cursor.fetchone()
        if not row:
            return

        if cleaner_id == row["cleaner1_id"]:
            col = "cleaner1_response"
        elif cleaner_id == row["cleaner2_id"]:
            col = "cleaner2_response"
        elif row.get("cleaner3_id") and cleaner_id == row["cleaner3_id"]:
            col = "cleaner3_response"
        else:
            return

        conn.execute(f"""
            UPDATE weeks
            SET {col} = ?, cleaners_answered_at = ?
            WHERE dorm_id = ? AND week_start = ?;
        """, (answer, now_iso, dorm_id, week_start))
        conn.commit()

async def record_cleaner_response(week_start: str, cleaner_id: int, answer: str, dorm_id: Optional[int] = None) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_record_cleaner_response_sync, week_start, cleaner_id, answer, resolved_id)

def _record_approver_decision_sync(
    week_start: str,
    decision: str,
    final_status: str,
    dorm_id: int = 0,
    rating: Optional[int] = None,
    review_text: Optional[str] = None
) -> None:
    now_iso = config.get_current_time().isoformat()
    with _get_connection() as conn:
        conn.execute("""
            UPDATE weeks
            SET approver_final_decision = ?,
                approver_decided_at = ?,
                final_status = ?,
                rating = COALESCE(?, rating, 5),
                review_text = COALESCE(?, review_text)
            WHERE dorm_id = ? AND week_start = ?;
        """, (decision, now_iso, final_status, rating, review_text, dorm_id, week_start))
        conn.commit()

async def record_approver_decision(
    week_start: str,
    decision: str,
    final_status: str,
    dorm_id: Optional[int] = None,
    rating: Optional[int] = None,
    review_text: Optional[str] = None
) -> None:
    resolved_id = _resolve_dorm_id(dorm_id)
    await asyncio.to_thread(_record_approver_decision_sync, week_start, decision, final_status, resolved_id, rating, review_text)
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
    dorm_id: int = 0,
    cleaner3_id: int = 0,
    cleaner3_name: str = ""
) -> bool:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cleaners = [(cleaner1_id, cleaner1_name)]
        if cleaner2_id:
            cleaners.append((cleaner2_id, cleaner2_name))
        if cleaner3_id:
            cleaners.append((cleaner3_id, cleaner3_name))

        for cid, cname in cleaners:
            if cid:
                cursor.execute("""
                    INSERT OR IGNORE INTO fines (dorm_id, week_start, user_id, user_name, amount, status)
                    VALUES (?, ?, ?, ?, ?, 'unpaid');
                """, (dorm_id, week_start, cid, cname, fine_each))

        conn.commit()
    names_str = " & ".join(c[1] for c in cleaners if c[0])
    logger.info("Fines recorded for dorm %s, week %s: %s (%d ETB each)", dorm_id, week_start, names_str, fine_each)
    return True

async def create_fines_for_week(
    week_start: str,
    cleaner1_id: int,
    cleaner1_name: str,
    cleaner2_id: int,
    cleaner2_name: str,
    fine_each: int,
    dorm_id: Optional[int] = None,
    cleaner3_id: int = 0,
    cleaner3_name: str = ""
) -> bool:
    resolved_id = _resolve_dorm_id(dorm_id)
    return await asyncio.to_thread(
        _create_fines_sync, week_start, cleaner1_id, cleaner1_name, cleaner2_id, cleaner2_name, fine_each, resolved_id, cleaner3_id, cleaner3_name
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

# ---------------------------------------------------------------------------
# Tsidat Token Operations
# ---------------------------------------------------------------------------
def _add_tsidat_tokens_sync(dorm_id: int, user_id: int, amount: int) -> int:
    """Award Tsidat tokens to a dorm member and return their new balance."""
    with _get_connection() as conn:
        conn.execute("""
            UPDATE dorm_members
            SET tsidat_balance = COALESCE(tsidat_balance, 0) + ?
            WHERE dorm_id = ? AND user_id = ?;
        """, (amount, dorm_id, user_id))
        conn.commit()
        cursor = conn.cursor()
        cursor.execute("SELECT tsidat_balance FROM dorm_members WHERE dorm_id = ? AND user_id = ?", (dorm_id, user_id))
        row = cursor.fetchone()
        return row["tsidat_balance"] if row and row["tsidat_balance"] is not None else amount

async def add_tsidat_tokens(dorm_id: int, user_id: int, amount: int) -> int:
    return await asyncio.to_thread(_add_tsidat_tokens_sync, dorm_id, user_id, amount)

def _get_tsidat_balance_sync(dorm_id: int, user_id: int) -> int:
    """Get the current Tsidat token balance for a member."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT tsidat_balance FROM dorm_members WHERE dorm_id = ? AND user_id = ?", (dorm_id, user_id))
        row = cursor.fetchone()
        return row["tsidat_balance"] if row and row["tsidat_balance"] is not None else 0

async def get_tsidat_balance(dorm_id: int, user_id: int) -> int:
    return await asyncio.to_thread(_get_tsidat_balance_sync, dorm_id, user_id)

def _get_tsidat_leaderboard_sync(dorm_id: int) -> List[Dict[str, Any]]:
    """Return all members in a dorm sorted by highest Tsidat token balance."""
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, name, COALESCE(tsidat_balance, 0) AS tsidat_balance
            FROM dorm_members
            WHERE dorm_id = ?
            ORDER BY tsidat_balance DESC, name ASC;
        """, (dorm_id,))
        rows = cursor.fetchall()
        if rows:
            return [dict(r) for r in rows]
        if dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0):
            _seed_default_dorm_sync()
            cursor.execute("""
                SELECT user_id, name, COALESCE(tsidat_balance, 0) AS tsidat_balance
                FROM dorm_members
                WHERE dorm_id = ?
                ORDER BY tsidat_balance DESC, name ASC;
            """, (dorm_id,))
            return [dict(r) for r in cursor.fetchall()]
        return []

async def get_tsidat_leaderboard(dorm_id: int) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_get_tsidat_leaderboard_sync, dorm_id)

class MemberItem(dict):
    """Dictionary supporting both dict item lookup (m['user_id']) and attribute lookup (m.user_id)."""
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"'MemberItem' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

async def get_connection_status(dorm_id: Optional[int] = None) -> tuple:
    """Return (connected_count, total_count, connected_members, pending_members)."""
    resolved_id = _resolve_dorm_id(dorm_id)
    members = await get_dorm_members(resolved_id)
    if not members:
        if resolved_id == getattr(config, "DEFAULT_GROUP_ID", -1002378440774):
            connected_ids = set(await get_connected_user_ids())
            connected = [MemberItem(m) if isinstance(m, dict) else m for m in config.MEMBERS if m.user_id in connected_ids]
            pending = [MemberItem(m) if isinstance(m, dict) else m for m in config.MEMBERS if m.user_id not in connected_ids]
            return len(connected), len(config.MEMBERS), connected, pending
        return 0, 0, [], []

    connected_ids = set(await get_connected_user_ids())
    connected_list = [MemberItem(m) if isinstance(m, dict) else m for m in members if (m["user_id"] if isinstance(m, dict) else m.user_id) in connected_ids]
    pending_list = [MemberItem(m) if isinstance(m, dict) else m for m in members if (m["user_id"] if isinstance(m, dict) else m.user_id) not in connected_ids]
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
        # Migrate any legacy placeholder ID 111111111 for Bilisuma to 7220854785
        conn.execute("UPDATE dorm_members SET user_id = 7220854785 WHERE user_id = 111111111;")
        conn.execute("UPDATE dorm_pairs SET cleaner1_id = 7220854785 WHERE cleaner1_id = 111111111;")
        conn.execute("UPDATE dorm_pairs SET cleaner2_id = 7220854785 WHERE cleaner2_id = 111111111;")
        conn.execute("UPDATE weeks SET cleaner1_id = 7220854785 WHERE cleaner1_id = 111111111;")
        conn.execute("UPDATE weeks SET cleaner2_id = 7220854785 WHERE cleaner2_id = 111111111;")
        conn.execute("UPDATE weeks SET approver_id = 7220854785 WHERE approver_id = 111111111;")
        conn.execute("UPDATE users SET user_id = 7220854785 WHERE user_id = 111111111;")
        conn.execute("UPDATE fines SET user_id = 7220854785 WHERE user_id = 111111111;")

        cursor = conn.cursor()
        cursor.execute("SELECT dorm_code FROM dorms WHERE dorm_id = ?", (group_id,))
        row = cursor.fetchone()
        existing_code = row["dorm_code"] if row else None
        if not existing_code or is_weak_dorm_code(existing_code):
            default_code = _generate_unique_dorm_code_sync(cursor, group_id)
        else:
            default_code = existing_code

        # Upsert dorm so topic_id, dorm_code, and status are always correct and active
        conn.execute("""
            INSERT INTO dorms (dorm_id, topic_id, admin_id, dorm_name, fine_amount, start_date, status, dorm_code, updated_at)
            VALUES (?, ?, ?, 'Dorm Cleaning Schedule', ?, ?, 'active', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(dorm_id) DO UPDATE SET
                topic_id = COALESCE(excluded.topic_id, dorms.topic_id),
                dorm_code = CASE WHEN dorms.dorm_code IS NULL OR dorms.dorm_code = '' OR dorms.dorm_code = 'DORM-101' THEN excluded.dorm_code ELSE dorms.dorm_code END,
                status = 'active',
                updated_at = CURRENT_TIMESTAMP;
        """, (group_id, topic_id, admin_id, fine_amount, start_date, default_code))

        # Seed pre-configured members from config.MEMBERS as already connected
        for m in getattr(config, "MEMBERS", []):
            is_adm = 1 if m.user_id in (admin_id, 7220854785, 6163528558) else 0
            conn.execute("""
                INSERT INTO dorm_members (dorm_id, user_id, name, chat_id, is_admin)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(dorm_id, user_id) DO UPDATE SET
                    name = excluded.name,
                    chat_id = COALESCE(dorm_members.chat_id, excluded.chat_id, excluded.user_id),
                    is_admin = CASE WHEN excluded.is_admin = 1 THEN 1 ELSE dorm_members.is_admin END;
            """, (group_id, m.user_id, m.name, m.user_id, is_adm))

            # Ensure users table has each member tracked as connected
            conn.execute("""
                INSERT INTO users (user_id, name, chat_id, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    name = excluded.name,
                    chat_id = COALESCE(users.chat_id, excluded.chat_id, excluded.user_id),
                    updated_at = CURRENT_TIMESTAMP;
            """, (m.user_id, m.name, m.user_id))

            conn.execute("UPDATE dorm_members SET chat_id = ? WHERE dorm_id = ? AND user_id = ? AND chat_id IS NULL", (m.user_id, group_id, m.user_id))
            conn.execute("UPDATE users SET chat_id = ? WHERE user_id = ? AND chat_id IS NULL", (m.user_id, m.user_id))

        # Mark all_connected milestone as achieved for default dorm so it never starts over
        conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, '1', CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = CURRENT_TIMESTAMP;
        """, (f"all_connected_{group_id}",))

        # Enforce exact saved pairs from members.json / config.PAIRS for default dorm:
        # Pair 1: Bilisuma & Nahom
        # Pair 2: Hassan & Mintesnot
        # Pair 3: Dawit & Zeftret
        # Pair 4: Ahmed & Yonatan
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, name FROM dorm_members WHERE dorm_id = ?", (group_id,))
        name_to_id = {r["name"].strip().lower(): r["user_id"] for r in cursor.fetchall()}

        pairs = getattr(config, "PAIRS", [])
        conn.execute("DELETE FROM dorm_pairs WHERE dorm_id = ?", (group_id,))
        for idx, (p1, p2) in enumerate(pairs, start=1):
            c1_id = name_to_id.get(p1.name.strip().lower(), p1.user_id)
            c2_id = name_to_id.get(p2.name.strip().lower(), p2.user_id)
            conn.execute("""
                INSERT INTO dorm_pairs (dorm_id, pair_number, cleaner1_id, cleaner2_id)
                VALUES (?, ?, ?, ?);
            """, (group_id, idx, c1_id, c2_id))
            conn.execute(
                "UPDATE dorm_members SET pair_number = ? WHERE dorm_id = ? AND user_id IN (?, ?)",
                (idx, group_id, c1_id, c2_id)
            )

        # Pre-create Week 1 record in weeks table if not present so Week 1 duty is immediately active
        cursor.execute("SELECT * FROM weeks WHERE dorm_id = ? AND week_start = ?", (group_id, "2026-11-23"))
        if not cursor.fetchone():
            conn.execute("""
                INSERT INTO weeks (
                    dorm_id, week_start, week_number, cleaner1_id, cleaner2_id,
                    approver_id, announced, reminder_sent, done_reported, photo_status,
                    check_sent, final_status, timeout_processed, created_at
                )
                VALUES (?, '2026-11-23', 1, 7220854785, 1758752115, 1359460823, 0, 0, 0, 'none', 0, 'pending', 0, CURRENT_TIMESTAMP);
            """, (group_id,))

        # Update legacy records in weeks and fines to have group_id
        conn.execute("UPDATE weeks SET dorm_id = ? WHERE dorm_id = 0", (group_id,))
        conn.execute("UPDATE fines SET dorm_id = ? WHERE dorm_id = 0", (group_id,))

        conn.commit()
    logger.info("Default dorm %s automatically seeded into multi-tenant tables.", group_id)

async def seed_default_dorm() -> None:
    await asyncio.to_thread(_seed_default_dorm_sync)

def _seed_default_dorm_for_group_sync(group_id: int) -> Dict[str, Any]:
    """Seed the saved default dorm members, pairs, and settings to a specific group chat ID."""
    _seed_default_dorm_sync()
    admin_id = getattr(config, "ADMIN_ID", 6163528558)
    topic_id = getattr(config, "TOPIC_ID", 2031)
    start_date = getattr(config, "START_DATE_STR", "2026-11-23")
    fine_amount = getattr(config, "FINE_TOTAL", 100)

    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT dorm_code FROM dorms WHERE dorm_id = ?", (group_id,))
        row = cursor.fetchone()
        existing_code = row["dorm_code"] if row else None
        if not existing_code or is_weak_dorm_code(existing_code):
            default_code = _generate_unique_dorm_code_sync(cursor, group_id)
        else:
            default_code = existing_code

        conn.execute("""
            INSERT INTO dorms (dorm_id, topic_id, admin_id, dorm_name, fine_amount, start_date, status, dorm_code, updated_at)
            VALUES (?, ?, ?, 'Dorm Cleaning Schedule', ?, ?, 'active', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(dorm_id) DO UPDATE SET
                topic_id = COALESCE(excluded.topic_id, dorms.topic_id),
                dorm_code = CASE WHEN dorms.dorm_code IS NULL OR dorms.dorm_code = '' OR dorms.dorm_code = 'DORM-101' THEN excluded.dorm_code ELSE dorms.dorm_code END,
                status = 'active',
                updated_at = CURRENT_TIMESTAMP;
        """, (group_id, topic_id, admin_id, fine_amount, start_date, default_code))

        for m in getattr(config, "MEMBERS", []):
            is_adm = 1 if m.user_id in (admin_id, 7220854785, 6163528558) else 0
            conn.execute("""
                INSERT INTO dorm_members (dorm_id, user_id, name, chat_id, is_admin)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(dorm_id, user_id) DO UPDATE SET
                    name = excluded.name,
                    chat_id = COALESCE(dorm_members.chat_id, excluded.chat_id, excluded.user_id),
                    is_admin = CASE WHEN excluded.is_admin = 1 THEN 1 ELSE dorm_members.is_admin END;
            """, (group_id, m.user_id, m.name, m.user_id, is_adm))

            conn.execute("""
                INSERT INTO users (user_id, name, chat_id, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    name = excluded.name,
                    chat_id = COALESCE(users.chat_id, excluded.chat_id, excluded.user_id),
                    updated_at = CURRENT_TIMESTAMP;
            """, (m.user_id, m.name, m.user_id))

        pairs = getattr(config, "PAIRS", [])
        conn.execute("DELETE FROM dorm_pairs WHERE dorm_id = ?", (group_id,))
        for idx, (p1, p2) in enumerate(pairs, start=1):
            conn.execute("""
                INSERT INTO dorm_pairs (dorm_id, pair_number, cleaner1_id, cleaner2_id)
                VALUES (?, ?, ?, ?);
            """, (group_id, idx, p1.user_id, p2.user_id))
            conn.execute(
                "UPDATE dorm_members SET pair_number = ? WHERE dorm_id = ? AND user_id IN (?, ?)",
                (idx, group_id, p1.user_id, p2.user_id)
            )

        cursor = conn.cursor()
        cursor.execute("SELECT * FROM weeks WHERE dorm_id = ? AND week_start = ?", (group_id, "2026-11-23"))
        if not cursor.fetchone():
            conn.execute("""
                INSERT INTO weeks (
                    dorm_id, week_start, week_number, cleaner1_id, cleaner2_id,
                    approver_id, announced, reminder_sent, done_reported, photo_status,
                    check_sent, final_status, timeout_processed, created_at
                )
                VALUES (?, '2026-11-23', 1, 7220854785, 1758752115, 1359460823, 0, 0, 0, 'none', 0, 'pending', 0, CURRENT_TIMESTAMP);
            """, (group_id,))

        conn.execute("""
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, '1', CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = '1', updated_at = CURRENT_TIMESTAMP;
        """, (f"all_connected_{group_id}",))

        conn.commit()

        cursor.execute("SELECT * FROM dorms WHERE dorm_id = ?", (group_id,))
        return dict(cursor.fetchone())

async def seed_default_dorm_for_group(group_id: int) -> Dict[str, Any]:
    return await asyncio.to_thread(_seed_default_dorm_for_group_sync, group_id)

# ---------------------------------------------------------------------------
# Peer Duty Swap Operations (One-Week Temporary Overrides)
# ---------------------------------------------------------------------------
def _create_swap_request_sync(dorm_id: int, week_start: str, requester_id: int, target_id: int) -> int:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE swap_requests SET status = 'cancelled'
            WHERE dorm_id = ? AND week_start = ? AND requester_id = ? AND status = 'pending';
        """, (dorm_id, week_start, requester_id))
        cursor.execute("""
            INSERT INTO swap_requests (dorm_id, week_start, requester_id, target_id, status)
            VALUES (?, ?, ?, ?, 'pending');
        """, (dorm_id, week_start, requester_id, target_id))
        conn.commit()
        return cursor.lastrowid

async def create_swap_request(dorm_id: int, week_start: str, requester_id: int, target_id: int) -> int:
    return await asyncio.to_thread(_create_swap_request_sync, dorm_id, week_start, requester_id, target_id)

def _get_swap_request_sync(swap_id: int) -> Optional[Dict[str, Any]]:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM swap_requests WHERE id = ?", (swap_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

async def get_swap_request(swap_id: int) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_get_swap_request_sync, swap_id)

def _execute_swap_sync(swap_id: int) -> Tuple[bool, str]:
    """Execute swap in weeks table for that specific week ONLY."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM swap_requests WHERE id = ?", (swap_id,))
        req = cursor.fetchone()
        if not req or req["status"] != "pending":
            return False, "Swap request is no longer pending."

        dorm_id = req["dorm_id"]
        week_start = req["week_start"]
        requester_id = req["requester_id"]
        target_id = req["target_id"]

        cursor.execute("SELECT * FROM weeks WHERE dorm_id = ? AND week_start = ?", (dorm_id, week_start))
        w = cursor.fetchone()
        if not w:
            return False, "Week record not found."

        # Replace requester with target in cleaner1, cleaner2, or cleaner3 for this single week only
        if w["cleaner1_id"] == requester_id:
            cursor.execute("UPDATE weeks SET cleaner1_id = ? WHERE dorm_id = ? AND week_start = ?", (target_id, dorm_id, week_start))
        elif w["cleaner2_id"] == requester_id:
            cursor.execute("UPDATE weeks SET cleaner2_id = ? WHERE dorm_id = ? AND week_start = ?", (target_id, dorm_id, week_start))
        elif w.get("cleaner3_id") == requester_id:
            cursor.execute("UPDATE weeks SET cleaner3_id = ? WHERE dorm_id = ? AND week_start = ?", (target_id, dorm_id, week_start))
        else:
            return False, "Requester is not currently assigned to clean this week."

        cursor.execute("UPDATE swap_requests SET status = 'accepted' WHERE id = ?", (swap_id,))
        conn.commit()
        return True, "Swap completed successfully."

async def execute_swap(swap_id: int) -> Tuple[bool, str]:
    return await asyncio.to_thread(_execute_swap_sync, swap_id)

def _decline_swap_sync(swap_id: int) -> bool:
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE swap_requests SET status = 'declined' WHERE id = ? AND status = 'pending'", (swap_id,))
        conn.commit()
        return cursor.rowcount > 0

async def decline_swap(swap_id: int) -> bool:
    return await asyncio.to_thread(_decline_swap_sync, swap_id)

# ---------------------------------------------------------------------------
# AI Chat History & Conversational Memory Operations
# ---------------------------------------------------------------------------
def _add_ai_chat_message_sync(
    dorm_id: int,
    chat_id: int,
    user_id: int,
    user_name: str,
    role: str,
    content: str
) -> None:
    """Insert a chat message into history and prune old messages past retention limit."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ai_chat_history (dorm_id, chat_id, user_id, user_name, role, content)
            VALUES (?, ?, ?, ?, ?, ?);
        """, (dorm_id, chat_id, user_id, user_name, role, content.strip()))

        # Keep history per chat lean (max 40 messages, prune oldest)
        cursor.execute("SELECT COUNT(*) as cnt FROM ai_chat_history WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        if row and row["cnt"] > 40:
            cursor.execute("""
                DELETE FROM ai_chat_history WHERE id IN (
                    SELECT id FROM ai_chat_history WHERE chat_id = ? ORDER BY id ASC LIMIT ?
                )
            """, (chat_id, row["cnt"] - 30))
        conn.commit()

async def add_ai_chat_message(
    dorm_id: int,
    chat_id: int,
    user_id: int,
    user_name: str,
    role: str,
    content: str
) -> None:
    await asyncio.to_thread(_add_ai_chat_message_sync, dorm_id, chat_id, user_id, user_name, role, content)

def _get_recent_ai_chat_history_sync(chat_id: int, limit: int = 8) -> List[Dict[str, Any]]:
    """Retrieve recent chat history for a chat in chronological order (oldest to newest)."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, dorm_id, chat_id, user_id, user_name, role, content, created_at
            FROM ai_chat_history
            WHERE chat_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (chat_id, limit))
        rows = cursor.fetchall()
        # Return in ascending chronological order
        return [dict(r) for r in reversed(rows)]

async def get_recent_ai_chat_history(chat_id: int, limit: int = 8) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_get_recent_ai_chat_history_sync, chat_id, limit)

def _clear_ai_chat_history_sync(chat_id: int) -> int:
    """Clear conversational memory for a specific chat."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ai_chat_history WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cursor.rowcount

async def clear_ai_chat_history(chat_id: int) -> int:
    return await asyncio.to_thread(_clear_ai_chat_history_sync, chat_id)

def _get_user_recent_chat_history_sync(user_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """Retrieve recent queries and topics from a specific user across dorm interactions."""
    with _get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, dorm_id, chat_id, user_name, content, created_at
            FROM ai_chat_history
            WHERE user_id = ? AND role = 'user'
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit))
        rows = cursor.fetchall()
        return [dict(r) for r in reversed(rows)]

async def get_user_recent_chat_history(user_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(_get_user_recent_chat_history_sync, user_id, limit)

