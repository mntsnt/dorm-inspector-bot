"""
Cleaning Duty & Photo Submission Handler
Handles /done, 'We cleaned' button callbacks, photo submissions, and Friday check responses.
"""

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
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

async def _send_dm_fallback_notice(context: ContextTypes.DEFAULT_TYPE, member_name: str) -> None:
    """Send fallback notice to the dorm group chat if a private DM fails."""
    try:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username or "the bot"
        msg = (
            f"⚠️ Could not send a private message to **{member_name}**.\n"
            f"Please open a private chat with @{bot_username} and press /start!"
        )
        await context.bot.send_message(
            chat_id=config.GROUP_ID,
            text=msg,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error("Failed to post fallback notice in group chat: %s", e)

# ---------------------------------------------------------------------------
# /done Command & 'We cleaned' Button
# ---------------------------------------------------------------------------
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleaner triggers done via command."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not config.is_member(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("🚫 Sorry, this bot is exclusively for residents of the dorm.")
        return

    if chat.type != "private":
        await update.effective_message.reply_text(
            "Please send /done in a private chat with the bot."
        )
        return

    await handle_cleaner_done(update, context, user.id)

async def cleaned_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleaner clicks 'We cleaned' button."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not config.is_member(user.id):
        await query.answer("Unauthorized.", show_alert=True)
        return

    parts = query.data.split("_", 1)
    week_start = parts[1] if len(parts) > 1 else ""

    await handle_cleaner_done(update, context, user.id, week_start=week_start)

async def handle_cleaner_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    week_start: Optional[str] = None
) -> None:
    """Common logic for initiating cleaning submission."""
    msg = update.effective_message
    week_info = config.get_week_info()

    target_week_start = week_start or week_info.week_start
    week_record = await db.get_week(target_week_start)

    # Validate that user is one of the 2 cleaners
    c1_id = week_record["cleaner1_id"] if week_record else week_info.cleaner1.user_id
    c2_id = week_record["cleaner2_id"] if week_record else week_info.cleaner2.user_id

    if user_id not in (c1_id, c2_id):
        await msg.reply_text("🚫 You are not on cleaning duty for this week.")
        return

    # Check if already completed
    if week_record and week_record.get("final_status") == "completed":
        await msg.reply_text("🎉 The cleaning for this week has already been approved and completed!")
        return

    await db.mark_done_reported(target_week_start)

    instruction = (
        "🧼 **Awesome! Now please send a photo of the cleaned dorm.**\n\n"
        "Just send the photo directly in this chat. I will forward it to the approver and the group chat for verification."
    )
    await msg.reply_text(instruction, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Photo Message Handler
# ---------------------------------------------------------------------------
async def photo_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos submitted by cleaners in private chat."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not update.message or not update.message.photo:
        return

    if not config.is_member(user.id):
        await update.message.reply_text("🚫 Sorry, this bot is exclusively for residents of the dorm.")
        return

    week_info = config.get_week_info()
    week_record = await db.get_week(week_info.week_start)

    # Check cleaners
    c1_id = week_record["cleaner1_id"] if week_record else week_info.cleaner1.user_id
    c2_id = week_record["cleaner2_id"] if week_record else week_info.cleaner2.user_id

    if user.id not in (c1_id, c2_id):
        await update.message.reply_text(
            "ℹ️ You are not on cleaning duty this week, so your photo submission was not processed."
        )
        return

    if week_record and week_record.get("final_status") == "completed":
        await update.message.reply_text(
            "🎉 Cleaning duty for this week is already marked as completed!"
        )
        return

    # Get the highest resolution photo
    photo_file_id = update.message.photo[-1].file_id

    # Record photo in database
    await db.submit_photo(week_info.week_start, photo_file_id, user.id)

    submitter_member = config.get_member(user.id)
    submitter_name = submitter_member.name if submitter_member else user.first_name

    c1 = config.get_member(c1_id)
    c2 = config.get_member(c2_id)
    c1_name = c1.name if c1 else f"ID {c1_id}"
    c2_name = c2.name if c2 else f"ID {c2_id}"

    approver_id = week_record.get("approver_id") if week_record else None
    approver = config.get_member(approver_id) if approver_id else None
    approver_name = approver.name if approver else "Approver"

    # Forward/send photo to the dorm group chat
    group_caption = (
        f"📸 **Dorm cleaned by {c1_name} & {c2_name}!**\n"
        f"Submitted by: {submitter_name}\n"
        f"Waiting for approval from **{approver_name}**."
    )
    try:
        await context.bot.send_photo(
            chat_id=config.GROUP_ID,
            photo=photo_file_id,
            caption=group_caption,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error("Failed to send photo to group %s: %s", config.GROUP_ID, e)

    # Send photo with Approve / Reject buttons in private DM to the Approver
    if approver_id:
        approver_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{week_info.week_start}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{week_info.week_start}"),
            ]
        ])
        approver_caption = (
            f"🧹 **Cleaning Submission for Review**\n\n"
            f"Cleaners: **{c1_name}** & **{c2_name}**\n"
            f"Week starting: {week_info.week_start}\n\n"
            "Please review the photo and choose an action below:"
        )
        try:
            await context.bot.send_photo(
                chat_id=approver_id,
                photo=photo_file_id,
                caption=approver_caption,
                reply_markup=approver_keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as te:
            logger.warning("Could not send review photo to approver %s: %s", approver_id, te)
            await _send_dm_fallback_notice(context, approver_name)

    # Confirm to cleaner in DM
    reply_text = (
        f"✅ Photo received!\n"
        f"It has been posted to the group and forwarded to **{approver_name}** for review.\n"
        "You will be notified as soon as they decide."
    )
    await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Friday Check Callbacks (Cleaners answering Yes / No)
# ---------------------------------------------------------------------------
async def friday_check_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleaner answered YES to Friday check."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not config.is_member(user.id):
        await query.answer("Unauthorized.", show_alert=True)
        return

    parts = query.data.split("_", 2)
    week_start = parts[2] if len(parts) > 2 else ""

    week_record = await db.get_week(week_start)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    if user.id not in (week_record["cleaner1_id"], week_record["cleaner2_id"]):
        await query.answer("Only assigned cleaners can answer this check.", show_alert=True)
        return

    member = config.get_member(user.id)
    member_name = member.name if member else user.first_name

    await db.record_cleaner_response(week_start, user.id, "yes")

    # Re-fetch week record
    week_record = await db.get_week(week_start)
    photo_status = week_record.get("photo_status", "none")

    if photo_status == "approved":
        # Yes and photo already approved
        await query.edit_message_text(
            "✅ You confirmed you cleaned, and your photo has already been approved! Duty completed."
        )
        await context.bot.send_message(
            chat_id=config.GROUP_ID,
            text=f"✅ **Friday Check:** Cleaner **{member_name}** confirmed they cleaned, and the photo is already approved! Cleaned successfully.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        # Yes but no approved photo yet -> prompt for photo & grace period
        await query.edit_message_text(
            "⚠️ You answered **YES**, but your cleaning photo has not been approved yet!\n"
            "Please send the photo of the cleaned dorm right now (short grace period)."
        )
        await context.bot.send_message(
            chat_id=config.GROUP_ID,
            text=f"⚠️ **Friday Check:** Cleaner **{member_name}** confirmed they cleaned, but no approved photo is on record yet. Cleaners must submit a photo immediately!",
            parse_mode=ParseMode.MARKDOWN
        )

async def friday_check_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleaner answered NO to Friday check."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not config.is_member(user.id):
        await query.answer("Unauthorized.", show_alert=True)
        return

    parts = query.data.split("_", 2)
    week_start = parts[2] if len(parts) > 2 else ""

    week_record = await db.get_week(week_start)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    if user.id not in (week_record["cleaner1_id"], week_record["cleaner2_id"]):
        await query.answer("Only assigned cleaners can answer this check.", show_alert=True)
        return

    member = config.get_member(user.id)
    member_name = member.name if member else user.first_name

    await db.record_cleaner_response(week_start, user.id, "no")

    await query.edit_message_text(
        "❌ You confirmed you did not clean. Your response has been recorded."
    )
    await context.bot.send_message(
        chat_id=config.GROUP_ID,
        text=f"⚠️ **Friday Check:** Cleaner **{member_name}** answered that they did **NOT** clean the dorm.",
        parse_mode=ParseMode.MARKDOWN
    )

def register_cleaning_handlers(application: Application) -> None:
    """Attach cleaning duty and photo submission handlers."""
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CallbackQueryHandler(cleaned_button_callback, pattern=r"^cleaned_"))
    application.add_handler(CallbackQueryHandler(friday_check_yes_callback, pattern=r"^fcheck_yes_"))
    application.add_handler(CallbackQueryHandler(friday_check_no_callback, pattern=r"^fcheck_no_"))
    application.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_message_handler))
