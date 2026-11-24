"""
General Commands & Interactive Navigation Handler (Multi-Tenant Platform)
Handles /start, /help, /status, /schedule, /fines, /history, /menu, /members,
plus interactive inline buttons and persistent mobile keyboard.
Seamlessly handles both group-scoped commands and private DM interactions.
"""

from datetime import date, datetime, timedelta
import logging
import random
import re
from typing import Any, Dict, List, Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db

logger = logging.getLogger(__name__)

async def check_member_permission(user_id: int, dorm_id: Optional[int] = None) -> bool:
    """Return True if user is a registered member of the target dorm (or any active dorm if dorm_id is None)."""
    if user_id == getattr(config, "ADMIN_ID", 6163528558):
        return True
    if dorm_id:
        member = await db.get_dorm_member(dorm_id, user_id)
        return member is not None
    user_dorms = await db.get_user_dorms(user_id)
    if user_dorms:
        return True
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    if config.is_member(user_id):
        member = await db.get_dorm_member(default_gid, user_id)
        return member is not None
    return False

async def reject_non_member(update: Update) -> None:
    """Politely guide new students on how to set up their dorm."""
    if update.effective_message:
        await update.effective_message.reply_text(
            "👋 **Hi! I am Dorm Inspector Bot.**\n\n"
            "I manage weekly cleaning schedules, photo inspections, and fines for university dorm rooms.\n\n"
            "To use me for your room:\n"
            "1. Add me to your dorm's Telegram group chat.\n"
            "2. Send `/setup` in that group.\n"
            "3. Your roommates can tap the join button to link up!",
            parse_mode=ParseMode.MARKDOWN
        )

async def _resolve_user_dorm(user_id: int) -> Optional[Dict[str, Any]]:
    """Resolve the single active dorm for a user in private chat."""
    user_dorms = await db.get_user_dorms(user_id)
    if user_dorms:
        return user_dorms[0]
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    if user_id == getattr(config, "ADMIN_ID", 6163528558):
        dorm = await db.get_dorm(default_gid)
        if not dorm:
            await db.seed_default_dorm()
            dorm = await db.get_dorm(default_gid)
        return dorm
    if config.is_member(user_id):
        member = await db.get_dorm_member(default_gid, user_id)
        if member:
            return await db.get_dorm(default_gid)
    return None

async def _resolve_target_dorm(dorm_id: Optional[int] = None, user_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Reliably resolve the active dorm for a group chat or user command without cross-dorm leaks."""
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    if dorm_id is not None:
        dorm = await db.get_dorm(dorm_id)
        if dorm:
            return dorm
        if dorm_id in (default_gid, -2378440774, 2378440774):
            return await db.seed_default_dorm_for_group(dorm_id)
        return None

    if user_id is not None:
        return await _resolve_user_dorm(user_id)

    return None

# ---------------------------------------------------------------------------
# Keyboards & Navigation Builders
# ---------------------------------------------------------------------------
async def get_user_week_role(
    user_id: Optional[int], dorm_id: Optional[int] = None
) -> Tuple[bool, str, str]:
    """
    Determine whether user_id is an active cleaner for the current week.
    Returns: (is_cleaner, role_name, cleaners_display_str)
    """
    if not user_id:
        return False, "UNKNOWN", ""

    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user_id)
    if not dorm:
        return False, "UNKNOWN", ""

    resolved_id = dorm["dorm_id"]
    pairs = await db.get_dorm_pairs(resolved_id)
    members = await db.get_dorm_members(resolved_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    if user_id not in members_by_id:
        cfg_m = getattr(config, "MEMBERS_BY_ID", {}).get(user_id)
        if cfg_m:
            members_by_id[user_id] = cfg_m.name

    from scheduler import get_dorm_week_info
    week_info = get_dorm_week_info(dorm, pairs, members_by_id)
    if not week_info or week_info.week_number < 1:
        return False, "NOT_STARTED", ""

    cleaners_str = week_info.cleaner_display
    week_record = await db.get_week(week_info.week_start, dorm_id=resolved_id)

    if user_id in week_info.cleaner_ids:
        if week_record and (week_record.get("final_status") == "completed" or week_record.get("photo_status") == "approved"):
            return False, "CLEANER_DONE", cleaners_str
        return True, "CLEANER", cleaners_str

    if week_record and week_record.get("approver_id") == user_id:
        return False, "APPROVER", cleaners_str

    return False, "OFF_DUTY", cleaners_str

def get_main_menu_inline_keyboard(
    is_cleaner: bool = False,
    dorm_code: Optional[str] = None
) -> InlineKeyboardMarkup:
    """Main menu grid for one-tap navigation. Submit Cleaning only shown if user is cleaner."""
    dash_base = getattr(config, "DASHBOARD_URL", "https://inspector.app.aletcloud.com").rstrip("/")
    dash_url = f"{dash_base}/?code={dorm_code}" if dorm_code else f"{dash_base}/"

    if is_cleaner:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 Status", callback_data="menu_status"),
                InlineKeyboardButton("📅 Schedule", callback_data="menu_schedule"),
            ],
            [
                InlineKeyboardButton("🧼 Submit Cleaning", callback_data="menu_done"),
                InlineKeyboardButton("💸 Fines", callback_data="menu_fines"),
            ],
            [
                InlineKeyboardButton("📜 History", callback_data="menu_history"),
                InlineKeyboardButton("ℹ️ Help", callback_data="menu_help"),
            ],
            [
                InlineKeyboardButton("🪙 Tsidat Tokens", callback_data="menu_tsidat"),
                InlineKeyboardButton("🎲 Dorm Roulette", callback_data="menu_spin"),
            ],
            [
                InlineKeyboardButton("🌐 Open Dashboard", url=dash_url),
            ],
        ])

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Status", callback_data="menu_status"),
            InlineKeyboardButton("📅 Schedule", callback_data="menu_schedule"),
        ],
        [
            InlineKeyboardButton("💸 Fines", callback_data="menu_fines"),
            InlineKeyboardButton("📜 History", callback_data="menu_history"),
        ],
        [
            InlineKeyboardButton("🪙 Tsidat Tokens", callback_data="menu_tsidat"),
            InlineKeyboardButton("🎲 Dorm Roulette", callback_data="menu_spin"),
        ],
        [
            InlineKeyboardButton("ℹ️ Help", callback_data="menu_help"),
            InlineKeyboardButton("🌐 Open Dashboard", url=dash_url),
        ],
    ])

def get_reply_keyboard(is_cleaner: bool = False) -> ReplyKeyboardMarkup:
    """Persistent mobile bottom keyboard sending direct Telegram slash commands."""
    if is_cleaner:
        rows = [
            ["/status", "/schedule"],
            ["/submit", "/fines"],
            ["/tokens", "/roulette"],
            ["/history", "/help"],
            ["/code", "/menu"],
        ]
    else:
        rows = [
            ["/status", "/schedule"],
            ["/fines", "/tokens"],
            ["/roulette", "/history"],
            ["/code", "/help"],
            ["/menu"],
        ]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
    )

async def get_dorm_main_menu_text(
    member_name: str,
    connected_info: Optional[str] = None,
    dorm: Optional[Dict[str, Any]] = None,
    is_cleaner: bool = False
) -> str:
    """Welcome and overview text for the main menu, dynamically scoped to the target dorm."""
    dorm_name = dorm.get("dorm_name") if dorm else None
    status_suffix = f"\n\nMembers connected: **{connected_info}**" if connected_info else ""
    dorm_prefix = f"**{dorm_name}**\n\n" if dorm_name else ""

    if not dorm:
        return (
            f"Hi **{member_name}**!\n\n"
            "Use the buttons below to check duty, fines, or rules."
        )

    dorm_id = dorm["dorm_id"]
    from scheduler import get_dorm_week_info
    pairs = await db.get_dorm_pairs(dorm_id)
    members = await db.get_dorm_members(dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    current_date = config.get_current_date()
    week_info = get_dorm_week_info(dorm, pairs, members_by_id, target_date=current_date)

    if not week_info or not pairs:
        return (
            f"{dorm_prefix}Hi **{member_name}**!\n\n"
            f"Cleaning schedule is being configured.{status_suffix}\n\n"
            "Use the buttons below to check duty, fines, or rules."
        )

    if week_info.week_number < 1:
        first_c1 = members_by_id.get(pairs[0]["cleaner1_id"], "TBD")
        first_c2 = members_by_id.get(pairs[0]["cleaner2_id"], "TBD")
        start_date_str = dorm.get("start_date") or getattr(config, "START_DATE_STR", "2026-11-23")
        return (
            f"{dorm_prefix}Hi **{member_name}**!\n\n"
            f"Week 1 starts: Monday, {start_date_str}\n"
            f"First cleaners: **{first_c1} & {first_c2}**{status_suffix}\n\n"
            "Use the buttons below to check duty, fines, or rules."
        )
    else:
        c1 = week_info.cleaner1_name
        c2 = week_info.cleaner2_name
        week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)
        is_completed_duty = bool(week_record and (week_record.get("final_status") == "completed" or week_record.get("photo_status") == "approved"))
        if is_completed_duty:
            action_text = "check duty, fines, or schedule"
            status_note = " (✅ Cleaned & Approved - done for the week!)"
        else:
            action_text = "submit cleaning" if is_cleaner else "check duty, fines, or schedule"
            status_note = ""
        return (
            f"{dorm_prefix}Hi **{member_name}**!\n\n"
            f"Week {week_info.week_number} cleaners: **{c1} & {c2}**{status_note}{status_suffix}\n\n"
            f"Use the buttons below to {action_text}."
        )

def get_main_menu_text(member_name: str, connected_info: Optional[str] = None, dorm_name: Optional[str] = None) -> str:
    """Overview text for main menu."""
    status_suffix = f"\n\nMembers connected: **{connected_info}**" if connected_info else ""
    dorm_prefix = f"**{dorm_name}**\n\n" if dorm_name else ""

    if not dorm_name or dorm_name == "Dorm Cleaning Schedule":
        current_date = config.get_current_date()
        week_info = config.get_week_info(current_date)
        if week_info.week_number < 1:
            first_cleaners = f"{config.PAIRS[0][0].name} & {config.PAIRS[0][1].name}"
            return (
                f"{dorm_prefix}Hi **{member_name}**!\n\n"
                f"Week 1 starts: Monday, {config.START_BASE_MONDAY.strftime('%b %d')}\n"
                f"First cleaners: **{first_cleaners}**{status_suffix}\n\n"
                "Use the buttons below to check duty, fines, or rules."
            )
        else:
            return (
                f"{dorm_prefix}Hi **{member_name}**!\n\n"
                f"Week {week_info.week_number} cleaners: **{week_info.cleaner1.name} & {week_info.cleaner2.name}**{status_suffix}\n\n"
                "Use the buttons below to check duty, fines, or submit cleaning."
            )
    return (
        f"{dorm_prefix}Hi **{member_name}**!{status_suffix}\n\n"
        "Use the buttons below to check duty, fines, or submit cleaning."
    )

# ---------------------------------------------------------------------------
# View Content Generators (Text + Navigation Buttons)
# ---------------------------------------------------------------------------
async def get_status_content(user_id: Optional[int] = None, dorm_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate status report text and navigation buttons."""
    dash_base = getattr(config, "DASHBOARD_URL", "https://inspector.app.aletcloud.com").rstrip("/")
    default_dash = f"{dash_base}/"
    fallback_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
            InlineKeyboardButton("Fines", callback_data="menu_fines"),
        ],
        [
            InlineKeyboardButton("🌐 Open Dashboard", url=default_dash),
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    if user_id is not None and dorm_id is not None:
        member = await db.get_dorm_member(dorm_id, user_id)
        if not member:
            return "You are not a member of this dorm.", fallback_kb

    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user_id)
    if not dorm:
        return "No active cleaning schedule found. Send /setup to configure your dorm.", fallback_kb

    resolved_id = dorm["dorm_id"]
    dorm_code = dorm.get("dorm_code") or "DORM-101"
    dash_url = f"{dash_base}/?code={dorm_code}"

    pairs = await db.get_dorm_pairs(resolved_id)
    members = await db.get_dorm_members(resolved_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    from scheduler import get_dorm_week_info
    week_info = get_dorm_week_info(dorm, pairs, members_by_id)

    week_record = await db.get_week(week_info.week_start, dorm_id=resolved_id) if (week_info and week_info.week_number >= 1) else None
    is_completed_duty = bool(week_record and (week_record.get("final_status") == "completed" or week_record.get("photo_status") == "approved"))

    is_cleaner = False
    if user_id and week_info and week_info.week_number >= 1 and not is_completed_duty:
        is_cleaner = user_id in week_info.cleaner_ids

    if is_cleaner:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
                InlineKeyboardButton("Submit Cleaning", callback_data="menu_done"),
            ],
            [
                InlineKeyboardButton("🌐 Open Dashboard", url=dash_url),
                InlineKeyboardButton("Main Menu", callback_data="menu_home"),
            ]
        ])
    else:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
                InlineKeyboardButton("Fines", callback_data="menu_fines"),
            ],
            [
                InlineKeyboardButton("🌐 Open Dashboard", url=dash_url),
                InlineKeyboardButton("Main Menu", callback_data="menu_home"),
            ]
        ])

    if not week_info or week_info.week_number < 1:
        start_date = dorm.get("start_date", "Soon")
        first_cleaners = "TBD"
        if pairs:
            c1 = members_by_id.get(pairs[0]["cleaner1_id"], f"ID {pairs[0]['cleaner1_id']}")
            c2 = members_by_id.get(pairs[0]["cleaner2_id"], f"ID {pairs[0]['cleaner2_id']}")
            first_cleaners = f"**{c1}** & **{c2}**"
        msg = (
            f"Cleaning has not started yet for **{dorm['dorm_name']}**.\n"
            f"🔑 **Dorm Code:** `{dorm_code}`\n\n"
            f"Starts: {start_date}\n"
            f"First cleaners: {first_cleaners}"
        )
        return msg, keyboard

    if not week_record:
        week_record = await db.get_week(week_info.week_start, dorm_id=resolved_id)
    c1_name = week_info.cleaner1_name
    c2_name = week_info.cleaner2_name

    if week_record and week_record.get("approver_id"):
        approver_name = members_by_id.get(week_record["approver_id"], f"ID {week_record['approver_id']}")
    else:
        approver_name = "Not yet assigned"

    if not week_record:
        duty_state = "In progress (deadline Friday 8:00 PM)."
    else:
        final_status = week_record.get("final_status", "pending")
        photo_status = week_record.get("photo_status", "none")
        check_sent = week_record.get("check_sent", 0)
        fine_amount = dorm.get("fine_amount", 100)

        rating = week_record.get("rating")
        review_text = week_record.get("review_text")
        rating_suffix = f" (⭐⭐⭐⭐⭐ 5/5 Stars)" if rating == 5 else (f" ({rating}★ Stars)" if rating else "")

        if final_status == "completed":
            duty_state = f"Cleaned and approved{rating_suffix}. Done for the week! No fines."
        elif final_status == "fined":
            duty_state = f"Not cleaned. Fined {fine_amount} ETB total."
        elif photo_status == "approved":
            duty_state = "Photo approved."
        elif photo_status == "submitted":
            duty_state = f"Photo sent. Waiting for {approver_name} to check."
        elif photo_status == "rejected":
            duty_state = "Photo rejected. Cleaners need to clean again and send a new photo."
        elif week_record.get("done_reported"):
            duty_state = "Cleaners reported done. Waiting for photo."
        elif check_sent:
            duty_state = f"Friday check in progress. Waiting for {approver_name}."
        else:
            duty_state = "In progress (deadline Friday 8:00 PM)."

    msg = (
        f"**{dorm['dorm_name']}** — Week #{week_info.week_number} "
        f"({week_info.monday_date.strftime('%b %d')} – {week_info.friday_date.strftime('%b %d')})\n"
        f"🔑 **Dorm Code:** `{dorm_code}`\n\n"
        f"Cleaners: **{c1_name}** & **{c2_name}**\n"
        f"Approver: **{approver_name}**\n"
        f"Status: {duty_state}"
    )
    return msg, keyboard

async def get_schedule_content(dorm_id: Optional[int] = None, user_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate 4-week schedule text and navigation buttons."""
    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user_id)
    dorm_code = dorm.get("dorm_code") or "DORM-101" if dorm else "DORM-101"
    dash_base = getattr(config, "DASHBOARD_URL", "https://inspector.app.aletcloud.com").rstrip("/")
    dash_url = f"{dash_base}/?code={dorm_code}" if dorm_code else f"{dash_base}/"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Status", callback_data="menu_status"),
            InlineKeyboardButton("Fines", callback_data="menu_fines"),
        ],
        [
            InlineKeyboardButton("🌐 Open Dashboard", url=dash_url),
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    if user_id is not None and dorm_id is not None:
        member = await db.get_dorm_member(dorm_id, user_id)
        if not member:
            return "You are not a member of this dorm.", keyboard

    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user_id)
    if not dorm:
        return "No active cleaning schedule found. Send /setup to configure your dorm.", keyboard

    resolved_id = dorm["dorm_id"]
    pairs = await db.get_dorm_pairs(resolved_id)
    members = await db.get_dorm_members(resolved_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    if not dorm or not pairs:
        return f"No schedule configured for {dorm['dorm_name'] if dorm else 'this dorm'}.", keyboard

    from scheduler import get_dorm_week_info
    today = config.get_current_date()
    current_monday = config.get_monday_of_week(today)

    start_date_str = dorm.get("start_date") or getattr(config, "START_DATE_STR", "2026-11-23")
    try:
        start_date_obj = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except Exception:
        start_date_obj = getattr(config, "START_BASE_MONDAY", date(2026, 11, 23))
    start_base_monday = config.get_monday_of_week(start_date_obj)
    base_monday = max(current_monday, start_base_monday)

    lines = [f"**{dorm['dorm_name']}** — Next 4 Weeks:\n"]
    for i in range(4):
        target_monday = base_monday + timedelta(weeks=i)
        w = get_dorm_week_info(dorm, pairs, members_by_id, target_date=target_monday)
        if w:
            if target_monday == current_monday:
                tag = " (This Week)"
            elif (target_monday - today).days <= 7 and target_monday > current_monday:
                tag = " (Upcoming)"
            else:
                tag = ""
            date_str = f"{w.monday_date.strftime('%b %d')} – {w.friday_date.strftime('%b %d')}"
            lines.append(f"• Week {w.week_number} ({date_str}){tag}: {w.cleaner1_name} & {w.cleaner2_name}")

    return "\n".join(lines), keyboard

async def get_fines_content(dorm_id: Optional[int] = None, user_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate unpaid fines ledger text and navigation buttons."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
            InlineKeyboardButton("History", callback_data="menu_history"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    if user_id is not None and dorm_id is not None:
        member = await db.get_dorm_member(dorm_id, user_id)
        if not member:
            return "You are not a member of this dorm.", keyboard

    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user_id)
    if not dorm:
        return "No active cleaning schedule found. Send /setup to configure your dorm.", keyboard

    resolved_id = dorm["dorm_id"]
    unpaid_summary = await db.get_unpaid_fines_summary(dorm_id=resolved_id)

    prefix = f"**{dorm['dorm_name']}** — " if dorm else ""

    if not unpaid_summary:
        msg = f"{prefix}No unpaid fines! Everyone is clean. 🎉"
        return msg, keyboard

    lines = [f"{prefix}Unpaid Fines:\n"]
    total_ledger = 0
    for entry in unpaid_summary:
        amount = entry["total_unpaid"]
        name = entry["user_name"]
        total_ledger += amount
        lines.append(f"• **{name}**: {amount} ETB")

    lines.append(f"\nTotal: {total_ledger} ETB")
    return "\n".join(lines), keyboard

async def get_history_content(dorm_id: Optional[int] = None, user_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate past 8 weeks history text and navigation buttons."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Fines", callback_data="menu_fines"),
            InlineKeyboardButton("Help", callback_data="menu_help"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    if user_id is not None and dorm_id is not None:
        member = await db.get_dorm_member(dorm_id, user_id)
        if not member:
            return "You are not a member of this dorm.", keyboard

    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user_id)
    if not dorm:
        return "No active cleaning schedule found. Send /setup to configure your dorm.", keyboard

    resolved_id = dorm["dorm_id"]
    members = await db.get_dorm_members(resolved_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    records = await db.get_history(limit=8, dorm_id=resolved_id)
    prefix = f"**{dorm['dorm_name']}** — " if dorm else ""

    if not records:
        msg = f"{prefix}No history yet."
        return msg, keyboard

    lines = [f"{prefix}Past Weeks:\n"]
    for r in records:
        w_start = r["week_start"]
        c1_name = members_by_id.get(r["cleaner1_id"], f"ID {r['cleaner1_id']}")
        c2_name = members_by_id.get(r["cleaner2_id"], f"ID {r['cleaner2_id']}")
        f_status = r.get("final_status", "pending")
        rating = r.get("rating")
        stars_badge = f" ({rating}★)" if rating else ""
        if f_status == "completed":
            badge = f"Cleaned{stars_badge}"
        elif f_status == "fined":
            badge = "Fined"
        else:
            badge = "Pending"

        lines.append(f"• Week {r['week_number']} ({w_start}): {c1_name} & {c2_name} — {badge}")

    return "\n".join(lines), keyboard

def get_help_content() -> Tuple[str, InlineKeyboardMarkup]:
    """Generate user guide text and navigation buttons."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("History", callback_data="menu_history"),
            InlineKeyboardButton("Status", callback_data="menu_status"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    help_text = (
        "**Dorm Inspector Guide**\n\n"
        "• **Mon 8 AM**: Duty announced. Cleaners clean & send photo in DM.\n"
        "• **Review**: Approver rates 1-5 stars; cleaners earn Tsidat tokens.\n"
        "• **Fri 8 PM**: Check time. Cleaners confirm; uncleaned rooms get fined.\n\n"
        "**Commands:**\n"
        "/status • /schedule • /fines • /tsidat • /spin <reason> • /swap <name> • /inspector • /ai_status"
    )
    return help_text, keyboard

async def get_done_content(user_id: int, dorm_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate cleaning submission instruction text."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Status", callback_data="menu_status"),
            InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    msg = (
        "To submit cleaning duty:\n\n"
        "1. Clean the dorm room with your partner.\n"
        "2. Send /done or click 'We cleaned' in the Monday announcement.\n"
        "3. Send a photo of the cleaned room directly in this chat.\n"
        "4. The approver will review and confirm!"
    )
    return msg, keyboard

async def get_tsidat_content(dorm_id: Optional[int] = None, user_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate Tsidat token leaderboard text and navigation buttons."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Status", callback_data="menu_status"),
            InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
        ],
        [
            InlineKeyboardButton("🎲 Dorm Roulette", callback_data="menu_spin"),
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    if user_id is not None and dorm_id is not None:
        member = await db.get_dorm_member(dorm_id, user_id)
        if not member:
            return "You are not a member of this dorm.", keyboard

    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user_id)
    if not dorm:
        return "No active cleaning schedule found. Send /setup to configure your dorm.", keyboard

    resolved_id = dorm["dorm_id"]
    leaderboard = await db.get_tsidat_leaderboard(resolved_id)
    d_name = dorm.get("dorm_name") or "Dorm Cleaning Schedule"
    medals = ["🥇", "🥈", "🥉"]

    lines = [f"🪙 **{d_name} — Tsidat Leaderboard**\n"]
    if not leaderboard:
        lines.append("No Tsidat tokens awarded yet.")
    else:
        for idx, entry in enumerate(leaderboard):
            rank_icon = medals[idx] if idx < len(medals) else "•"
            bal = entry.get("tsidat_balance", 0)
            lines.append(f"{rank_icon} **{entry['name']}**: {bal} Tsidat")

    lines.append(
        "\nEarn Tsidat tokens by cleaning on schedule and completing inspections."
    )
    return "\n".join(lines), keyboard

# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command (including join deep-links and main menu presentation)."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # Check for deep-link parameter: /start join_<param>
    if context.args and len(context.args) > 0:
        arg = context.args[0]
        if arg.startswith("join_"):
            param = arg.replace("join_", "")
            # Telegram supergroups have negative IDs, encoded as abs(group_id)
            try:
                target_dorm_id = -int(param) if not param.startswith("-") else int(param)
            except ValueError:
                target_dorm_id = 0

            dorm = await db.get_dorm(target_dorm_id)
            if not dorm:
                try:
                    target_dorm_id = int(param)
                    dorm = await db.get_dorm(target_dorm_id)
                except ValueError:
                    dorm = None

            if dorm:
                member_name = user.first_name or user.username or f"User {user.id}"
                success, msg = await db.add_dorm_member(
                    dorm_id=dorm["dorm_id"],
                    user_id=user.id,
                    name=member_name,
                    chat_id=chat.id,
                    username=user.username,
                    is_admin=1 if user.id == dorm["admin_id"] else 0
                )
                if not success:
                    await update.effective_message.reply_text(
                        f"⚠️ **Cannot join {dorm['dorm_name']}**\n\n"
                        f"{msg}\n\n"
                        "A student can only belong to one dorm at a time. "
                        "To join this dorm, you must first leave your current dorm using `/leave`.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return

                logger.info("User %s (%s) joined dorm %s via deep-link", member_name, user.id, dorm["dorm_id"])

                # Post greeting in the dorm group chat
                try:
                    await context.bot.send_message(
                        chat_id=dorm["dorm_id"],
                        message_thread_id=dorm.get("topic_id"),
                        text=f"👤 **{member_name}** just joined the dorm!",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.warning("Could not ping dorm group %s on member join: %s", dorm["dorm_id"], e)

                default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
                is_our_dorm = (
                    dorm["dorm_id"] in (default_gid, getattr(config, "GROUP_ID", 0), 0)
                    or config.is_member(user.id)
                )

                if is_our_dorm:
                    context.user_data.pop("awaiting_name", None)
                    welcome_dm = (
                        f"🎉 **Welcome to {dorm['dorm_name']}!**\n\n"
                        "You are registered in the dorm cleaning schedule.\n"
                        "You will receive duty announcements, reminders, and verification prompts here in DM.\n\n"
                        "Tap below to see your dorm's status."
                    )
                else:
                    context.user_data["awaiting_name"] = dorm["dorm_id"]
                    welcome_dm = (
                        f"🎉 **You successfully joined {dorm['dorm_name']}!**\n\n"
                        "👤 **What is your name?**\n"
                        "Please reply with your real name (e.g. *Nahom* or *Bilisuma*) or send `/name <your_name>` "
                        "so your roommates and the inspector recognize you on the chore schedule and leaderboard.\n\n"
                        "You will receive duty announcements, reminders, and verification prompts here in DM."
                    )
                is_cleaner, _, _ = await get_user_week_role(user.id, dorm_id=dorm["dorm_id"])
                code = dorm.get("dorm_code") or "DORM-101"
                await update.effective_message.reply_text(
                    welcome_dm,
                    reply_markup=get_reply_keyboard(is_cleaner=is_cleaner),
                    parse_mode=ParseMode.MARKDOWN
                )
                await update.effective_message.reply_text(
                    "Main Menu:",
                    reply_markup=get_main_menu_inline_keyboard(is_cleaner=is_cleaner, dorm_code=code),
                    parse_mode=ParseMode.MARKDOWN
                )
                return

    # Auto-claim placeholder member if first name, username, or arg matches (e.g. Bilisuma with placeholder ID 111111111)
    if not await check_member_permission(user.id):
        user_dorms = await db.get_user_dorms(user.id)
        default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        if not (user_dorms and any(d["dorm_id"] != default_gid for d in user_dorms)):
            fname = (user.first_name or "").strip().lower()
            uname = (user.username or "").strip().lower()
            for m in getattr(config, "MEMBERS", []):
                if m.user_id == 111111111:
                    m_name = m.name.strip().lower()
                    is_claim_arg = (
                        context.args
                        and len(context.args) > 0
                        and context.args[0].lower() in (f"claim_{m_name}", m_name)
                    )
                    if m_name in (fname, uname) or is_claim_arg:
                        logger.info("Auto-linking placeholder member %s to user ID %s", m.name, user.id)
                        config.update_member_id(m.name, user.id)
                        await db.update_member_id_in_db(old_id=m.user_id, new_id=user.id)
                        break

    # Normal /start verification
    if not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    member_name = user.first_name
    conn_str = None
    target_dorm = None

    if chat.type == "private":
        target_dorm = await _resolve_user_dorm(user.id)
        dorm_id = target_dorm["dorm_id"] if target_dorm else getattr(config, "GROUP_ID", 0)

        # Look up member name from DB
        db_member = await db.get_dorm_member(dorm_id, user.id)
        if db_member:
            member_name = db_member["name"]

        await db.upsert_user(
            user_id=user.id,
            name=member_name,
            chat_id=chat.id,
            username=user.username
        )

        connected_count, total_count, connected_members, pending_members = await db.get_connection_status(dorm_id)
        conn_str = f"{connected_count}/{total_count}"

        # If all members are connected, celebrate in group
        if total_count >= 2 and connected_count >= total_count:
            announced_key = f"all_connected_{dorm_id}"
            already_announced = await db.get_setting(announced_key)
            if not already_announced:
                await db.set_setting(announced_key, "1")
                d_name = target_dorm["dorm_name"] if target_dorm else "Dorm"
                topic_id = target_dorm.get("topic_id") if target_dorm else getattr(config, "TOPIC_ID", None)
                group_text = (
                    f"🎉 **All {total_count} roommates in {d_name} are now connected!**\n\n"
                    "Everyone has linked to the bot. Dorm Inspector is fully operational.\n\n"
                    "Let's keep the dorm clean!"
                )
                welcome_meme = config.get_meme_file("welcome")
                try:
                    if welcome_meme:
                        with open(welcome_meme, "rb") as photo:
                            await context.bot.send_photo(
                                chat_id=dorm_id,
                                message_thread_id=topic_id,
                                photo=photo,
                                caption=group_text,
                                parse_mode=ParseMode.MARKDOWN
                            )
                    else:
                        await context.bot.send_message(
                            chat_id=dorm_id,
                            message_thread_id=topic_id,
                            text=group_text,
                            parse_mode=ParseMode.MARKDOWN
                        )
                except Exception as e:
                    logger.error("Failed to post all-connected celebration to group: %s", e)

        target_dorm_id = target_dorm["dorm_id"] if target_dorm else None
        is_cleaner, _, _ = await get_user_week_role(user.id, dorm_id=target_dorm_id)
        dorm_code = target_dorm.get("dorm_code") or "DORM-101" if target_dorm else "DORM-101"

        welcome_text = await get_dorm_main_menu_text(member_name, connected_info=conn_str, dorm=target_dorm, is_cleaner=is_cleaner)
        inline_markup = get_main_menu_inline_keyboard(is_cleaner=is_cleaner, dorm_code=dorm_code)
        persistent_reply_kb = get_reply_keyboard(is_cleaner=is_cleaner)

        welcome_meme = config.get_meme_file("welcome")
        if welcome_meme:
            try:
                with open(welcome_meme, "rb") as photo:
                    await update.effective_message.reply_photo(
                        photo=photo,
                        caption=f"Welcome, {member_name}!",
                        reply_markup=persistent_reply_kb,
                        parse_mode=ParseMode.MARKDOWN
                    )
            except Exception as e:
                logger.warning("Could not send welcome meme: %s", e)
        else:
            try:
                await update.effective_message.reply_text(
                    "🎛️ Navigation keyboard ready below.",
                    reply_markup=persistent_reply_kb
                )
            except Exception as e:
                logger.warning("Could not send reply keyboard: %s", e)

        await update.effective_message.reply_text(welcome_text, reply_markup=inline_markup, parse_mode=ParseMode.MARKDOWN)

        default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        is_our_dorm = (
            target_dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)
            or config.is_member(user.id)
        )

        name_confirmed = await db.get_setting(f"name_confirmed_{user.id}")
        is_setname_arg = context.args and len(context.args) > 0 and context.args[0].lower() in ("setname", "name")

        # Our dorm is already set up and running. Escape our dorm from automatic name prompts.
        # Only prompt if user explicitly typed /name or /start setname, OR if it is a new unconfigured dorm user without a confirmed name.
        if is_setname_arg or (not is_our_dorm and not name_confirmed):
            context.user_data["awaiting_name"] = dorm_id
            name_prompt = (
                f"👤 **What is your name?**\n\n"
                f"Currently your name is listed as **{member_name}**.\n"
                f"Please reply with your real name (e.g. *Nahom* or *Bilisuma*) or type `/name <your_name>` "
                f"so your roommates can easily recognize you on the chore schedule."
            )
            await update.effective_message.reply_text(name_prompt, parse_mode=ParseMode.MARKDOWN)
        else:
            context.user_data.pop("awaiting_name", None)
    else:
        # In a group chat
        await update.effective_message.reply_text(
            f"Hi **{member_name}**! To check your duty or fines privately, message me in DM.",
            parse_mode=ParseMode.MARKDOWN
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send help instructions with navigation buttons."""
    user = update.effective_user
    if not user or not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    text, keyboard = get_help_content()
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current week's cleaners, approver, and duty status."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    if chat.type == "private" and not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    text, keyboard = await get_status_content(user.id, dorm_id=dorm_id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the next 4 weeks of cleaning pairs."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    if chat.type == "private" and not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    text, keyboard = await get_schedule_content(dorm_id=dorm_id, user_id=user.id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def fines_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show unpaid fine totals."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    if chat.type == "private" and not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    text, keyboard = await get_fines_content(dorm_id=dorm_id, user_id=user.id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show last 8 weeks of cleaning results."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    if chat.type == "private" and not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    text, keyboard = await get_history_content(dorm_id=dorm_id, user_id=user.id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def members_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show connection status of dorm members."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else None
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user.id)
    if not dorm:
        if is_group:
            await update.effective_message.reply_text(
                "🧼 This group doesn't have an active cleaning schedule yet.\n"
                "Send `/setup` to initialize Dorm Inspector for this room!",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await reject_non_member(update)
        return

    target_dorm_id = dorm["dorm_id"]
    if is_group and not await check_member_permission(user.id, dorm_id=target_dorm_id):
        await update.effective_message.reply_text("You are not a member of this dorm.")
        return

    d_name = dorm.get("dorm_name") or "Dorm Cleaning Schedule"

    connected_count, total_count, connected_members, pending_members = await db.get_connection_status(target_dorm_id)

    lines = [f"**{d_name} Members** ({connected_count}/{total_count} Connected):\n"]

    def _uid(item):
        return getattr(item, "user_id", None) if not isinstance(item, dict) else item.get("user_id")

    def _uname(item):
        return getattr(item, "name", None) if not isinstance(item, dict) else item.get("name")

    connected_ids = {_uid(m) for m in connected_members if _uid(m) is not None}
    members = await db.get_dorm_members(target_dorm_id)
    if not members and target_dorm_id == default_gid:
        members = config.MEMBERS
    elif not members:
        await update.effective_message.reply_text(f"No members registered yet in {d_name}.")
        return

    for m in members:
        mid = _uid(m)
        mname = _uname(m) or f"ID {mid}"
        if mid in connected_ids:
            lines.append(f"• **{mname}**: Connected")
        else:
            lines.append(f"• **{mname}**: Waiting to send /start")

    if total_count >= 2 and connected_count >= total_count:
        lines.append("\nAll members are connected! The bot is fully functional.")
    elif pending_members:
        pending_names = ", ".join(_uname(m) or "Unknown" for m in pending_members)
        lines.append(f"\nWaiting for: {pending_names}")

    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def spin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Spin Dorm Roulette to pick a random roommate for a specified reason/chore."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else None

    if chat.type == "private" and not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user.id)
    if not dorm:
        if is_group:
            await update.effective_message.reply_text(
                "🧼 No active schedule found. Send `/setup` to initialize.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await reject_non_member(update)
        return

    target_dorm_id = dorm["dorm_id"]
    if is_group and not await check_member_permission(user.id, dorm_id=target_dorm_id):
        await update.effective_message.reply_text("You are not a member of this dorm.")
        return

    members = await db.get_dorm_members(target_dorm_id)
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    if not members and target_dorm_id == default_gid:
        members = config.MEMBERS
    elif not members:
        await update.effective_message.reply_text(f"No members found in {dorm.get('dorm_name', 'this dorm')} to spin.")
        return

    if not context.args or len(context.args) == 0:
        await update.effective_message.reply_text(
            "Please provide a reason or task for the spin.\n"
            "Usage: `/spin <reason>`\n"
            "Example: `/spin buy bread` or `/spin take out trash`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    task_title = " ".join(context.args).strip()
    chosen = random.choice(members)
    chosen_name = chosen["name"] if isinstance(chosen, dict) else chosen.name
    caller_name = user.first_name or "A roommate"

    roulette_msg = (
        f"🎯 **Dorm Roulette**\n\n"
        f"Reason: **{task_title}**\n"
        f"Chosen: **{chosen_name}** (spun by {caller_name})\n"
        f"Go get it done!"
    )
    await update.effective_message.reply_text(roulette_msg, parse_mode=ParseMode.MARKDOWN)

async def tsidat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Tsidat token leaderboard and balances."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if chat.type == "private" and not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    text, keyboard = await get_tsidat_content(dorm_id=dorm_id, user_id=user.id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def roast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deliver a witty, hilarious dorm roast & awards ceremony based on this week's cleaning."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else None

    if chat.type == "private" and not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user.id)
    if not dorm:
        await update.effective_message.reply_text("🧼 No active dorm schedule found.")
        return

    try:
        from telegram.constants import ChatAction
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    except Exception:
        pass

    from services.ai_service import generate_dorm_roast
    roast_text = await generate_dorm_roast(dorm["dorm_id"])
    await update.effective_message.reply_text(roast_text, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Interactive Inline Menu Callback Handler
# ---------------------------------------------------------------------------
async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button clicks for smooth menu navigation."""
    query = update.callback_query
    if not query:
        return

    user = query.from_user
    if not user:
        return

    if not await check_member_permission(user.id):
        await query.answer("Sorry, this bot is only for dorm members.", show_alert=True)
        return

    dorm = await _resolve_user_dorm(user.id)
    if not dorm:
        await query.answer("You are not registered in any active dorm.", show_alert=True)
        return
    dorm_id = dorm["dorm_id"]

    action = query.data
    member = await db.get_dorm_member(dorm_id, user.id)
    member_name = member["name"] if member else user.first_name
    is_cleaner, _, cleaners_str = await get_user_week_role(user.id, dorm_id=dorm_id)

    text = ""
    keyboard: Optional[InlineKeyboardMarkup] = None

    if action == "menu_home":
        code = dorm.get("dorm_code") or "DORM-101"
        text = await get_dorm_main_menu_text(member_name, dorm=dorm, is_cleaner=is_cleaner)
        keyboard = get_main_menu_inline_keyboard(is_cleaner=is_cleaner, dorm_code=code)
    elif action == "menu_status":
        text, keyboard = await get_status_content(user.id, dorm_id=dorm_id)
    elif action == "menu_schedule":
        text, keyboard = await get_schedule_content(dorm_id=dorm_id, user_id=user.id)
    elif action == "menu_done":
        if not is_cleaner:
            alert_text = f"You are not on cleaning duty this week! Cleaners: {cleaners_str}" if cleaners_str else "You are not on cleaning duty this week."
            try:
                await query.answer(alert_text, show_alert=True)
            except Exception:
                pass
            return
        try:
            await query.answer()
        except Exception:
            pass
        from handlers.cleaning import handle_cleaner_done
        await handle_cleaner_done(update, context, user.id, dorm_id=dorm_id)
        return
    elif action == "menu_fines":
        text, keyboard = await get_fines_content(dorm_id=dorm_id, user_id=user.id)
    elif action == "menu_history":
        text, keyboard = await get_history_content(dorm_id=dorm_id, user_id=user.id)
    elif action == "menu_help":
        text, keyboard = get_help_content()
    elif action == "menu_tsidat":
        text, keyboard = await get_tsidat_content(dorm_id=dorm_id, user_id=user.id)
    elif action == "menu_spin":
        text = (
            "🎯 **Dorm Roulette**\n\n"
            "To spin the wheel, send `/spin <reason>` in chat.\n"
            "Example: `/spin buy bread` or `/spin take out trash`"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("« Main Menu", callback_data="menu_home"),
            ]
        ])
    else:
        return

    try:
        await query.answer()
    except Exception:
        pass

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    except BadRequest as be:
        if "Message is not modified" not in str(be):
            logger.warning("BadRequest editing menu message: %s", be)
    except Exception as e:
        logger.warning("Could not edit message for action %s: %s", action, e)

# ---------------------------------------------------------------------------
# Quick Reply Keyboard Handler
# ---------------------------------------------------------------------------
async def reply_keyboard_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to clicks on the persistent bottom reply keyboard."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not msg.text:
        return

    chat = update.effective_chat
    if chat and chat.type == "private" and not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    text_lower = msg.text.strip().lower()

    if "status" in text_lower:
        await status_command(update, context)
    elif "schedule" in text_lower:
        await schedule_command(update, context)
    elif "clean" in text_lower or "submit" in text_lower:
        dorm = await _resolve_user_dorm(user.id)
        if not dorm:
            await msg.reply_text("You are not registered in any active dorm.")
            return
        dorm_id = dorm["dorm_id"]
        is_cleaner, _, cleaners_str = await get_user_week_role(user.id, dorm_id=dorm_id)
        if not is_cleaner:
            cleaners_info = f"This week's cleaners are **{cleaners_str}**." if cleaners_str else "Duty has not started yet."
            await msg.reply_text(
                f"ℹ️ **You are not on cleaning duty this week!**\n\n{cleaners_info}\n"
                "You don't need to submit anything. Use the buttons below to check your schedule or fines.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_reply_keyboard(is_cleaner=False)
            )
            return
        from handlers.cleaning import handle_cleaner_done
        await handle_cleaner_done(update, context, user.id, dorm_id=dorm_id)
    elif "fine" in text_lower:
        await fines_command(update, context)
    elif "history" in text_lower:
        await history_command(update, context)
    elif "member" in text_lower:
        await members_command(update, context)
    elif "tsidat" in text_lower or "token" in text_lower or "leaderboard" in text_lower:
        await tsidat_command(update, context)
    elif "spin" in text_lower or "roulette" in text_lower:
        await spin_command(update, context)
    elif "code" in text_lower:
        await dorm_code_command(update, context)
    elif "help" in text_lower or "guide" in text_lower:
        await help_command(update, context)
    elif "main menu" in text_lower or "menu" in text_lower or "home" in text_lower:
        await start_command(update, context)

# ---------------------------------------------------------------------------
# Roommate Slot Claim Command
# ---------------------------------------------------------------------------
async def claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow a roommate with a placeholder ID (e.g. Bilisuma) to link their Telegram account."""
    user = update.effective_user
    if not user:
        return

    user_dorms = await db.get_user_dorms(user.id)
    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    if user_dorms and any(d["dorm_id"] != default_gid for d in user_dorms):
        await update.effective_message.reply_text(
            f"⚠️ You are already a member of **{user_dorms[0]['dorm_name']}**.\n\n"
            "A student can only belong to one dorm at a time. Leave your current dorm first with `/leave`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if not context.args or len(context.args) < 1:
        await update.effective_message.reply_text(
            "Usage: `/claim <YourName>`\nExample: `/claim Bilisuma`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    name_input = context.args[0].strip().lower()
    target_member = None
    for m in getattr(config, "MEMBERS", []):
        if m.user_id == 111111111 and m.name.strip().lower() == name_input:
            target_member = m
            break

    if not target_member:
        await update.effective_message.reply_text(
            f"No unlinked member found matching '{context.args[0]}'. "
            "If your name is already claimed or registered, send /start to see your menu.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    old_id = target_member.user_id
    config.update_member_id(target_member.name, user.id)
    await db.update_member_id_in_db(old_id=old_id, new_id=user.id)
    logger.info("User %s (%s) claimed member slot %s", user.first_name, user.id, target_member.name)

    target_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    target_dorm = await _resolve_target_dorm(dorm_id=target_gid, user_id=user.id)
    target_code = target_dorm.get("dorm_code") if target_dorm else None
    is_cleaner, _, _ = await get_user_week_role(user.id, dorm_id=target_gid)
    reply_kb = get_reply_keyboard(is_cleaner=is_cleaner)
    inline_kb = get_main_menu_inline_keyboard(is_cleaner=is_cleaner, dorm_code=target_code)

    try:
        await update.effective_message.reply_text(
            "🎛️ Navigation keyboard ready below.",
            reply_markup=reply_kb
        )
    except Exception as e:
        logger.warning("Could not send reply keyboard in claim: %s", e)

    await update.effective_message.reply_text(
        f"🎉 Welcome **{target_member.name}**!\n\n"
        f"Your Telegram account (ID: `{user.id}`) is now linked to your dorm cleaning schedule.\n"
        "Tap below to view your schedule and status.",
        reply_markup=inline_kb,
        parse_mode=ParseMode.MARKDOWN
    )

# ---------------------------------------------------------------------------
# Leave Dorm Command
# ---------------------------------------------------------------------------
async def leave_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow a student moving rooms or leaving the dorm to cleanly leave."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else None

    # Resolve dorm
    if is_group:
        member = await db.get_dorm_member(dorm_id, user.id)
        if not member:
            await update.effective_message.reply_text("You are not a member of this dorm.")
            return
        target_dorm = await db.get_dorm(dorm_id)
    else:
        user_dorms = await db.get_user_dorms(user.id)
        if not user_dorms:
            await update.effective_message.reply_text("You are not currently a member of any active dorm.")
            return
        target_dorm = user_dorms[0]
        dorm_id = target_dorm["dorm_id"]

    success, msg = await db.leave_dorm(dorm_id, user.id)
    if not success:
        await update.effective_message.reply_text(f"⚠️ {msg}")
        return

    d_name = target_dorm["dorm_name"] if target_dorm else "the dorm"
    await update.effective_message.reply_text(
        f"👋 You have successfully left **{d_name}**.\n\n"
        "You can now join another dorm via its join link or set up a new room with `/setup`.",
        parse_mode=ParseMode.MARKDOWN
    )

    # If in private chat, notify the group
    if not is_group and dorm_id:
        try:
            member_name = user.first_name or f"User {user.id}"
            await context.bot.send_message(
                chat_id=dorm_id,
                message_thread_id=target_dorm.get("topic_id"),
                text=f"ℹ️ **{member_name}** has left the dorm.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

async def dorm_code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show this dorm's unique dorm code and web dashboard link."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else None
    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user.id)
    if not dorm:
        await msg.reply_text("You are not part of an active dorm. Use /setup to configure one.")
        return

    code = dorm.get("dorm_code") or "DORM-101"
    dash_base = getattr(config, "DASHBOARD_URL", "https://inspector.app.aletcloud.com").rstrip("/")
    web_url = f"{dash_base}/?code={code}"

    reply_text = (
        f"🔑 **Dorm Code for {dorm['dorm_name']}**\n\n"
        f"Code: `{code}`\n\n"
        f"🌐 **Website:** [Open Live Dashboard]({web_url})\n\n"
        f"Anyone can enter `{code}` on the website to view this dorm's live cleaning schedule, photo approvals, fines, and Tsidat leaderboard!"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open Live Dashboard", url=web_url)]
    ])
    await msg.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def set_dorm_code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to customize their dorm code."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else None
    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user.id)
    if not dorm:
        await msg.reply_text("Could not determine which dorm to update.")
        return

    target_dorm_id = dorm["dorm_id"]
    dorm_admin_id = dorm.get("admin_id") or getattr(config, "ADMIN_ID", 0)
    if user.id != dorm_admin_id and user.id != getattr(config, "ADMIN_ID", 0):
        await msg.reply_text("Only the dorm admin can change the dorm code.")
        return

    if not context.args:
        current_code = dorm.get("dorm_code") or "DORM-101"
        await msg.reply_text(
            f"Usage: `/set_code <new_code>`\n\n"
            f"Current Code: `{current_code}`\n"
            f"Example: `/set_code BLOCK3-102`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    new_code = context.args[0].strip().upper()
    success, result = await db.set_dorm_code(target_dorm_id, new_code)
    if not success:
        await msg.reply_text(f"⚠️ {result}")
        return

    dash_base = getattr(config, "DASHBOARD_URL", "https://inspector.app.aletcloud.com").rstrip("/")
    web_url = f"{dash_base}/?code={result}"
    await msg.reply_text(
        f"✅ **Dorm Code Updated!**\n\n"
        f"New Code: `{result}`\n"
        f"Dashboard Link: [Open Dashboard]({web_url})\n\n"
        f"Roommates can now use `{result}` on the website to view your dorm.",
        parse_mode=ParseMode.MARKDOWN
    )

async def set_name_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /name or /setname command to set or update member display name."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    target_dorm = await _resolve_user_dorm(user.id)
    dorm_id = target_dorm["dorm_id"] if target_dorm else None

    if not context.args:
        context.user_data["awaiting_name"] = dorm_id
        await update.effective_message.reply_text(
            "👤 **What is your name?**\n\n"
            "Usage: `/name <your_name>`\n"
            "Example: `/name Nahom`\n\n"
            "Or reply directly to this message with your name.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    new_name = " ".join(context.args).strip()
    new_name = re.sub(r"[*_`~[\]()#]", "", new_name).strip()
    if len(new_name) < 2 or len(new_name) > 40:
        await update.effective_message.reply_text("Please provide a name between 2 and 40 characters.")
        return

    await db.update_member_name(user.id, new_name, dorm_id=dorm_id)
    await db.set_setting(f"name_confirmed_{user.id}", "1")
    context.user_data.pop("awaiting_name", None)

    if getattr(config, "MEMBERS", None):
        for m in config.MEMBERS:
            if m.user_id == user.id:
                m.name = new_name
                break

    await update.effective_message.reply_text(
        f"✅ Your name has been set to **{new_name}**!",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_name_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture user's name when they reply in private chat after bot asked for it."""
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not chat or not user or not message or not message.text:
        return

    if chat.type != "private":
        return

    text = message.text.strip()
    if text.startswith("/"):
        return

    menu_buttons = {
        "status", "schedule", "submit cleaning", "fines", "history", "help",
        "main menu", "members", "tsidat", "tokens", "spin", "roulette",
        "dorm code", "code", "🔑 dorm code", "👥 members", "📋 status",
        "📅 schedule", "🧼 submit cleaning", "💸 fines", "📜 history",
        "ℹ️ help", "🏠 main menu", "🪙 tsidat tokens", "🎲 dorm roulette"
    }
    if text.lower() in menu_buttons:
        return

    if not context.user_data.get("awaiting_name"):
        return

    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    dorm_id = context.user_data.get("awaiting_name")

    # If the user belongs to our dorm or primary group, escape so their chat messages are never intercepted
    if config.is_member(user.id) or (dorm_id and dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)):
        context.user_data.pop("awaiting_name", None)
        return

    # Do not treat conversational queries, greetings, or sentences as names
    chat_prefixes = (
        "what", "who", "why", "how", "when", "where", "can", "is", "are",
        "do", "does", "hello", "hi", "hey", "sup", "tell", "explain", "please"
    )
    if "?" in text or text.lower().startswith(chat_prefixes) or len(text.split()) > 4:
        return

    dorm_id = context.user_data.pop("awaiting_name")
    new_name = re.sub(r"[*_`~[\]()#]", "", text).strip()
    if len(new_name) < 2 or len(new_name) > 40:
        await message.reply_text(
            "Please provide a real name between 2 and 40 characters (or type `/name <your_name>`)."
        )
        raise ApplicationHandlerStop

    await db.update_member_name(user.id, new_name, dorm_id=dorm_id)
    await db.set_setting(f"name_confirmed_{user.id}", "1")

    if getattr(config, "MEMBERS", None):
        for m in config.MEMBERS:
            if m.user_id == user.id:
                m.name = new_name
                break

    dorm = await db.get_dorm(dorm_id) if dorm_id else None
    d_name = dorm["dorm_name"] if dorm else "your dorm"
    code = dorm.get("dorm_code") if dorm else "DORM-101"
    is_cleaner, _, _ = await get_user_week_role(user.id, dorm_id=dorm_id)

    await message.reply_text(
        f"✅ Nice to meet you, **{new_name}**!\n\n"
        f"Your name is now registered for **{d_name}**.",
        reply_markup=get_reply_keyboard(is_cleaner=is_cleaner),
        parse_mode=ParseMode.MARKDOWN
    )
    await message.reply_text(
        "Main Menu:",
        reply_markup=get_main_menu_inline_keyboard(is_cleaner=is_cleaner, dorm_code=code),
        parse_mode=ParseMode.MARKDOWN
    )
    raise ApplicationHandlerStop

# ---------------------------------------------------------------------------
# Handler Registration
# ---------------------------------------------------------------------------
def register_command_handlers(application: Application) -> None:
    """Attach general command, inline callback, and reply keyboard handlers."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", start_command))
    application.add_handler(CommandHandler("name", set_name_command))
    application.add_handler(CommandHandler("setname", set_name_command))
    application.add_handler(CommandHandler("claim", claim_command))
    application.add_handler(CommandHandler("leave", leave_command))
    application.add_handler(CommandHandler("leavedorm", leave_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("code", dorm_code_command))
    application.add_handler(CommandHandler("dormcode", dorm_code_command))
    application.add_handler(CommandHandler("set_code", set_dorm_code_command))
    application.add_handler(CommandHandler("setcode", set_dorm_code_command))
    application.add_handler(CommandHandler("fines", fines_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("members", members_command))
    application.add_handler(CommandHandler("spin", spin_command))
    application.add_handler(CommandHandler("roulette", spin_command))
    application.add_handler(CommandHandler("tsidat", tsidat_command))
    application.add_handler(CommandHandler("tokens", tsidat_command))
    application.add_handler(CommandHandler("leaderboard", tsidat_command))
    application.add_handler(CommandHandler("roast", roast_command))
    application.add_handler(CommandHandler("awards", roast_command))

    application.add_handler(CallbackQueryHandler(menu_callback_handler, pattern=r"^menu_"))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^(Status|Schedule|Submit Cleaning|Fines|History|Help|Main Menu|Members|Tsidat|Tokens|Spin|Roulette|Dorm Code|Code|🔑 Dorm Code|👥 Members|📋 Status|📅 Schedule|🧼 Submit Cleaning|💸 Fines|📜 History|ℹ️ Help|🏠 Main Menu|🪙 Tsidat Tokens|🎲 Dorm Roulette)$"),
            reply_keyboard_text_handler
        )
    )

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            handle_name_reply
        )
    )
