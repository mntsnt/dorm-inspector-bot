"""
Approver Actions Handler
Handles photo approval/rejection and Friday final confirmation decisions.
The approver has the final authority; fines are only created upon their decision or timeout fallback.
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

import config
import db

logger = logging.getLogger(__name__)

async def _notify_cleaner(
    context: ContextTypes.DEFAULT_TYPE,
    cleaner_id: int,
    cleaner_name: str,
    text: str
) -> None:
    """Send private DM to cleaner, falling back to group message if DM fails."""
    try:
        await context.bot.send_message(
            chat_id=cleaner_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN
        )
    except TelegramError as te:
        logger.warning("Could not DM cleaner %s (%s): %s", cleaner_id, cleaner_name, te)
        try:
            bot_info = await context.bot.get_me()
            bot_user = bot_info.username or "the bot"
            fallback = (
                f"⚠️ Notification for cleaner **{cleaner_name}**:\n"
                f"{text}\n\n"
                f"_(Please open a private chat with @{bot_user} and send /start!)_"
            )
            await context.bot.send_message(
                chat_id=config.GROUP_ID,
                text=fallback,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as ge:
            logger.error("Failed to send fallback message to group: %s", ge)

# ---------------------------------------------------------------------------
# Photo Review Callbacks: Approve / Reject
# ---------------------------------------------------------------------------
async def approve_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver approves the cleaning photo."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    parts = query.data.split("_", 1)
    week_start = parts[1] if len(parts) > 1 else ""

    week_record = await db.get_week(week_start)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    approver_id = week_record["approver_id"]
    approver = config.get_member(approver_id)
    approver_name = approver.name if approver else f"ID {approver_id}"

    if user.id != approver_id:
        await query.answer(
            f"🚫 Only the assigned approver ({approver_name}) can approve this!",
            show_alert=True
        )
        return

    if week_record.get("final_status") == "completed":
        await query.answer("This week's cleaning is already completed.", show_alert=True)
        return

    # Update database: mark approved and completed
    await db.update_photo_status(week_start, status="approved", final_status="completed")

    c1 = config.get_member(week_record["cleaner1_id"])
    c2 = config.get_member(week_record["cleaner2_id"])
    c1_name = c1.name if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2.name if c2 else f"ID {week_record['cleaner2_id']}"

    # Update approver's message
    await query.edit_message_caption(
        caption=f"✅ **You approved the cleaning photo for week starting {week_start}.**\nThank you!",
        parse_mode=ParseMode.MARKDOWN
    )

    # Post result in group chat
    group_msg = (
        f"🎉 **Cleaning Approved!**\n\n"
        f"Approver **{approver_name}** has approved the cleaning by **{c1_name}** and **{c2_name}**.\n"
        "Duty is marked as completed for this week. No fines! 👏"
    )
    await context.bot.send_message(
        chat_id=config.GROUP_ID,
        text=group_msg,
        parse_mode=ParseMode.MARKDOWN
    )

    # Notify cleaners in private DM
    dm_text = (
        f"🎉 **Great job!** Approver **{approver_name}** has approved your cleaning photo.\n"
        "Your duty for this week is officially completed. No fines!"
    )
    await _notify_cleaner(context, week_record["cleaner1_id"], c1_name, dm_text)
    await _notify_cleaner(context, week_record["cleaner2_id"], c2_name, dm_text)

async def reject_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver rejects the cleaning photo."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    parts = query.data.split("_", 1)
    week_start = parts[1] if len(parts) > 1 else ""

    week_record = await db.get_week(week_start)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    approver_id = week_record["approver_id"]
    approver = config.get_member(approver_id)
    approver_name = approver.name if approver else f"ID {approver_id}"

    if user.id != approver_id:
        await query.answer(
            f"🚫 Only the assigned approver ({approver_name}) can reject this!",
            show_alert=True
        )
        return

    # Update database: mark rejected
    await db.update_photo_status(week_start, status="rejected")

    c1 = config.get_member(week_record["cleaner1_id"])
    c2 = config.get_member(week_record["cleaner2_id"])
    c1_name = c1.name if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2.name if c2 else f"ID {week_record['cleaner2_id']}"

    # Update approver's message
    await query.edit_message_caption(
        caption=f"❌ **You rejected the cleaning photo for week starting {week_start}.**\nThe cleaners have been notified to redo it.",
        parse_mode=ParseMode.MARKDOWN
    )

    # Post in group chat
    group_msg = (
        f"❌ **Cleaning Photo Rejected**\n\n"
        f"Approver **{approver_name}** rejected the cleaning photo.\n"
        f"Cleaners **{c1_name}** and **{c2_name}** must redo the cleaning and send a new photo before Friday 8:00 PM!"
    )
    await context.bot.send_message(
        chat_id=config.GROUP_ID,
        text=group_msg,
        parse_mode=ParseMode.MARKDOWN
    )

    # Notify cleaners in private DM
    dm_text = (
        f"⚠️ **Attention:** Approver **{approver_name}** has rejected your cleaning photo.\n\n"
        "Please redo the cleaning and send an updated photo using the 'We cleaned' button or /done before Friday 8:00 PM!"
    )
    await _notify_cleaner(context, week_record["cleaner1_id"], c1_name, dm_text)
    await _notify_cleaner(context, week_record["cleaner2_id"], c2_name, dm_text)

# ---------------------------------------------------------------------------
# Friday Final Confirmation Callbacks: Confirm Cleaned / Confirm Not Cleaned
# ---------------------------------------------------------------------------
async def confirm_cleaned_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver confirms the final result: Cleaned."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    parts = query.data.split("_", 2)
    week_start = parts[2] if len(parts) > 2 else ""

    week_record = await db.get_week(week_start)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    approver_id = week_record["approver_id"]
    approver = config.get_member(approver_id)
    approver_name = approver.name if approver else f"ID {approver_id}"

    if user.id != approver_id:
        await query.answer(
            f"🚫 Only the assigned approver ({approver_name}) can confirm this!",
            show_alert=True
        )
        return

    if week_record.get("final_status") in ("completed", "fined"):
        await query.answer("This week's status has already been finalized.", show_alert=True)
        return

    # Record approver decision
    await db.record_approver_decision(week_start, decision="cleaned", final_status="completed")

    c1 = config.get_member(week_record["cleaner1_id"])
    c2 = config.get_member(week_record["cleaner2_id"])
    c1_name = c1.name if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2.name if c2 else f"ID {week_record['cleaner2_id']}"

    # Edit approver message
    await query.edit_message_text(
        f"✅ **You confirmed that the dorm was cleaned for week starting {week_start}.**\nNo fines recorded."
    )

    # Post in group chat
    group_msg = (
        f"🎉 **Final Confirmation:**\n\n"
        f"Approver **{approver_name}** has confirmed that **{c1_name}** & **{c2_name}** cleaned the dorm.\n"
        "Duty complete for this week. No fines! 🌟"
    )
    await context.bot.send_message(
        chat_id=config.GROUP_ID,
        text=group_msg,
        parse_mode=ParseMode.MARKDOWN
    )

    # Notify cleaners
    dm_text = (
        f"🎉 Approver **{approver_name}** has confirmed that you cleaned the dorm!\n"
        "Your duty is officially complete. No fines."
    )
    await _notify_cleaner(context, week_record["cleaner1_id"], c1_name, dm_text)
    await _notify_cleaner(context, week_record["cleaner2_id"], c2_name, dm_text)

async def confirm_not_cleaned_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver confirms the final result: Not Cleaned (Fines recorded)."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    parts = query.data.split("_", 2)
    week_start = parts[2] if len(parts) > 2 else ""

    week_record = await db.get_week(week_start)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    approver_id = week_record["approver_id"]
    approver = config.get_member(approver_id)
    approver_name = approver.name if approver else f"ID {approver_id}"

    if user.id != approver_id:
        await query.answer(
            f"🚫 Only the assigned approver ({approver_name}) can confirm this!",
            show_alert=True
        )
        return

    if week_record.get("final_status") in ("completed", "fined"):
        await query.answer("This week's status has already been finalized.", show_alert=True)
        return

    # Record approver decision
    await db.record_approver_decision(week_start, decision="not_cleaned", final_status="fined")

    c1 = config.get_member(week_record["cleaner1_id"])
    c2 = config.get_member(week_record["cleaner2_id"])
    c1_name = c1.name if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2.name if c2 else f"ID {week_record['cleaner2_id']}"

    # Create fines in ledger (100 ETB total: 50 ETB each)
    await db.create_fines_for_week(
        week_start=week_start,
        cleaner1_id=week_record["cleaner1_id"],
        cleaner1_name=c1_name,
        cleaner2_id=week_record["cleaner2_id"],
        cleaner2_name=c2_name,
        fine_each=config.FINE_EACH
    )

    # Edit approver message
    await query.edit_message_text(
        f"❌ **You confirmed that the dorm was NOT cleaned for week starting {week_start}.**\n"
        f"A fine of {config.FINE_TOTAL} ETB total ({config.FINE_EACH} ETB each) has been recorded."
    )

    # Post in group chat
    group_msg = (
        f"🚨 **Final Confirmation:**\n\n"
        f"Approver **{approver_name}** has confirmed that **{c1_name}** & **{c2_name}** did **NOT** clean the dorm.\n\n"
        f"💸 **Fines Recorded:**\n"
        f"• **{c1_name}**: {config.FINE_EACH} ETB\n"
        f"• **{c2_name}**: {config.FINE_EACH} ETB\n"
        f"Total: {config.FINE_TOTAL} ETB\n\n"
        "Use /fines to view outstanding fines."
    )
    await context.bot.send_message(
        chat_id=config.GROUP_ID,
        text=group_msg,
        parse_mode=ParseMode.MARKDOWN
    )

    # Notify cleaners
    dm_text = (
        f"🚨 **Fine Notice:** Approver **{approver_name}** confirmed the dorm was NOT cleaned this week.\n"
        f"A fine of **{config.FINE_EACH} ETB** has been recorded for you in the ledger.\n"
        "Please settle your fine with the admin."
    )
    await _notify_cleaner(context, week_record["cleaner1_id"], c1_name, dm_text)
    await _notify_cleaner(context, week_record["cleaner2_id"], c2_name, dm_text)

def register_approval_handlers(application: Application) -> None:
    """Attach approver decision handlers."""
    application.add_handler(CallbackQueryHandler(approve_photo_callback, pattern=r"^approve_"))
    application.add_handler(CallbackQueryHandler(reject_photo_callback, pattern=r"^reject_"))
    application.add_handler(CallbackQueryHandler(confirm_cleaned_callback, pattern=r"^fconfirm_clean_"))
    application.add_handler(CallbackQueryHandler(confirm_not_cleaned_callback, pattern=r"^fconfirm_notclean_"))
