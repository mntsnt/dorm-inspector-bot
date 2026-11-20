"""
Configuration and Validation Module for Dorm Inspector Bot
"""

from collections import Counter
from dataclasses import dataclass
import datetime
import json
import logging
import os
from pathlib import Path
import sys
from typing import Dict, List, Optional, Tuple

# Try loading .env if python-dotenv is installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Memes Directory
MEMES_DIR = BASE_DIR / "assets" / "memes"

def get_meme_file(name: str) -> Optional[Path]:
    """Return Path to a meme image file if it exists, else None."""
    if not MEMES_DIR.exists():
        return None
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = MEMES_DIR / f"{name}{ext}"
        if candidate.is_file():
            return candidate
    return None

# ---------------------------------------------------------------------------
# Timezone Configuration (Africa/Addis_Ababa is permanently UTC+3, no DST)
# ---------------------------------------------------------------------------
def _init_timezone():
    tz_name = "Africa/Addis_Ababa"
    try:
        import zoneinfo
        try:
            return zoneinfo.ZoneInfo(tz_name)
        except Exception:
            pass
    except ImportError:
        pass

    try:
        import pytz
        return pytz.timezone(tz_name)
    except Exception:
        pass

    # Reliable fallback: Addis Ababa is fixed UTC+3
    return datetime.timezone(datetime.timedelta(hours=3), name=tz_name)

TIMEZONE = _init_timezone()

def get_current_time() -> datetime.datetime:
    """Return the current datetime localized to Africa/Addis_Ababa."""
    return datetime.datetime.now(TIMEZONE)

def get_current_date() -> datetime.date:
    """Return the current date in Africa/Addis_Ababa."""
    return get_current_time().date()

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Member:
    user_id: int
    name: str

@dataclass(frozen=True)
class WeekInfo:
    week_start: str         # "YYYY-MM-DD" (Monday)
    week_number: int        # 1-indexed from START_DATE
    cleaner1: Member
    cleaner2: Member
    monday_date: datetime.date
    friday_date: datetime.date

# ---------------------------------------------------------------------------
# Members Validation and Loading
# ---------------------------------------------------------------------------
MEMBERS_FILE = BASE_DIR / "members.json"

def load_and_validate_members(filepath: Path = MEMBERS_FILE) -> Tuple[List[Member], Dict[int, Member], Dict[str, Member], List[Tuple[Member, Member]]]:
    """
    Load members.json and strictly validate:
    1. Every member in 'members' must have user_id and name.
    2. Every member must appear in EXACTLY ONE pair in 'pairs'.
    3. No missing or extra members in pairs.
    Refuses to start if validation fails.
    """
    if not filepath.exists():
        sys.exit(f"CRITICAL ERROR: Members file not found at {filepath}")

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        sys.exit(f"CRITICAL ERROR: Failed to parse {filepath}: {e}")

    raw_members = data.get("members", [])
    raw_pairs = data.get("pairs", [])

    if not raw_members:
        sys.exit("CRITICAL ERROR: 'members' list in members.json is empty!")
    if not raw_pairs:
        sys.exit("CRITICAL ERROR: 'pairs' list in members.json is empty!")

    members: List[Member] = []
    members_by_id: Dict[int, Member] = {}
    members_by_name: Dict[str, Member] = {}

    for item in raw_members:
        uid = item.get("user_id")
        name = item.get("name")
        if uid is None or name is None:
            sys.exit(f"CRITICAL ERROR: Invalid member entry in members.json: {item}")
        if uid in members_by_id:
            sys.exit(f"CRITICAL ERROR: Duplicate user_id {uid} found in members.json!")
        if name.strip().lower() in members_by_name:
            sys.exit(f"CRITICAL ERROR: Duplicate name '{name}' found in members.json!")

        m = Member(user_id=int(uid), name=name.strip())
        members.append(m)
        members_by_id[m.user_id] = m
        members_by_name[m.name.strip().lower()] = m

    # Validate pairs
    pair_counts = Counter()
    validated_pairs: List[Tuple[Member, Member]] = []

    for idx, pair in enumerate(raw_pairs, start=1):
        if not isinstance(pair, list) or len(pair) != 2:
            sys.exit(f"CRITICAL ERROR: Pair #{idx} in members.json must contain exactly 2 names: {pair}")
        
        name1, name2 = pair[0].strip(), pair[1].strip()
        m1 = members_by_name.get(name1.lower())
        m2 = members_by_name.get(name2.lower())

        if not m1:
            sys.exit(f"CRITICAL ERROR: Member '{name1}' in pair #{idx} does not exist in 'members' list!")
        if not m2:
            sys.exit(f"CRITICAL ERROR: Member '{name2}' in pair #{idx} does not exist in 'members' list!")
        if m1.user_id == m2.user_id:
            sys.exit(f"CRITICAL ERROR: Pair #{idx} has duplicate member '{name1}' paired with themselves!")

        pair_counts[m1.name] += 1
        pair_counts[m2.name] += 1
        validated_pairs.append((m1, m2))

    # Strict rule: Every member must appear in EXACTLY ONE pair
    errors = []
    for m in members:
        count = pair_counts[m.name]
        if count == 0:
            errors.append(f"- Member '{m.name}' appears in 0 pairs (must appear in exactly one pair).")
        elif count > 1:
            errors.append(f"- Member '{m.name}' appears in {count} pairs (must appear in exactly one pair).")

    if errors:
        sys.exit("CRITICAL ERROR: Pairing validation failed:\n" + "\n".join(errors))

    return members, members_by_id, members_by_name, validated_pairs

MEMBERS, MEMBERS_BY_ID, MEMBERS_BY_NAME, PAIRS = load_and_validate_members()

# ---------------------------------------------------------------------------
# Environment and Schedule Settings
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

raw_group_id = os.getenv("GROUP_ID", "").strip()
raw_admin_id = os.getenv("ADMIN_ID", "").strip()
raw_start_date = os.getenv("START_DATE", "").strip()
raw_topic_id = os.getenv("TOPIC_ID", "").strip()

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is not set in environment or .env file.")

try:
    GROUP_ID = int(raw_group_id) if raw_group_id else 0
except ValueError:
    sys.exit(f"CRITICAL ERROR: GROUP_ID must be an integer, got: {raw_group_id}")

try:
    TOPIC_ID = int(raw_topic_id) if raw_topic_id else None
except ValueError:
    TOPIC_ID = None

try:
    ADMIN_ID = int(raw_admin_id) if raw_admin_id else 0
except ValueError:
    sys.exit(f"CRITICAL ERROR: ADMIN_ID must be an integer, got: {raw_admin_id}")

def get_monday_of_week(d: datetime.date) -> datetime.date:
    """Return the Monday of the week containing date d."""
    return d - datetime.timedelta(days=d.weekday())

if not raw_start_date:
    START_DATE_STR = "2026-11-23"  # Monday, November 23 (Announcement Mon Nov 23, Cleaners on Fri Nov 27)
else:
    START_DATE_STR = raw_start_date

# Validate START_DATE
try:
    START_DATE_OBJ = datetime.datetime.strptime(START_DATE_STR, "%Y-%m-%d").date()
except ValueError:
    sys.exit(f"CRITICAL ERROR: START_DATE must be in YYYY-MM-DD format, got: {START_DATE_STR}")

# Determine the base Monday for weekly rotation calculations.
# If START_DATE is already a Monday (e.g. Monday Nov 23, 2026), that is the base Monday.
# If a user provides a Friday (e.g. Friday Nov 27, 2026), base Monday is Monday of that same week (Nov 23)
# so the announcement starts that Monday and cleaning occurs on Friday.
if START_DATE_OBJ.weekday() == 0:
    START_BASE_MONDAY = START_DATE_OBJ
else:
    START_BASE_MONDAY = get_monday_of_week(START_DATE_OBJ)

ANNOUNCE_DAY = os.getenv("ANNOUNCE_DAY", "mon").strip().lower()
ANNOUNCE_TIME_STR = os.getenv("ANNOUNCE_TIME", "08:00").strip()
REMINDER_DAY = os.getenv("REMINDER_DAY", "thu").strip().lower()
REMINDER_TIME_STR = os.getenv("REMINDER_TIME", "18:00").strip()
CHECK_DAY = os.getenv("CHECK_DAY", "fri").strip().lower()
CHECK_TIME_STR = os.getenv("CHECK_TIME", "20:00").strip()

try:
    APPROVER_TIMEOUT_HOURS = int(os.getenv("APPROVER_TIMEOUT_HOURS", "12"))
except ValueError:
    APPROVER_TIMEOUT_HOURS = 12

try:
    FINE_TOTAL = int(os.getenv("FINE_TOTAL", "100"))
except ValueError:
    FINE_TOTAL = 100

FINE_EACH = FINE_TOTAL // 2

DB_PATH = BASE_DIR / os.getenv("DB_NAME", "dorm_inspector.db")

# Helper map for weekday names to Python weekday index (0=Monday, 6=Sunday)
DAY_MAP_PYTHON = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

# Python-telegram-bot job queue weekday: 0=Sunday, 1=Monday, ..., 6=Saturday
DAY_MAP_PTB = {
    "sun": 0, "sunday": 0,
    "mon": 1, "monday": 1,
    "tue": 2, "tuesday": 2,
    "wed": 3, "wednesday": 3,
    "thu": 4, "thursday": 4,
    "fri": 5, "friday": 5,
    "sat": 6, "saturday": 6,
}

def parse_time(time_str: str) -> datetime.time:
    """Parse HH:MM string to datetime.time with Africa/Addis_Ababa timezone."""
    parts = time_str.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    return datetime.time(hour=hour, minute=minute, tzinfo=TIMEZONE)

ANNOUNCE_TIME = parse_time(ANNOUNCE_TIME_STR)
REMINDER_TIME = parse_time(REMINDER_TIME_STR)
CHECK_TIME = parse_time(CHECK_TIME_STR)

# ---------------------------------------------------------------------------
# Rotation Logic
# ---------------------------------------------------------------------------
def get_week_info(target_date: Optional[datetime.date] = None) -> WeekInfo:
    """
    Compute the WeekInfo for the given target date (or today in Addis Ababa).
    The pair is deterministically computed from START_DATE:
    - Week 1 begins on START_BASE_MONDAY with Pair 1
    - Cycles every len(PAIRS) weeks (4 weeks)
    """
    if target_date is None:
        target_date = get_current_date()

    target_monday = get_monday_of_week(target_date)
    days_diff = (target_monday - START_BASE_MONDAY).days
    weeks_diff = days_diff // 7

    week_number = weeks_diff + 1
    pair_index = 0 if week_number < 1 else (week_number - 1) % len(PAIRS)
    cleaner1, cleaner2 = PAIRS[pair_index]
    friday_date = target_monday + datetime.timedelta(days=4)

    return WeekInfo(
        week_start=target_monday.strftime("%Y-%m-%d"),
        week_number=week_number,
        cleaner1=cleaner1,
        cleaner2=cleaner2,
        monday_date=target_monday,
        friday_date=friday_date,
    )

def get_upcoming_schedule(count: int = 4, from_date: Optional[datetime.date] = None) -> List[WeekInfo]:
    """Generate upcoming cleaning weeks starting from current week or START_BASE_MONDAY if not started."""
    if from_date is None:
        from_date = get_current_date()
    
    current_monday = get_monday_of_week(from_date)
    base_monday = max(current_monday, START_BASE_MONDAY)
    schedule: List[WeekInfo] = []
    for i in range(count):
        week_monday = base_monday + datetime.timedelta(weeks=i)
        schedule.append(get_week_info(week_monday))
    return schedule

def is_member(user_id: int) -> bool:
    """Check if a given user_id is one of the 8 dorm members."""
    return user_id in MEMBERS_BY_ID

def get_member(user_id: int) -> Optional[Member]:
    """Get Member object by user_id."""
    return MEMBERS_BY_ID.get(user_id)

def get_member_by_name(name: str) -> Optional[Member]:
    """Get Member object by name (case-insensitive, supporting Hasan / Hassan aliases)."""
    norm = name.strip().lower()
    if norm in ("hasan", "hassen", "hasen"):
        norm = "hassan"
    return MEMBERS_BY_NAME.get(norm)

def update_member_id(name: str, new_user_id: int) -> bool:
    """
    Update a member's user_id in members.json and reload in-memory structures.
    Returns True on success, False on failure.
    """
    global MEMBERS, MEMBERS_BY_ID, MEMBERS_BY_NAME, PAIRS
    try:
        with open(MEMBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        target_norm = name.strip().lower()
        if target_norm in ("hasan", "hassen", "hasen"):
            target_norm = "hassan"

        found = False
        for item in data.get("members", []):
            item_norm = item.get("name", "").strip().lower()
            if item_norm in ("hasan", "hassen", "hasen"):
                item_norm = "hassan"
            if item_norm == target_norm:
                item["user_id"] = new_user_id
                found = True
                break

        if not found:
            return False

        with open(MEMBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        MEMBERS, MEMBERS_BY_ID, MEMBERS_BY_NAME, PAIRS = load_and_validate_members(MEMBERS_FILE)
        logger.info("Successfully updated user_id for %s to %s", name, new_user_id)
        return True
    except Exception as e:
        logger.error("Failed to update user_id for %s: %s", name, e)
        return False
