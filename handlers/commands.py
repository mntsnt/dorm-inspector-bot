"""
General Commands & Interactive Navigation Handler
Handles /start, /help, /status, /schedule, /fines, /history, /menu,
plus interactive inline buttons (Next, Prev, Back to Menu) and quick reply buttons.
"""

from datetime import datetime, timedelta
import logging
from typing import Optional, Tuple

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
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

def check_member_permission(user_id: int) -> bool:
    """Return True if user is one of the 8 dorm members, False otherwise."""
    return config.is_member(user_id)

async def reject_non_member(update: Update) -> None:
    """Politely reject users who are not in members.json."""
    if update.effective_message:
        await update.effective_message.reply_text(
            "Sorry, this bot is only for dorm members."
        )

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

def get_main_menu_text(member_name: str) -> str:
    """Welcome and overview text for the main menu."""
    current_date = config.get_current_date()
    week_info = config.get_week_info(current_date)

    if week_info.week_number < 1:
        first_cleaners = f"{config.PAIRS[0][0].name} & {config.PAIRS[0][1].name}"
        return (
            f"Hi **{member_name}**!\n\n"
            f"Week 1 starts: Monday, {config.START_BASE_MONDAY.strftime('%b %d')}\n"
            f"First cleaners: **{first_cleaners}**\n\n"
            "Use the buttons below to check duty, fines, or rules."
        )
    else:
        return (
            f"Hi **{member_name}**!\n\n"
            f"Week {week_info.week_number} cleaners: **{week_info.cleaner1.name}** & **{week_info.cleaner2.name}**\n\n"
            "Use the buttons below to check duty, fines, or submit cleaning."
        )

# ---------------------------------------------------------------------------
# View Content Generators (Text + Next/Prev/Back Buttons)
# ---------------------------------------------------------------------------
async def get_status_content(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate status report text and navigation buttons."""
    current_date = config.get_current_date()
    week_info = config.get_week_info(current_date)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
            InlineKeyboardButton("Submit Cleaning", callback_data="menu_done"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    if week_info.week_number < 1:
        first_pair = config.PAIRS[0]
        msg = (
            f"Cleaning has not started yet.\n\n"
            f"Starts: Monday, {config.START_BASE_MONDAY.strftime('%b %d')}\n"
            f"First cleaners: **{first_pair[0].name}** & **{first_pair[1].name}**"
        )
        return msg, keyboard

    week_record = await db.get_week(week_info.week_start)
    c1_name = week_info.cleaner1.name
    c2_name = week_info.cleaner2.name

    if week_record and week_record.get("approver_id"):
        approver = config.get_member(week_record["approver_id"])
        approver_name = approver.name if approver else f"ID {week_record['approver_id']}"
    else:
        approver_name = "Not yet assigned"

    if not week_record or not week_record.get("announced"):
        duty_state = "Not announced yet."
    else:
        final_status = week_record.get("final_status", "pending")
        photo_status = week_record.get("photo_status", "none")
        check_sent = week_record.get("check_sent", 0)

        if final_status == "completed":
            duty_state = "Cleaned and approved. No fines."
        elif final_status == "fined":
            duty_state = f"Not cleaned. Fined {config.FINE_TOTAL} ETB total."
        elif photo_status == "approved":
            duty_state = "Photo approved."
        elif photo_status == "submitted":
            duty_state = f"Photo sent. Waiting for {approver_name} to check."
        elif photo_status == "rejected":
            duty_state = f"Photo rejected. Cleaners need to clean again and send a new photo."
        elif week_record.get("done_reported"):
            duty_state = "Cleaners reported done. Waiting for photo."
        elif check_sent:
            duty_state = f"Friday check in progress. Waiting for {approver_name}."
        else:
            duty_state = "In progress (deadline Friday 8:00 PM)."

    msg = (
        f"Week #{week_info.week_number} ({week_info.monday_date.strftime('%b %d')} – {week_info.friday_date.strftime('%b %d')})\n\n"
        f"Cleaners: **{c1_name}** & **{c2_name}**\n"
        f"Approver: **{approver_name}**\n"
        f"Status: {duty_state}"
    )
    return msg, keyboard

def get_schedule_content() -> Tuple[str, InlineKeyboardMarkup]:
    """Generate 4-week schedule text and navigation buttons."""
    upcoming = config.get_upcoming_schedule(count=4)
    lines = ["Next 4 Weeks:\n"]
    for idx, w in enumerate(upcoming):
        tag = " (This Week)" if idx == 0 else ""
        date_str = f"{w.monday_date.strftime('%b %d')} – {w.friday_date.strftime('%b %d')}"
        lines.append(f"• Week {w.week_number} ({date_str}){tag}: {w.cleaner1.name} & {w.cleaner2.name}")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Status", callback_data="menu_status"),
            InlineKeyboardButton("Fines", callback_data="menu_fines"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])
    return "\n".join(lines), keyboard

async def get_fines_content() -> Tuple[str, InlineKeyboardMarkup]:
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

    unpaid_summary = await db.get_unpaid_fines_summary()
    if not unpaid_summary:
        msg = "No unpaid fines!"
        return msg, keyboard

    lines = ["Unpaid Fines:\n"]
    total_ledger = 0
    for entry in unpaid_summary:
        amount = entry["total_unpaid"]
        name = entry["user_name"]
        total_ledger += amount
        lines.append(f"• **{name}**: {amount} ETB")

    lines.append(f"\nTotal: {total_ledger} ETB")
    return "\n".join(lines), keyboard

async def get_history_content() -> Tuple[str, InlineKeyboardMarkup]:
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

    records = await db.get_history(limit=8)
    if not records:
        msg = "No history yet."
        return msg, keyboard

    lines = ["Past Weeks:\n"]
    for r in records:
        w_start = r["week_start"]
        c1 = config.get_member(r["cleaner1_id"])
        c2 = config.get_member(r["cleaner2_id"])

        c1_name = c1.name if c1 else f"ID {r['cleaner1_id']}"
        c2_name = c2.name if c2 else f"ID {r['cleaner2_id']}"

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
        "How it works:\n\n"
        "• Monday 8:00 AM: Duty is announced (cleaners & approver)\n"
        "• Monday – Friday: Cleaners clean, tap 'We cleaned', and send a photo\n"
        "• Photo review: Approver approves or rejects the photo\n"
        "• Thursday 6:00 PM: Reminder if not done yet\n"
        "• Friday 8:00 PM: Check time. Cleaners confirm and approver gives final decision\n"
        "• Fines: 50 ETB each (100 ETB total) if not cleaned\n"
        "• Weekend: Off"
    )
    return help_text, keyboard

async def get_done_content(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Generate cleaning submission instruction text and navigation buttons."""
    week_info = config.get_week_info()
    week_record = await db.get_week(week_info.week_start)

    c1_id = week_record["cleaner1_id"] if week_record else week_info.cleaner1.user_id
    c2_id = week_record["cleaner2_id"] if week_record else week_info.cleaner2.user_id

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Status", callback_data="menu_status"),
            InlineKeyboardButton("Schedule", callback_data="menu_schedule"),
        ],
        [
            InlineKeyboardButton("Main Menu", callback_data="menu_home"),
        ]
    ])

    if user_id not in (c1_id, c2_id):
        c1 = config.get_member(c1_id)
        c2 = config.get_member(c2_id)
        c1_name = c1.name if c1 else f"ID {c1_id}"
        c2_name = c2.name if c2 else f"ID {c2_id}"
        msg = f"You are not on cleaning duty this week.\nCleaners: **{c1_name}** & **{c2_name}**."
        return msg, keyboard

    if week_record and week_record.get("final_status") == "completed":
        msg = "Cleaning for this week is already done!"
        return msg, keyboard

    await db.mark_done_reported(week_info.week_start)

    msg = "Please send a photo of the cleaned dorm in this chat."
    return msg, keyboard

# ---------------------------------------------------------------------------
# Command Handlers (/start, /help, /status, /schedule, /fines, /history)
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register member's chat_id and send the interactive start menu."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not check_member_permission(user.id):
        await reject_non_member(update)
        return

    member = config.get_member(user.id)
    member_name = member.name if member else user.first_name

    # Persist user chat_id for private DM capability
    if chat.type == "private":
        await db.upsert_user(
            user_id=user.id,
            name=member_name,
            chat_id=chat.id,
            username=user.username
        )

    welcome_text = get_main_menu_text(member_name)
    inline_markup = get_main_menu_inline_keyboard()

    welcome_meme = config.get_meme_file("welcome")
    if chat.type == "private":
        reply_markup = get_reply_keyboard()
        if welcome_meme:
            with open(welcome_meme, "rb") as photo:
                await update.effective_message.reply_photo(
                    photo=photo,
                    caption=welcome_text,
                    reply_markup=inline_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await update.effective_message.reply_text(
                welcome_text,
                reply_markup=inline_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        await update.effective_message.reply_text(
            "Buttons are also available below.",
            reply_markup=reply_markup
        )
    else:
        if welcome_meme:
            with open(welcome_meme, "rb") as photo:
                await update.effective_message.reply_photo(
                    photo=photo,
                    caption=welcome_text,
                    reply_markup=inline_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await update.effective_message.reply_text(
                welcome_text,
                reply_markup=inline_markup,
                parse_mode=ParseMode.MARKDOWN
            )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send help instructions with navigation buttons."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    text, keyboard = get_help_content()
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current week's cleaners, approver, and duty status with navigation buttons."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    text, keyboard = await get_status_content(user.id)
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the next 4 weeks of cleaning pairs with navigation buttons."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    text, keyboard = get_schedule_content()
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def fines_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show unpaid fine totals with navigation buttons."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    text, keyboard = await get_fines_content()
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show last 8 weeks of cleaning results with navigation buttons."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    text, keyboard = await get_history_content()
    await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Interactive Inline Menu Callback Handler (Back / Forward / Menu Navigation)
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

    if not check_member_permission(user.id):
        await query.answer("Sorry, this bot is only for dorm members.", show_alert=True)
        return

    action = query.data
    member = config.get_member(user.id)
    member_name = member.name if member else user.first_name

    text = ""
    keyboard: Optional[InlineKeyboardMarkup] = None

    if action == "menu_home":
        text = get_main_menu_text(member_name)
        keyboard = get_main_menu_inline_keyboard()
    elif action == "menu_status":
        text, keyboard = await get_status_content(user.id)
    elif action == "menu_schedule":
        text, keyboard = get_schedule_content()
    elif action == "menu_done":
        text, keyboard = await get_done_content(user.id)
    elif action == "menu_fines":
        text, keyboard = await get_fines_content()
    elif action == "menu_history":
        text, keyboard = await get_history_content()
    elif action == "menu_help":
        text, keyboard = get_help_content()
    else:
        return

    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        # Ignore if content hasn't changed
        pass
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

    if not check_member_permission(user.id):
        await reject_non_member(update)
        return

    text_lower = msg.text.strip().lower()

    if "status" in text_lower:
        await status_command(update, context)
    elif "schedule" in text_lower:
        await schedule_command(update, context)
    elif "clean" in text_lower or "submit" in text_lower:
        member_id = user.id
        resp_text, keyboard = await get_done_content(member_id)
        await msg.reply_text(resp_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    elif "fine" in text_lower:
        await fines_command(update, context)
    elif "history" in text_lower:
        await history_command(update, context)
    elif "help" in text_lower or "guide" in text_lower:
        await help_command(update, context)
    elif "main menu" in text_lower or "menu" in text_lower or "home" in text_lower:
        await start_command(update, context)

# ---------------------------------------------------------------------------
# Handler Registration
# ---------------------------------------------------------------------------
def register_command_handlers(application: Application) -> None:
    """Attach general command, inline callback, and reply keyboard handlers."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("fines", fines_command))
    application.add_handler(CommandHandler("history", history_command))

    # Register inline menu navigation callback handler
    application.add_handler(CallbackQueryHandler(menu_callback_handler, pattern=r"^menu_"))

    # Register persistent reply keyboard text handler
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(r"^(Status|Schedule|Submit Cleaning|Fines|History|Help|Main Menu|📋 Status|📅 Schedule|🧼 Submit Cleaning|💸 Fines|📜 History|ℹ️ Help|🏠 Main Menu)$"),
            reply_keyboard_text_handler
        )
    )
