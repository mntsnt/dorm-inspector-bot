"""
Cleaning Duty & Photo Submission Handler (Multi-Tenant Platform)
Handles /done, 'We cleaned' button callbacks, photo submissions, and Friday check responses.
Scoper to specific dorms with automatic resolution for private chat interactions.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
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

async def _safe_edit_message(
    query,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = ParseMode.MARKDOWN
) -> None:
    """Safely edit query message handling photo captions and suppressing Message is not modified."""
    if not query or not query.message:
        return
    try:
        if query.message.photo:
            await query.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            logger.warning("BadRequest editing message: %s", e)
    except Exception as e:
        logger.warning("Error editing message: %s", e)

async def _is_member(user_id: int) -> bool:
    """Return True if user is a member in config or any active dorm."""
    if config.is_member(user_id):
        return True
    user_dorms = await db.get_user_dorms(user_id)
    return len(user_dorms) > 0

async def _resolve_user_active_dorm(user_id: int) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """
    Resolve which dorm a user is interacting with for cleaning duty:
    Returns (dorm_dict, active_week_record) or None.
    """
    user_dorms = await db.get_user_dorms(user_id)
    if not user_dorms:
        if config.is_member(user_id) and getattr(config, "GROUP_ID", 0):
            await db.seed_default_dorm()
            default_dorm = await db.get_dorm(config.GROUP_ID)
            if default_dorm:
                return default_dorm, None
        return None

    if len(user_dorms) == 1:
        dorm = user_dorms[0]
        return dorm, None

    # If user belongs to multiple dorms, prioritize the one where they are on duty this week
    for dorm in user_dorms:
        d_id = dorm["dorm_id"]
        # Check current history or active week
        history = await db.get_history(limit=1, dorm_id=d_id)
        if history and history[0].get("final_status") == "pending":
            rec = history[0]
            if user_id in (rec.get("cleaner1_id"), rec.get("cleaner2_id")):
                return dorm, rec

    return user_dorms[0], None

def _parse_callback_dorm_and_week(callback_data: str) -> Tuple[int, str]:
    """Parse callback data which may be 'prefix_{dorm_id}_{week_start}' or 'prefix_{week_start}'."""
    parts = callback_data.split("_")
    # Example: ['cleaned', '-1002378440774', '2026-11-23'] -> 3 parts
    # Example: ['fcheck', 'yes', '-1002378440774', '2026-11-23'] -> 4 parts
    if len(parts) >= 4 and parts[0] in ("fcheck", "fconfirm"):
        if len(parts) >= 4 and "-" in parts[3]:
            try:
                return int(parts[2]), parts[3]
            except ValueError:
                return 0, ""
        return 0, ""
    elif len(parts) == 3:
        try:
            return int(parts[1]), parts[2]
        except ValueError:
            return 0, ""
    elif len(parts) == 2:
        return getattr(config, "DEFAULT_GROUP_ID", -1002378440774), parts[1]

    return 0, ""

# ---------------------------------------------------------------------------
# /done Command & 'We cleaned' Button
# ---------------------------------------------------------------------------
async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleaner triggers done via command."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not await _is_member(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("Sorry, this bot is only for dorm members.")
        return

    if chat.type != "private":
        await update.effective_message.reply_text(
            "Please send /done in private chat with the bot."
        )
        return

    resolved = await _resolve_user_active_dorm(user.id)
    if not resolved:
        await update.effective_message.reply_text("You are not registered in any active dorm.")
        return

    dorm, _ = resolved
    await handle_cleaner_done(update, context, user.id, dorm_id=dorm["dorm_id"])

async def cleaned_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleaner clicks 'We cleaned' button."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user

    dorm_id, week_start = _parse_callback_dorm_and_week(query.data)
    if not dorm_id:
        await query.answer("Invalid dorm reference.", show_alert=True)
        return

    member = await db.get_dorm_member(dorm_id, user.id)
    if not member:
        await query.answer("You are not a member of this dorm.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    await handle_cleaner_done(update, context, user.id, dorm_id=dorm_id, week_start=week_start)

async def handle_cleaner_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    dorm_id: Optional[int] = None,
    week_start: Optional[str] = None
) -> None:
    """Common logic for initiating cleaning submission."""
    msg = update.effective_message
    if not msg:
        return

    # Check if this was triggered in a group chat — photo submissions must occur in private DM
    if msg.chat and msg.chat.type in ("group", "supergroup"):
        bot_info = await context.bot.get_me()
        bot_user = bot_info.username or "DormInspectorBot"
        dm_url = f"https://t.me/{bot_user}?start=submit"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 Open Bot in DM to Submit", url=dm_url)]
        ])
        await msg.reply_text(
            "📸 **Photo submissions are handled in private chat with the bot.**\n\n"
            f"Please message @{bot_user} in DM to submit your inspection photo!",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if dorm_id is None:
        resolved = await _resolve_user_active_dorm(user_id)
        if resolved:
            dorm_id = resolved[0]["dorm_id"]
        else:
            await msg.reply_text("You are not registered in any active dorm.")
            return

    member = await db.get_dorm_member(dorm_id, user_id)
    if not member:
        await msg.reply_text("You are not a member of this dorm.")
        return

    dorm = await db.get_dorm(dorm_id)
    if not dorm:
        await msg.reply_text("Dorm configuration not found.")
        return

    pairs = await db.get_dorm_pairs(dorm_id)
    members = await db.get_dorm_members(dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    from scheduler import get_dorm_week_info
    week_info = get_dorm_week_info(dorm, pairs, members_by_id)

    if not week_info or week_info.week_number < 1:
        await msg.reply_text(
            f"Cleaning duty has not started yet for {dorm['dorm_name']}."
        )
        return

    target_week_start = week_start or week_info.week_start
    week_record = await db.get_week(target_week_start, dorm_id=dorm_id)

    if not week_record:
        cleaner_ids = week_info.cleaner_ids
        eligible_approver_ids = [
            m["user_id"] for m in members
            if m["user_id"] not in cleaner_ids
        ]
        if not eligible_approver_ids:
            eligible_approver_ids = [week_info.cleaner1_id]

        week_record = await db.get_or_create_week(
            week_start=week_info.week_start,
            week_number=week_info.week_number,
            cleaner1_id=week_info.cleaner1_id,
            cleaner2_id=week_info.cleaner2_id,
            eligible_approver_ids=eligible_approver_ids,
            dorm_id=dorm_id,
            cleaner3_id=week_info.cleaner3_id,
        )

    # Validate that user is one of the assigned cleaners
    c1_id = week_record["cleaner1_id"]
    c2_id = week_record.get("cleaner2_id", 0)
    c3_id = week_record.get("cleaner3_id", 0)
    assigned_cleaner_ids = [cid for cid in (c1_id, c2_id, c3_id) if cid]

    if user_id not in assigned_cleaner_ids:
        disp = week_info.cleaner_display
        await msg.reply_text(
            f"ℹ️ **You are not on cleaning duty this week!**\n\n"
            f"Cleaners for this week are **{disp}**.\n"
            "Only the assigned cleaners can submit the cleaning photo.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Check if already completed or approved
    if week_record.get("final_status") == "completed" or week_record.get("photo_status") == "approved":
        await msg.reply_text("✅ Cleaning for this week is already completed and approved! You are done for the week.")
        return

    await db.mark_done_reported(target_week_start, dorm_id=dorm_id)

    instruction = "Please send a photo of the cleaned dorm in this chat."
    dog_meme = config.get_meme_file("suspicious_dog")
    if dog_meme:
        try:
            with open(dog_meme, "rb") as photo:
                await msg.reply_photo(
                    photo=photo,
                    caption="Are you sure? 👀\nSend a photo of the cleaned dorm to prove it.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
        except Exception as e:
            logger.warning("Could not send suspicious dog meme: %s", e)

    await msg.reply_text(instruction, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Photo Message Handler
# ---------------------------------------------------------------------------
async def photo_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos submitted by cleaners in private chat or in the dorm group/topic."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not update.message or not update.message.photo:
        return

    is_group = bool(chat and chat.type in ("group", "supergroup"))

    if is_group:
        dorm_id = chat.id
        dorm = await db.get_dorm(dorm_id)
        if not dorm:
            default_gid = getattr(config, "GROUP_ID", -1002378440774)
            if dorm_id == default_gid:
                dorm = await db.get_dorm(default_gid)
        if not dorm:
            # Not a recognized dorm group; ignore silently so normal chatting isn't affected
            return
    else:
        if not await _is_member(user.id):
            await update.message.reply_text("Sorry, this bot is only for dorm members.")
            return

        resolved = await _resolve_user_active_dorm(user.id)
        if not resolved:
            await update.message.reply_text("You are not registered in any active dorm.")
            return

        dorm, _ = resolved
        dorm_id = dorm["dorm_id"]

    topic_id = dorm.get("topic_id")

    pairs = await db.get_dorm_pairs(dorm_id)
    members = await db.get_dorm_members(dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    from scheduler import get_dorm_week_info
    week_info = get_dorm_week_info(dorm, pairs, members_by_id)

    if not week_info or week_info.week_number < 1:
        if not is_group:
            await update.message.reply_text("Cleaning duty has not started yet.")
        return

    caption = (update.message.caption or "").lower()
    c1_id = week_info.cleaner1_id
    c2_id = week_info.cleaner2_id
    c3_id = getattr(week_info, "cleaner3_id", 0)
    cleaner_uids = [cid for cid in (c1_id, c2_id, c3_id) if cid]

    is_cleaner = user.id in cleaner_uids
    mentions_clean = any(w in caption for w in ("clean", "done", "duty", "proof", "pic", "photo", "inspection", "room", "floor"))

    if is_group and not is_cleaner and not mentions_clean:
        # In a group, don't hijack unrelated photos from non-cleaners
        return

    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)
    if not week_record:
        eligible_approver_ids = [
            m["user_id"] for m in members
            if m["user_id"] not in week_info.cleaner_ids
        ]
        if not eligible_approver_ids:
            eligible_approver_ids = [week_info.cleaner1_id]

        week_record = await db.get_or_create_week(
            week_start=week_info.week_start,
            week_number=week_info.week_number,
            cleaner1_id=week_info.cleaner1_id,
            cleaner2_id=week_info.cleaner2_id,
            eligible_approver_ids=eligible_approver_ids,
            dorm_id=dorm_id,
            cleaner3_id=week_info.cleaner3_id,
        )

    if not is_cleaner and not is_group:
        disp = week_info.cleaner_display
        await update.message.reply_text(
            f"ℹ️ **You are not on cleaning duty this week!**\n\n"
            f"Cleaners for this week are **{disp}**.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if week_record.get("final_status") == "completed" or week_record.get("photo_status") == "approved":
        if not is_group:
            await update.message.reply_text("✅ Cleaning for this week is already completed and approved! You are done for the week.")
        return

    # Get the highest resolution photo
    photo_file_id = update.message.photo[-1].file_id

    # Record photo in database
    await db.submit_photo(week_info.week_start, photo_file_id, user.id, dorm_id=dorm_id)

    submitter_name = members_by_id.get(user.id, user.first_name)
    c1_name = members_by_id.get(c1_id, f"ID {c1_id}")
    c2_name = members_by_id.get(c2_id, f"ID {c2_id}") if c2_id else ""
    c_disp = f"{c1_name} & {c2_name}" if c2_name else c1_name

    approver_id = week_record.get("approver_id")
    approver_name = members_by_id.get(approver_id, "Approver")

    approver_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐ Approve", callback_data=f"approve_{dorm_id}_{week_info.week_start}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{dorm_id}_{week_info.week_start}"),
        ]
    ])

    if is_group:
        # If posted directly in the group, reply with approval buttons for Approver/Admin right in the group!
        group_caption = (
            f"📸 **Cleaning Photo Submitted by {submitter_name}!**\n\n"
            f"Cleaners: **{c_disp}**\n"
            f"Waiting for **{approver_name}** (or Admin) to review:"
        )
        try:
            await update.message.reply_text(
                group_caption,
                reply_markup=approver_keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error("Failed to post submission acknowledge in group: %s", e)
    else:
        # Forward/send photo to the dorm group chat
        group_caption = (
            f"Cleaned by **{c_disp}** (sent by {submitter_name}).\n"
            f"Waiting for **{approver_name}** to approve."
        )
        try:
            await context.bot.send_photo(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                photo=photo_file_id,
                caption=group_caption,
                reply_markup=approver_keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error("Failed to send photo to group %s: %s", dorm_id, e)

    # Send photo with Approve / Reject buttons in private DM to the Approver
    if approver_id:
        approver_caption = (
            f"Photo sent by **{c_disp}** ({dorm['dorm_name']}).\n"
            "Please approve or reject:"
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
            if not is_group:
                from scheduler import send_dm_with_fallback
                await send_dm_with_fallback(
                    context, approver_id, approver_name,
                    f"Photo sent by **{c_disp}**. Please check and reply.",
                    reply_markup=approver_keyboard,
                    dorm_id=dorm_id,
                    topic_id=topic_id
                )

    # Confirm to cleaner in DM if triggered in DM
    if not is_group:
        reply_text = f"Photo received! Sent to the group and to **{approver_name}** to review."
        await update.message.reply_text(reply_text, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Friday Check Callbacks (Cleaners answering Yes / No)
# ---------------------------------------------------------------------------
async def friday_check_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleaner answered YES to Friday check."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user

    dorm_id, week_start = _parse_callback_dorm_and_week(query.data)
    if not dorm_id:
        await query.answer("Invalid dorm reference.", show_alert=True)
        return

    member = await db.get_dorm_member(dorm_id, user.id)
    if not member:
        await query.answer("You are not a member of this dorm.", show_alert=True)
        return

    dorm = await db.get_dorm(dorm_id)
    topic_id = dorm.get("topic_id") if dorm else None

    week_record = await db.get_week(week_start, dorm_id=dorm_id)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    assigned_ids = [cid for cid in (week_record["cleaner1_id"], week_record.get("cleaner2_id", 0), week_record.get("cleaner3_id", 0)) if cid]
    if user.id not in assigned_ids:
        await query.answer("Only assigned cleaners can answer this check.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    member_name = member["name"] if member else user.first_name

    await db.record_cleaner_response(week_start, user.id, "yes", dorm_id=dorm_id)

    # Re-fetch week record
    week_record = await db.get_week(week_start, dorm_id=dorm_id)
    photo_status = week_record.get("photo_status", "none")

    if photo_status == "approved":
        await _safe_edit_message(
            query,
            "You confirmed you cleaned, and your photo was approved. All done!"
        )
        try:
            await context.bot.send_message(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                text=f"Friday Check: **{member_name}** confirmed they cleaned (photo already approved).",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.warning("Could not send group message for Friday check: %s", e)
    else:
        await _safe_edit_message(
            query,
            "You answered YES, but no approved photo on record yet. Please send the photo now!"
        )
        try:
            await context.bot.send_message(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                text=f"Friday Check: **{member_name}** answered YES, but no photo approved yet.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.warning("Could not send group message for Friday check: %s", e)

async def friday_check_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cleaner answered NO to Friday check."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user

    dorm_id, week_start = _parse_callback_dorm_and_week(query.data)
    if not dorm_id:
        await query.answer("Invalid dorm reference.", show_alert=True)
        return

    member = await db.get_dorm_member(dorm_id, user.id)
    if not member:
        await query.answer("You are not a member of this dorm.", show_alert=True)
        return

    dorm = await db.get_dorm(dorm_id)
    topic_id = dorm.get("topic_id") if dorm else None

    week_record = await db.get_week(week_start, dorm_id=dorm_id)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    assigned_ids = [cid for cid in (week_record["cleaner1_id"], week_record.get("cleaner2_id", 0), week_record.get("cleaner3_id", 0)) if cid]
    if user.id not in assigned_ids:
        await query.answer("Only assigned cleaners can answer this check.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    member_name = member["name"] if member else user.first_name

    await db.record_cleaner_response(week_start, user.id, "no", dorm_id=dorm_id)

    await _safe_edit_message(query, "Recorded. The approver will make the final decision.")
    try:
        await context.bot.send_message(
            chat_id=dorm_id,
            message_thread_id=topic_id,
            text=f"Friday Check: **{member_name}** answered that they did NOT clean.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning("Could not send group message for Friday check: %s", e)

def register_cleaning_handlers(application: Application) -> None:
    """Attach cleaning duty and photo submission handlers."""
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("submit", done_command))
    application.add_handler(CommandHandler("clean", done_command))
    application.add_handler(CallbackQueryHandler(cleaned_button_callback, pattern=r"^cleaned_"))
    application.add_handler(CallbackQueryHandler(friday_check_yes_callback, pattern=r"^fcheck_yes_"))
    application.add_handler(CallbackQueryHandler(friday_check_no_callback, pattern=r"^fcheck_no_"))
    application.add_handler(MessageHandler(filters.PHOTO, photo_message_handler))
