"""
Approver Actions Handler (Multi-Tenant Platform)
Handles photo approval/rejection and Friday final confirmation decisions.
The approver has the final authority; fines are only created upon their decision or timeout fallback.
Properly scoped per dorm with group topic and DM notifications.
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

import config
import db

logger = logging.getLogger(__name__)

def _parse_callback_dorm_and_week(callback_data: str) -> Tuple[int, str]:
    """Parse callback data which may be 'prefix_{dorm_id}_{week_start}' or 'prefix_{week_start}'."""
    parts = callback_data.split("_")
    if len(parts) >= 4 and parts[0] == "fconfirm":
        if "-" in parts[3]:
            try:
                return int(parts[2]), parts[3]
            except ValueError:
                pass
        return getattr(config, "GROUP_ID", 0), parts[-1]
    elif len(parts) == 3:
        try:
            return int(parts[1]), parts[2]
        except ValueError:
            return getattr(config, "GROUP_ID", 0), parts[2]
    elif len(parts) == 2:
        return getattr(config, "GROUP_ID", 0), parts[1]
    return getattr(config, "GROUP_ID", 0), ""

async def _notify_cleaner(
    context: ContextTypes.DEFAULT_TYPE,
    cleaner_id: int,
    cleaner_name: str,
    text: str,
    photo_path: Optional[Path] = None,
    dorm_id: Optional[int] = None,
    topic_id: Optional[int] = None,
) -> None:
    """Send private DM to cleaner, falling back to group message if DM fails."""
    try:
        if photo_path and photo_path.is_file():
            with open(photo_path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=cleaner_id,
                    photo=photo,
                    caption=text,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=cleaner_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN
            )
    except TelegramError as te:
        logger.warning("Could not DM cleaner %s (%s): %s", cleaner_id, cleaner_name, te)
        target_group = dorm_id if dorm_id is not None else getattr(config, "GROUP_ID", 0)
        target_topic = topic_id if topic_id is not None else getattr(config, "TOPIC_ID", None)
        if not target_group:
            return

        try:
            bot_info = await context.bot.get_me()
            bot_user = bot_info.username or "bot"
            fallback = (
                f"For **{cleaner_name}**:\n"
                f"{text}\n\n"
                f"_(Could not DM you. Please message @{bot_user} and send /start)_"
            )
            if photo_path and photo_path.is_file():
                with open(photo_path, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=target_group,
                        message_thread_id=target_topic,
                        photo=photo,
                        caption=fallback,
                        parse_mode=ParseMode.MARKDOWN
                    )
            else:
                await context.bot.send_message(
                    chat_id=target_group,
                    message_thread_id=target_topic,
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

    dorm_id, week_start = _parse_callback_dorm_and_week(query.data)
    dorm = await db.get_dorm(dorm_id)
    topic_id = dorm.get("topic_id") if dorm else None

    week_record = await db.get_week(week_start, dorm_id=dorm_id)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    approver_id = week_record["approver_id"]
    approver = await db.get_dorm_member(dorm_id, approver_id)
    approver_name = approver["name"] if approver else f"ID {approver_id}"

    if user.id != approver_id:
        await query.answer(
            f"Only the assigned approver ({approver_name}) can approve this.",
            show_alert=True
        )
        return

    if week_record.get("final_status") == "completed":
        await query.answer("Cleaning is already completed.", show_alert=True)
        return

    # Update database: mark approved and completed
    await db.update_photo_status(week_start, status="approved", final_status="completed", dorm_id=dorm_id)

    c1 = await db.get_dorm_member(dorm_id, week_record["cleaner1_id"])
    c2 = await db.get_dorm_member(dorm_id, week_record["cleaner2_id"])
    c1_name = c1["name"] if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2["name"] if c2 else f"ID {week_record['cleaner2_id']}"

    # Update approver's message
    await query.edit_message_caption(
        caption="You approved the cleaning photo. Thanks!",
        parse_mode=ParseMode.MARKDOWN
    )

    # Post result in group chat (with approved meme)
    group_msg = f"**{approver_name}** approved the cleaning by **{c1_name}** & **{c2_name}**. Done for the week!"
    approved_meme = config.get_meme_file("approved")
    try:
        if approved_meme:
            with open(approved_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=dorm_id,
                    message_thread_id=topic_id,
                    photo=photo,
                    caption=group_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                text=group_msg,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error("Failed to send approval to group: %s", e)

    # Notify cleaners in private DM
    dm_text = f"**{approver_name}** approved your cleaning. Done for the week!"
    await _notify_cleaner(context, week_record["cleaner1_id"], c1_name, dm_text, photo_path=approved_meme, dorm_id=dorm_id, topic_id=topic_id)
    await _notify_cleaner(context, week_record["cleaner2_id"], c2_name, dm_text, photo_path=approved_meme, dorm_id=dorm_id, topic_id=topic_id)

async def reject_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver rejects the cleaning photo."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    dorm_id, week_start = _parse_callback_dorm_and_week(query.data)
    dorm = await db.get_dorm(dorm_id)
    topic_id = dorm.get("topic_id") if dorm else None

    week_record = await db.get_week(week_start, dorm_id=dorm_id)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    approver_id = week_record["approver_id"]
    approver = await db.get_dorm_member(dorm_id, approver_id)
    approver_name = approver["name"] if approver else f"ID {approver_id}"

    if user.id != approver_id:
        await query.answer(
            f"Only the assigned approver ({approver_name}) can reject this.",
            show_alert=True
        )
        return

    # Update database: mark rejected
    await db.update_photo_status(week_start, status="rejected", dorm_id=dorm_id)

    c1 = await db.get_dorm_member(dorm_id, week_record["cleaner1_id"])
    c2 = await db.get_dorm_member(dorm_id, week_record["cleaner2_id"])
    c1_name = c1["name"] if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2["name"] if c2 else f"ID {week_record['cleaner2_id']}"

    # Update approver's message
    await query.edit_message_caption(
        caption="You rejected the cleaning photo. Cleaners have been notified.",
        parse_mode=ParseMode.MARKDOWN
    )

    # Post in group chat (with rejected meme)
    group_msg = (
        f"**{approver_name}** rejected the cleaning photo.\n"
        f"**{c1_name}** & **{c2_name}** need to clean again and send a new photo before Friday 8:00 PM."
    )
    rejected_meme = config.get_meme_file("rejected")
    try:
        if rejected_meme:
            with open(rejected_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=dorm_id,
                    message_thread_id=topic_id,
                    photo=photo,
                    caption=group_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                text=group_msg,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error("Failed to send rejection to group: %s", e)

    # Notify cleaners in private DM
    dm_text = (
        f"**{approver_name}** rejected your cleaning photo.\n"
        "Please clean again and send a new photo before Friday 8:00 PM."
    )
    await _notify_cleaner(context, week_record["cleaner1_id"], c1_name, dm_text, photo_path=rejected_meme, dorm_id=dorm_id, topic_id=topic_id)
    await _notify_cleaner(context, week_record["cleaner2_id"], c2_name, dm_text, photo_path=rejected_meme, dorm_id=dorm_id, topic_id=topic_id)

# ---------------------------------------------------------------------------
# Friday Final Confirmation Callbacks: Confirm Cleaned / Confirm Not Cleaned
# ---------------------------------------------------------------------------
async def confirm_cleaned_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver confirms the final result: Cleaned."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    dorm_id, week_start = _parse_callback_dorm_and_week(query.data)
    dorm = await db.get_dorm(dorm_id)
    topic_id = dorm.get("topic_id") if dorm else None

    week_record = await db.get_week(week_start, dorm_id=dorm_id)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    approver_id = week_record["approver_id"]
    approver = await db.get_dorm_member(dorm_id, approver_id)
    approver_name = approver["name"] if approver else f"ID {approver_id}"

    if user.id != approver_id:
        await query.answer(
            f"Only the assigned approver ({approver_name}) can confirm this.",
            show_alert=True
        )
        return

    if week_record.get("final_status") in ("completed", "fined"):
        await query.answer("Status has already been decided.", show_alert=True)
        return

    # Record approver decision
    await db.record_approver_decision(week_start, decision="cleaned", final_status="completed", dorm_id=dorm_id)

    c1 = await db.get_dorm_member(dorm_id, week_record["cleaner1_id"])
    c2 = await db.get_dorm_member(dorm_id, week_record["cleaner2_id"])
    c1_name = c1["name"] if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2["name"] if c2 else f"ID {week_record['cleaner2_id']}"

    # Edit approver message
    await query.edit_message_text("You confirmed the dorm was cleaned. No fines.")

    # Post in group chat (with approved meme)
    group_msg = f"**{approver_name}** confirmed that **{c1_name}** & **{c2_name}** cleaned the dorm. No fines!"
    approved_meme = config.get_meme_file("approved")
    try:
        if approved_meme:
            with open(approved_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=dorm_id,
                    message_thread_id=topic_id,
                    photo=photo,
                    caption=group_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                text=group_msg,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error("Failed to send confirm cleaned to group: %s", e)

    # Notify cleaners
    dm_text = f"**{approver_name}** confirmed you cleaned the dorm. No fines!"
    await _notify_cleaner(context, week_record["cleaner1_id"], c1_name, dm_text, photo_path=approved_meme, dorm_id=dorm_id, topic_id=topic_id)
    await _notify_cleaner(context, week_record["cleaner2_id"], c2_name, dm_text, photo_path=approved_meme, dorm_id=dorm_id, topic_id=topic_id)

async def confirm_not_cleaned_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver confirms the final result: Not Cleaned (Fines recorded)."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    dorm_id, week_start = _parse_callback_dorm_and_week(query.data)
    dorm = await db.get_dorm(dorm_id)
    topic_id = dorm.get("topic_id") if dorm else None
    fine_total = dorm.get("fine_amount", 100) if dorm else getattr(config, "FINE_TOTAL", 100)
    fine_each = fine_total // 2

    week_record = await db.get_week(week_start, dorm_id=dorm_id)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    approver_id = week_record["approver_id"]
    approver = await db.get_dorm_member(dorm_id, approver_id)
    approver_name = approver["name"] if approver else f"ID {approver_id}"

    if user.id != approver_id:
        await query.answer(
            f"Only the assigned approver ({approver_name}) can confirm this.",
            show_alert=True
        )
        return

    if week_record.get("final_status") in ("completed", "fined"):
        await query.answer("Status has already been decided.", show_alert=True)
        return

    # Record approver decision
    await db.record_approver_decision(week_start, decision="not_cleaned", final_status="fined", dorm_id=dorm_id)

    c1 = await db.get_dorm_member(dorm_id, week_record["cleaner1_id"])
    c2 = await db.get_dorm_member(dorm_id, week_record["cleaner2_id"])
    c1_name = c1["name"] if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2["name"] if c2 else f"ID {week_record['cleaner2_id']}"

    # Create fines in ledger
    await db.create_fines_for_week(
        week_start=week_start,
        cleaner1_id=week_record["cleaner1_id"],
        cleaner1_name=c1_name,
        cleaner2_id=week_record["cleaner2_id"],
        cleaner2_name=c2_name,
        fine_each=fine_each,
        dorm_id=dorm_id
    )

    # Edit approver message
    await query.edit_message_text(
        f"You confirmed the dorm was NOT cleaned. Fine: {fine_total} ETB total."
    )

    # Post in group chat (with fined meme)
    group_msg = (
        f"**{approver_name}** confirmed the dorm was NOT cleaned.\n\n"
        f"Fine: {fine_each} ETB each for **{c1_name}** & **{c2_name}** ({fine_total} ETB total)."
    )
    fined_meme = config.get_meme_file("fined")
    try:
        if fined_meme:
            with open(fined_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=dorm_id,
                    message_thread_id=topic_id,
                    photo=photo,
                    caption=group_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                text=group_msg,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error("Failed to send fine notice to group: %s", e)

    # Notify cleaners
    dm_text = (
        f"Approver **{approver_name}** confirmed the dorm was NOT cleaned.\n"
        f"Fine: **{fine_each} ETB** recorded for you."
    )
    await _notify_cleaner(context, week_record["cleaner1_id"], c1_name, dm_text, photo_path=fined_meme, dorm_id=dorm_id, topic_id=topic_id)
    await _notify_cleaner(context, week_record["cleaner2_id"], c2_name, dm_text, photo_path=fined_meme, dorm_id=dorm_id, topic_id=topic_id)

def register_approval_handlers(application: Application) -> None:
    """Attach approver decision handlers."""
    application.add_handler(CallbackQueryHandler(approve_photo_callback, pattern=r"^approve_"))
    application.add_handler(CallbackQueryHandler(reject_photo_callback, pattern=r"^reject_"))
    application.add_handler(CallbackQueryHandler(confirm_cleaned_callback, pattern=r"^fconfirm_clean_"))
    application.add_handler(CallbackQueryHandler(confirm_not_cleaned_callback, pattern=r"^fconfirm_notclean_"))
