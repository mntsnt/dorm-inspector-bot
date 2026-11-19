"""
General Commands Handler: /start, /help, /status, /schedule, /fines, /history
"""

from datetime import datetime, timedelta
import logging
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import db

logger = logging.getLogger(__name__)

def check_member_permission(user_id: int) -> bool:
    """Return True if user is one of the 8 members, False otherwise."""
    return config.is_member(user_id)

async def reject_non_member(update: Update) -> None:
    """Politely reject users who are not in members.json."""
    if update.effective_message:
        await update.effective_message.reply_text(
            "🚫 Sorry, this bot is exclusively for residents of the dorm."
        )

# ---------------------------------------------------------------------------
# /start Command
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register member's chat_id and send a welcome message."""
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

    welcome_text = (
        f"👋 **Welcome, {member_name}!**\n\n"
        "You are registered with the **Dorm Inspector Bot**.\n"
        "I coordinate the weekly cleaning rotation, approvals, and reminders.\n\n"
        "**Available Commands:**\n"
        "• /status — Check this week's cleaners, approver & state\n"
        "• /schedule — View the upcoming 4-week cleaning schedule\n"
        "• /done — Report cleaning finished & submit photo\n"
        "• /fines — View the unpaid fines ledger\n"
        "• /history — View cleaning records of past 8 weeks\n"
        "• /help — Full guide and instructions"
    )
    await update.effective_message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# /help Command
# ---------------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send help instructions."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    help_text = (
        "🧹 **Dorm Inspector Bot — User Guide**\n\n"
        "**Weekly Cleaning Schedule:**\n"
        "• **Monday 08:00 AM**: Bot announces this week's 2 cleaners & randomly chosen approver.\n"
        "• **Mon – Fri**: Cleaners do the cleaning, click /done (or 'We cleaned'), and send a photo.\n"
        "• **Photo Approval**: Approver reviews the photo in private DM (Approve / Reject).\n"
        "• **Thursday 06:00 PM**: Reminder sent to cleaners if cleaning is not completed.\n"
        "• **Friday 08:00 PM (Check Time)**: Final verification. Bot asks cleaners if they cleaned.\n"
        "• **Approver Confirmation**: Approver has 12 hours to confirm final result. Fines are only created after approver's decision.\n"
        "• **Weekend**: Saturday & Sunday have no cleaning duty.\n\n"
        "**Commands:**\n"
        "/status — Current duty status\n"
        "/schedule — Upcoming 4-week pair schedule\n"
        "/done — Report cleaning done and send photo\n"
        "/fines — View unpaid fines\n"
        "/history — Past 8 weeks history\n"
        "/markpaid `<member>` — (Admin only) Mark fines as paid"
    )
    await update.effective_message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# /status Command
# ---------------------------------------------------------------------------
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current week's cleaners, approver, and duty status."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    current_date = config.get_current_date()
    current_time = config.get_current_time()
    week_info = config.get_week_info(current_date)

    if week_info.week_number < 1:
        first_pair = config.PAIRS[0]
        msg = (
            f"⏳ **Cleaning duty has not started yet.**\n\n"
            f"Week #1 officially begins on **Monday, {config.START_BASE_MONDAY.strftime('%B %d, %Y')}**.\n"
            f"🧹 **First Cleaners:** {first_pair[0].name} & {first_pair[1].name}\n"
            f"📢 The announcement will be sent at 08:00 AM on Monday, {config.START_BASE_MONDAY.strftime('%b %d')}."
        )
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    week_record = await db.get_week(week_info.week_start)

    # Cleaners
    c1_name = week_info.cleaner1.name
    c2_name = week_info.cleaner2.name

    # Approver
    if week_record and week_record.get("approver_id"):
        approver = config.get_member(week_record["approver_id"])
        approver_name = approver.name if approver else f"ID {week_record['approver_id']}"
    else:
        approver_name = "Not yet assigned (will be picked on Monday announcement)"

    # Determine status string
    if not week_record or not week_record.get("announced"):
        duty_state = "⏳ Awaiting Monday morning announcement."
    else:
        final_status = week_record.get("final_status", "pending")
        photo_status = week_record.get("photo_status", "none")
        check_sent = week_record.get("check_sent", 0)

        if final_status == "completed":
            duty_state = "✅ **Completed** — Cleaning has been approved! No fines."
        elif final_status == "fined":
            duty_state = f"❌ **Missed / Fined** — A fine of {config.FINE_TOTAL} ETB total was recorded."
        elif photo_status == "approved":
            duty_state = "✅ **Photo Approved** by approver! Good job."
        elif photo_status == "submitted":
            duty_state = f"📸 **Photo Submitted** — Waiting for approval from {approver_name}."
        elif photo_status == "rejected":
            duty_state = f"⚠️ **Photo Rejected** by {approver_name}. Cleaners must redo and resubmit before Friday 8 PM!"
        elif week_record.get("done_reported"):
            duty_state = "⏳ Cleaners reported 'We cleaned' — Waiting for photo submission."
        elif check_sent:
            duty_state = f"⚖️ **Friday Check in progress** — Waiting for final confirmation from {approver_name}."
        else:
            duty_state = "🧹 **In Progress** — Cleaners must clean and submit photo before Friday 8:00 PM."

    msg = (
        f"📋 **Current Cleaning Status (Week #{week_info.week_number})**\n\n"
        f"📅 **Week:** {week_info.monday_date.strftime('%b %d, %Y')} – {week_info.friday_date.strftime('%b %d, %Y')}\n"
        f"🧹 **Cleaners:** {c1_name} & {c2_name}\n"
        f"👀 **Approver:** {approver_name}\n\n"
        f"**State:**\n{duty_state}"
    )
    await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# /schedule Command
# ---------------------------------------------------------------------------
async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the next 4 weeks of cleaning pairs with dates."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    upcoming = config.get_upcoming_schedule(count=4)
    lines = ["📅 **Upcoming Cleaning Schedule (Next 4 Weeks)**:\n"]
    for idx, w in enumerate(upcoming):
        tag = " (This Week)" if idx == 0 else ""
        date_str = f"{w.monday_date.strftime('%b %d')} – {w.friday_date.strftime('%b %d')}"
        lines.append(
            f"• **Week {w.week_number}** ({date_str}){tag}:\n"
            f"   🧹 **{w.cleaner1.name}** & **{w.cleaner2.name}**"
        )

    lines.append("\n_Note: Each member cleans once every 4 weeks in a fixed rotation._")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# /fines Command
# ---------------------------------------------------------------------------
async def fines_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show each member's unpaid fine total."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    unpaid_summary = await db.get_unpaid_fines_summary()
    if not unpaid_summary:
        await update.effective_message.reply_text(
            "🎉 **Clean slate!** There are currently no unpaid fines in the dorm."
        )
        return

    lines = ["💸 **Unpaid Fines Ledger**:\n"]
    total_ledger = 0
    for entry in unpaid_summary:
        amount = entry["total_unpaid"]
        count = entry["unpaid_count"]
        name = entry["user_name"]
        total_ledger += amount
        lines.append(f"• **{name}**: {amount} ETB ({count} missed week{'s' if count > 1 else ''})")

    lines.append(f"\n**Total Outstanding Fines:** {total_ledger} ETB")
    lines.append("_Admin can clear paid fines using /markpaid <member>_")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# /history Command
# ---------------------------------------------------------------------------
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show last 8 weeks of cleaning results."""
    user = update.effective_user
    if not user or not check_member_permission(user.id):
        await reject_non_member(update)
        return

    records = await db.get_history(limit=8)
    if not records:
        await update.effective_message.reply_text(
            "📜 No cleaning history recorded yet."
        )
        return

    lines = ["📜 **Cleaning History (Last 8 Weeks)**:\n"]
    for r in records:
        w_start = r["week_start"]
        c1 = config.get_member(r["cleaner1_id"])
        c2 = config.get_member(r["cleaner2_id"])
        app = config.get_member(r["approver_id"])

        c1_name = c1.name if c1 else f"ID {r['cleaner1_id']}"
        c2_name = c2.name if c2 else f"ID {r['cleaner2_id']}"
        app_name = app.name if app else f"ID {r['approver_id']}"

        status = r.get("final_status", "pending")
        if status == "completed":
            badge = "✅ Completed"
        elif status == "fined":
            badge = "❌ Fined"
        else:
            badge = "⏳ In Progress"

        lines.append(
            f"• **Week {r['week_number']}** ({w_start}):\n"
            f"   Cleaners: {c1_name} & {c2_name} | Approver: {app_name}\n"
            f"   Result: {badge}\n"
        )

    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

def register_command_handlers(application: Application) -> None:
    """Attach general command handlers."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("fines", fines_command))
    application.add_handler(CommandHandler("history", history_command))
