"""
General Commands & Interactive Navigation Handler (Multi-Tenant Platform)
Handles /start, /help, /status, /schedule, /fines, /history, /menu, /members,
plus interactive inline buttons and persistent mobile keyboard.
Seamlessly handles both group-scoped commands and private DM interactions.
"""

from datetime import datetime, timedelta
import logging
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
from telegram.error import BadRequest
from telegram.ext import (
    Application,
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
    """Return True if user is a registered member of the dorm or platform."""
    if config.is_member(user_id):
        return True
    if dorm_id:
        member = await db.get_dorm_member(dorm_id, user_id)
        if member:
            return True
    user_dorms = await db.get_user_dorms(user_id)
    return len(user_dorms) > 0

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
    """Resolve primary dorm for a user in private chat."""
    user_dorms = await db.get_user_dorms(user_id)
    if user_dorms:
        return user_dorms[0]
    if config.is_member(user_id) and getattr(config, "GROUP_ID", 0):
        await db.seed_default_dorm()
        return await db.get_dorm(config.GROUP_ID)
    return None

# ---------------------------------------------------------------------------
# Keyboards & Navigation Builders
# ---------------------------------------------------------------------------
def get_main_menu_inline_keyboard() -> InlineKeyboardMarkup:
    """Main menu 6-button grid for one-tap navigation."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Status", callback_data="menu_status"),
            InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
        ],
        [
            InlineKeyboardButton("Submit Cleaning", callback_data="menu_done"),
            InlineKeyboardButton("Fines", callback_data="menu_fines"),
        ],
        [
            InlineKeyboardButton("History", callback_data="menu_history"),
            InlineKeyboardButton("Help", callback_data="menu_help"),
        ],
    ])

def get_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent mobile bottom keyboard for easy thumb navigation."""
    return ReplyKeyboardMarkup(
        [
            ["Status", "Schedule"],
            ["Submit Cleaning", "Fines"],
            ["History", "Help"],
            ["Main Menu"],
        ],
        resize_keyboard=True
    )

def get_main_menu_text(member_name: str, connected_info: Optional[str] = None, dorm_name: Optional[str] = None) -> str:
    """Welcome and overview text for the main menu."""
    current_date = config.get_current_date()
    week_info = config.get_week_info(current_date)

    status_suffix = f"\n\nMembers connected: **{connected_info}**" if connected_info else ""
    dorm_prefix = f"**{dorm_name}**\n\n" if dorm_name else ""

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
            f"Week {week_info.week_number} cleaners: **{week_info.cleaner1.name}** & **{week_info.cleaner2.name}**{status_suffix}\n\n"
            "Use the buttons below to check duty, fines, or submit cleaning."
        )

# ---------------------------------------------------------------------------
# View Content Generators (Text + Navigation Buttons)
# ---------------------------------------------------------------------------
async def get_status_content(user_id: int, dorm_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate status report text and navigation buttons."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
            InlineKeyboardButton("Submit Cleaning", callback_data="menu_done"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    if dorm_id is None:
        dorm = await _resolve_user_dorm(user_id)
        if dorm:
            dorm_id = dorm["dorm_id"]
        else:
            dorm_id = getattr(config, "GROUP_ID", 0)

    dorm = await db.get_dorm(dorm_id)
    if not dorm:
        return "No dorm found.", keyboard

    pairs = await db.get_dorm_pairs(dorm_id)
    members = await db.get_dorm_members(dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    from scheduler import get_dorm_week_info
    week_info = get_dorm_week_info(dorm, pairs, members_by_id)

    if not week_info or week_info.week_number < 1:
        start_date = dorm.get("start_date", "Soon")
        first_cleaners = f"{pairs[0]['cleaner1_id']} & {pairs[0]['cleaner2_id']}" if pairs else "TBD"
        if pairs:
            c1 = members_by_id.get(pairs[0]["cleaner1_id"], f"ID {pairs[0]['cleaner1_id']}")
            c2 = members_by_id.get(pairs[0]["cleaner2_id"], f"ID {pairs[0]['cleaner2_id']}")
            first_cleaners = f"**{c1}** & **{c2}**"
        msg = (
            f"Cleaning has not started yet for **{dorm['dorm_name']}**.\n\n"
            f"Starts: {start_date}\n"
            f"First cleaners: {first_cleaners}"
        )
        return msg, keyboard

    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)
    c1_name = week_info.cleaner1_name
    c2_name = week_info.cleaner2_name

    if week_record and week_record.get("approver_id"):
        approver_name = members_by_id.get(week_record["approver_id"], f"ID {week_record['approver_id']}")
    else:
        approver_name = "Not yet assigned"

    if not week_record or not week_record.get("announced"):
        duty_state = "Not announced yet."
    else:
        final_status = week_record.get("final_status", "pending")
        photo_status = week_record.get("photo_status", "none")
        check_sent = week_record.get("check_sent", 0)
        fine_amount = dorm.get("fine_amount", 100)

        if final_status == "completed":
            duty_state = "Cleaned and approved. No fines."
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
        f"({week_info.monday_date.strftime('%b %d')} – {week_info.friday_date.strftime('%b %d')})\n\n"
        f"Cleaners: **{c1_name}** & **{c2_name}**\n"
        f"Approver: **{approver_name}**\n"
        f"Status: {duty_state}"
    )
    return msg, keyboard

async def get_schedule_content(dorm_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate 4-week schedule text and navigation buttons."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Status", callback_data="menu_status"),
            InlineKeyboardButton("Fines", callback_data="menu_fines"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    resolved_id = dorm_id if dorm_id is not None else getattr(config, "GROUP_ID", 0)
    dorm = await db.get_dorm(resolved_id)
    pairs = await db.get_dorm_pairs(resolved_id)
    members = await db.get_dorm_members(resolved_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    if not dorm or not pairs:
        upcoming = config.get_upcoming_schedule(count=4)
        lines = ["Next 4 Weeks:\n"]
        for idx, w in enumerate(upcoming):
            tag = " (This Week)" if idx == 0 else ""
            date_str = f"{w.monday_date.strftime('%b %d')} – {w.friday_date.strftime('%b %d')}"
            lines.append(f"• Week {w.week_number} ({date_str}){tag}: {w.cleaner1.name} & {w.cleaner2.name}")
        return "\n".join(lines), keyboard

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

async def get_fines_content(dorm_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
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

    resolved_id = dorm_id if dorm_id is not None else getattr(config, "GROUP_ID", 0)
    dorm = await db.get_dorm(resolved_id)
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

async def get_history_content(dorm_id: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
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

    resolved_id = dorm_id if dorm_id is not None else getattr(config, "GROUP_ID", 0)
    dorm = await db.get_dorm(resolved_id)
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

        status = r.get("final_status", "pending")
        if status == "completed":
            badge = "Cleaned"
        elif status == "fined":
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
        "How Dorm Inspector Bot Works:\n\n"
        "• Monday 8:00 AM: Duty is announced (cleaners & approver)\n"
        "• Mon – Fri: Cleaners clean, tap 'We cleaned', and send a photo\n"
        "• Photo review: Approver approves or rejects the photo\n"
        "• Thursday 6:00 PM: Reminder if cleaning is not yet completed\n"
        "• Friday 8:00 PM: Check time. Cleaners confirm and approver gives final decision\n"
        "• Fines: Recorded in ledger if not cleaned\n"
        "• Weekend: Rest and off-duty!\n\n"
        "Commands:\n"
        "• /status: Check current week's cleaners and status\n"
        "• /schedule: View upcoming 4 weeks\n"
        "• /fines: See unpaid fines ledger\n"
        "• /history: See past week records\n"
        "• /members: See who is connected"
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
                await db.add_dorm_member(
                    dorm_id=dorm["dorm_id"],
                    user_id=user.id,
                    name=member_name,
                    chat_id=chat.id,
                    username=user.username,
                    is_admin=1 if user.id == dorm["admin_id"] else 0
                )
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

                welcome_dm = (
                    f"🎉 **You successfully joined {dorm['dorm_name']}!**\n\n"
                    "You are now registered in the dorm cleaning schedule.\n"
                    "You will receive duty announcements, reminders, and verification prompts here in DM.\n\n"
                    "Tap below to see your dorm's status."
                )
                await update.effective_message.reply_text(
                    welcome_dm,
                    reply_markup=get_main_menu_inline_keyboard(),
                    parse_mode=ParseMode.MARKDOWN
                )
                return

    # Auto-claim placeholder member if first name, username, or arg matches (e.g. Bilisuma with placeholder ID 111111111)
    if not await check_member_permission(user.id):
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

        d_name = target_dorm["dorm_name"] if target_dorm else None
        welcome_text = get_main_menu_text(member_name, connected_info=conn_str, dorm_name=d_name)
        inline_markup = get_main_menu_inline_keyboard()

        welcome_meme = config.get_meme_file("welcome")
        if welcome_meme:
            try:
                with open(welcome_meme, "rb") as photo:
                    await update.effective_message.reply_photo(
                        photo=photo,
                        caption=f"Welcome, {member_name}!",
                        reply_markup=ReplyKeyboardRemove(),
                        parse_mode=ParseMode.MARKDOWN
                    )
            except Exception as e:
                logger.warning("Could not send welcome meme: %s", e)

        await update.effective_message.reply_text(welcome_text, reply_markup=inline_markup, parse_mode=ParseMode.MARKDOWN)
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
    if not user or not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    text, keyboard = await get_status_content(user.id, dorm_id=dorm_id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the next 4 weeks of cleaning pairs."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    text, keyboard = await get_schedule_content(dorm_id=dorm_id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def fines_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show unpaid fine totals."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    text, keyboard = await get_fines_content(dorm_id=dorm_id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show last 8 weeks of cleaning results."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    text, keyboard = await get_history_content(dorm_id=dorm_id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def members_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show connection status of dorm members."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    dorm_id = chat.id if chat.type in ("group", "supergroup") else None
    if dorm_id is None:
        dorm = await _resolve_user_dorm(user.id)
        dorm_id = dorm["dorm_id"] if dorm else getattr(config, "GROUP_ID", 0)

    dorm = await db.get_dorm(dorm_id)
    d_name = dorm["dorm_name"] if dorm else "Dorm"

    connected_count, total_count, connected_members, pending_members = await db.get_connection_status(dorm_id)
    lines = [f"**{d_name} Members** ({connected_count}/{total_count} Connected):\n"]

    def _uid(item):
        return getattr(item, "user_id", None) if not isinstance(item, dict) else item.get("user_id")

    def _uname(item):
        return getattr(item, "name", None) if not isinstance(item, dict) else item.get("name")

    connected_ids = {_uid(m) for m in connected_members if _uid(m) is not None}
    members = await db.get_dorm_members(dorm_id)
    display_members = members if members else config.MEMBERS

    for m in display_members:
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

# ---------------------------------------------------------------------------
# Interactive Inline Menu Callback Handler
# ---------------------------------------------------------------------------
async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button clicks for smooth menu navigation."""
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user = query.from_user
    if not user:
        return

    if not await check_member_permission(user.id):
        await query.answer("Sorry, this bot is only for dorm members.", show_alert=True)
        return

    dorm = await _resolve_user_dorm(user.id)
    dorm_id = dorm["dorm_id"] if dorm else getattr(config, "GROUP_ID", 0)

    action = query.data
    member = await db.get_dorm_member(dorm_id, user.id)
    member_name = member["name"] if member else user.first_name

    text = ""
    keyboard: Optional[InlineKeyboardMarkup] = None

    if action == "menu_home":
        text = get_main_menu_text(member_name, dorm_name=dorm["dorm_name"] if dorm else None)
        keyboard = get_main_menu_inline_keyboard()
    elif action == "menu_status":
        text, keyboard = await get_status_content(user.id, dorm_id=dorm_id)
    elif action == "menu_schedule":
        text, keyboard = await get_schedule_content(dorm_id=dorm_id)
    elif action == "menu_done":
        from handlers.cleaning import handle_cleaner_done
        await handle_cleaner_done(update, context, user.id, dorm_id=dorm_id)
        return
    elif action == "menu_fines":
        text, keyboard = await get_fines_content(dorm_id=dorm_id)
    elif action == "menu_history":
        text, keyboard = await get_history_content(dorm_id=dorm_id)
    elif action == "menu_help":
        text, keyboard = get_help_content()
    else:
        return

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        pass
    except Exception as e:
        logger.warning("Could not edit message for action %s: %s", action, e)

# ---------------------------------------------------------------------------
# Quick Reply Keyboard Handler
# ---------------------------------------------------------------------------
async def reply_keyboard_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respond to clicks on the persistent bottom reply keyboard and dismiss it."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg or not msg.text:
        return

    if not await check_member_permission(user.id):
        await reject_non_member(update)
        return

    try:
        await msg.reply_text("Switching to clean inline buttons...", reply_markup=ReplyKeyboardRemove())
    except Exception:
        pass

    text_lower = msg.text.strip().lower()

    if "status" in text_lower:
        await status_command(update, context)
    elif "schedule" in text_lower:
        await schedule_command(update, context)
    elif "clean" in text_lower or "submit" in text_lower:
        dorm = await _resolve_user_dorm(user.id)
        dorm_id = dorm["dorm_id"] if dorm else getattr(config, "GROUP_ID", 0)
        from handlers.cleaning import handle_cleaner_done
        await handle_cleaner_done(update, context, user.id, dorm_id=dorm_id)
    elif "fine" in text_lower:
        await fines_command(update, context)
    elif "history" in text_lower:
        await history_command(update, context)
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

    await update.effective_message.reply_text(
        f"🎉 Welcome **{target_member.name}**!\n\n"
        f"Your Telegram account (ID: `{user.id}`) is now linked to your dorm cleaning schedule.\n"
        "Tap below to view your schedule and status.",
        reply_markup=get_main_menu_inline_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

# ---------------------------------------------------------------------------
# Handler Registration
# ---------------------------------------------------------------------------
def register_command_handlers(application: Application) -> None:
    """Attach general command, inline callback, and reply keyboard handlers."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", start_command))
    application.add_handler(CommandHandler("claim", claim_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("fines", fines_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("members", members_command))

    application.add_handler(CallbackQueryHandler(menu_callback_handler, pattern=r"^menu_"))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"(?i)^(Status|Schedule|Submit Cleaning|Fines|History|Help|Main Menu|📋 Status|📅 Schedule|🧼 Submit Cleaning|💸 Fines|📜 History|ℹ️ Help|🏠 Main Menu)$"),
            reply_keyboard_text_handler
        )
    )
