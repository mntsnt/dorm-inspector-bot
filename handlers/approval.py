"""
Approver Actions Handler (Multi-Tenant Platform)
Handles photo approval/rejection and Friday final confirmation decisions.
The approver has the final authority; fines are only created upon their decision or timeout fallback.
Properly scoped per dorm with group topic and DM notifications.
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

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

async def _safe_edit_message(
    query,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = ParseMode.MARKDOWN
) -> None:
    """Safely edit query message regardless of whether it contains photo or text, ignoring unmodified errors."""
    if not query.message:
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

# ---------------------------------------------------------------------------
# Photo Review Callbacks: Approve / Reject
# ---------------------------------------------------------------------------
async def approve_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver approves the cleaning photo."""
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

    approver_id = week_record["approver_id"]
    approver = await db.get_dorm_member(dorm_id, approver_id)
    approver_name = approver["name"] if approver else f"ID {approver_id}"

    is_admin = bool(dorm and user.id == dorm.get("admin_id")) or (user.id == getattr(config, "ADMIN_ID", 0))
    if user.id != approver_id and not is_admin:
        await query.answer(
            f"Only the assigned approver ({approver_name}) or Admin can approve this.",
            show_alert=True
        )
        return

    if week_record.get("final_status") == "completed":
        await query.answer("Cleaning is already completed.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    c1 = await db.get_dorm_member(dorm_id, week_record["cleaner1_id"])
    c2 = await db.get_dorm_member(dorm_id, week_record["cleaner2_id"])
    c1_name = c1["name"] if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2["name"] if c2 else f"ID {week_record['cleaner2_id']}"

    # Present 5-Star Inspection Review rating keyboard
    star_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⭐⭐⭐⭐⭐ (5★)", callback_data=f"rate_5_{dorm_id}_{week_start}"),
            InlineKeyboardButton("⭐⭐⭐⭐ (4★)", callback_data=f"rate_4_{dorm_id}_{week_start}"),
        ],
        [
            InlineKeyboardButton("⭐⭐⭐ (3★)", callback_data=f"rate_3_{dorm_id}_{week_start}"),
            InlineKeyboardButton("⭐⭐ (2★)", callback_data=f"rate_2_{dorm_id}_{week_start}"),
            InlineKeyboardButton("⭐ (1★)", callback_data=f"rate_1_{dorm_id}_{week_start}"),
        ],
    ])

    prompt_text = (
        f"⭐ **Rate This Week's Cleaning ({c1_name} & {c2_name})**\n\n"
        "Choose a 1 to 5 star rating for room inspection.\n"
        "Cleaners will receive **Tsidat tokens** based on your rating! 🪙"
    )
    await _safe_edit_message(query, prompt_text, reply_markup=star_keyboard)

RATING_COMMENTS = {
    5: "Sparkling clean! 5-star hotel standard.",
    4: "Great job! Room looks and smells fresh.",
    3: "Good effort! Passed inspection cleanly.",
    2: "Acceptable, but room needs a deeper scrub next time.",
    1: "Barely passed. The broom was barely awake!"
}

RATING_STARS_DISPLAY = {
    5: "⭐⭐⭐⭐⭐ (5/5)",
    4: "⭐⭐⭐⭐☆ (4/5)",
    3: "⭐⭐⭐☆☆ (3/5)",
    2: "⭐⭐☆☆☆ (2/5)",
    1: "⭐☆☆☆☆ (1/5)"
}

async def rate_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver submits a 1-5 star rating and review for the cleaning photo."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user

    parts = query.data.split("_")
    if len(parts) >= 4:
        try:
            stars = int(parts[1])
            dorm_id = int(parts[2])
            week_start = parts[3]
        except (ValueError, IndexError):
            await query.answer("Invalid rating data.", show_alert=True)
            return
    elif len(parts) == 3:
        try:
            stars = int(parts[1])
            week_start = parts[2]
            dorm_id = await db.resolve_dorm_for_user(user.id) or getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        except (ValueError, IndexError):
            await query.answer("Invalid rating data.", show_alert=True)
            return
    else:
        await query.answer("Invalid rating reference.", show_alert=True)
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

    approver_id = week_record["approver_id"]
    approver = await db.get_dorm_member(dorm_id, approver_id)
    approver_name = approver["name"] if approver else f"ID {approver_id}"

    is_admin = bool(dorm and user.id == dorm.get("admin_id")) or (user.id == getattr(config, "ADMIN_ID", 0))
    if user.id != approver_id and not is_admin:
        await query.answer(
            f"Only the assigned approver ({approver_name}) or Admin can submit this rating.",
            show_alert=True
        )
        return

    if week_record.get("final_status") == "completed":
        await query.answer("Cleaning is already completed.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    stars_clamped = max(1, min(5, stars))
    review_comment = RATING_COMMENTS.get(stars_clamped, "Passed inspection.")
    stars_display = RATING_STARS_DISPLAY.get(stars_clamped, f"{stars_clamped} Stars")
    tokens_earned = stars_clamped * 5

    # 1. Update database: mark approved, completed, rating, review_text
    await db.update_photo_status(
        week_start,
        status="approved",
        final_status="completed",
        dorm_id=dorm_id,
        rating=stars_clamped,
        review_text=review_comment
    )

    # 2. Award Tsidat tokens: cleaners get stars*5, approver gets +5
    c1_id = week_record["cleaner1_id"]
    c2_id = week_record["cleaner2_id"]
    await db.add_tsidat_tokens(dorm_id, c1_id, tokens_earned)
    await db.add_tsidat_tokens(dorm_id, c2_id, tokens_earned)
    await db.add_tsidat_tokens(dorm_id, approver_id, 5)

    c1 = await db.get_dorm_member(dorm_id, c1_id)
    c2 = await db.get_dorm_member(dorm_id, c2_id)
    c3_id = week_record.get("cleaner3_id", 0)
    c3 = await db.get_dorm_member(dorm_id, c3_id) if c3_id else None
    c1_name = c1["name"] if c1 else f"ID {c1_id}"
    c2_name = c2["name"] if c2 else (f"ID {c2_id}" if c2_id else "")
    c3_name = c3["name"] if c3 else (f"ID {c3_id}" if c3_id else "")
    cleaner_names = [n for n in (c1_name, c2_name, c3_name) if n]
    c_disp = " & ".join(cleaner_names)

    # 3. Update approver's message in DM
    confirmation_text = (
        f"✅ **Inspection Approved**\n\n"
        f"Rating: {stars_display}\n"
        f"Cleaners received **+{tokens_earned} Tsidat tokens** (+5 for you)."
    )
    await _safe_edit_message(query, confirmation_text)

    # 4. Post photo and well-done celebration in dorm group chat
    group_msg = (
        f"🎉 **Well Done! Cleaning Approved**\n\n"
        f"⭐ **Approver:** {approver_name}\n"
        f"⭐ **Rating:** {stars_display} — _{review_comment}_\n"
        f"👏 **Cleaners:** {c_disp} — duty completed for the week!\n"
        f"🪙 **Reward:** +{tokens_earned} Tsidat tokens awarded to each cleaner."
    )
    photo_file_id = week_record.get("photo_file_id")
    approved_meme = config.get_meme_file("approved")
    try:
        if photo_file_id:
            await context.bot.send_photo(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                photo=photo_file_id,
                caption=group_msg,
                parse_mode=ParseMode.MARKDOWN
            )
        elif approved_meme:
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
        logger.error("Failed to send approved cleaning photo to group: %s", e)

    # 5. Notify cleaners in private DM (confirming they are done for the week)
    dm_text = (
        f"🎉 **Well Done! Inspection Approved**\n\n"
        f"Approver: **{approver_name}**\n"
        f"Rating: {stars_display} — _{review_comment}_\n"
        f"Earned: **+{tokens_earned} Tsidat tokens**.\n\n"
        f"✅ You have completed your cleaning duty and are done for the week!"
    )
    for cid, cname in [(c1_id, c1_name), (c2_id, c2_name), (c3_id, c3_name)]:
        if cid:
            await _notify_cleaner(context, cid, cname, dm_text, photo_path=approved_meme, dorm_id=dorm_id, topic_id=topic_id)

async def reject_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver rejects the cleaning photo."""
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

    approver_id = week_record["approver_id"]
    approver = await db.get_dorm_member(dorm_id, approver_id)
    approver_name = approver["name"] if approver else f"ID {approver_id}"

    is_admin = bool(dorm and user.id == dorm.get("admin_id")) or (user.id == getattr(config, "ADMIN_ID", 0))
    if user.id != approver_id and not is_admin:
        await query.answer(
            f"Only the assigned approver ({approver_name}) or Admin can reject this.",
            show_alert=True
        )
        return

    try:
        await query.answer()
    except Exception:
        pass

    # Update database: mark rejected
    await db.update_photo_status(week_start, status="rejected", dorm_id=dorm_id)

    c1 = await db.get_dorm_member(dorm_id, week_record["cleaner1_id"])
    c2 = await db.get_dorm_member(dorm_id, week_record["cleaner2_id"])
    c1_name = c1["name"] if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2["name"] if c2 else f"ID {week_record['cleaner2_id']}"

    # Update approver's message
    await _safe_edit_message(query, "You rejected the cleaning photo. Cleaners have been notified.")

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

    approver_id = week_record["approver_id"]
    approver = await db.get_dorm_member(dorm_id, approver_id)
    approver_name = approver["name"] if approver else f"ID {approver_id}"

    is_admin = bool(dorm and user.id == dorm.get("admin_id")) or (user.id == getattr(config, "ADMIN_ID", 0))
    if user.id != approver_id and not is_admin:
        await query.answer(
            f"Only the assigned approver ({approver_name}) or Admin can confirm this.",
            show_alert=True
        )
        return

    if week_record.get("final_status") in ("completed", "fined"):
        await query.answer("Status has already been decided.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    # Record approver decision with 5-star rating and Tsidat tokens
    await db.record_approver_decision(
        week_start,
        decision="cleaned",
        final_status="completed",
        dorm_id=dorm_id,
        rating=5,
        review_text="Cleaned and verified on Friday check. 🌟"
    )

    c1_id = week_record["cleaner1_id"]
    c2_id = week_record["cleaner2_id"]
    await db.add_tsidat_tokens(dorm_id, c1_id, 25)
    await db.add_tsidat_tokens(dorm_id, c2_id, 25)
    await db.add_tsidat_tokens(dorm_id, approver_id, 5)

    c1 = await db.get_dorm_member(dorm_id, c1_id)
    c2 = await db.get_dorm_member(dorm_id, c2_id)
    c3_id = week_record.get("cleaner3_id", 0)
    c3 = await db.get_dorm_member(dorm_id, c3_id) if c3_id else None
    c1_name = c1["name"] if c1 else f"ID {c1_id}"
    c2_name = c2["name"] if c2 else (f"ID {c2_id}" if c2_id else "")
    c3_name = c3["name"] if c3 else (f"ID {c3_id}" if c3_id else "")
    cleaner_names = [n for n in (c1_name, c2_name, c3_name) if n]
    c_disp = " & ".join(cleaner_names)

    # Edit approver message
    await _safe_edit_message(query, "You confirmed the dorm was cleaned. Both cleaners earned +25 Tsidat tokens! 🪙")

    # Post photo and confirmation in group chat
    group_msg = (
        f"🎉 **Well Done! Friday Check Approved**\n\n"
        f"**{approver_name}** verified that **{c_disp}** cleaned the dorm.\n"
        f"⭐ Rating: ⭐⭐⭐⭐⭐ (5/5 Stars)\n"
        f"🎉 Reward: **+25 Tsidat Tokens** awarded to each cleaner! 🪙 No fines. Duty done for the week!"
    )
    photo_file_id = week_record.get("photo_file_id")
    approved_meme = config.get_meme_file("approved")
    try:
        if photo_file_id:
            await context.bot.send_photo(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                photo=photo_file_id,
                caption=group_msg,
                parse_mode=ParseMode.MARKDOWN
            )
        elif approved_meme:
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
    dm_text = (
        f"🎉 **Well Done! Cleaning Confirmed**\n\n"
        f"**{approver_name}** confirmed you cleaned the dorm. No fines!\n"
        f"✅ You have completed your cleaning duty and are done for the week."
    )
    for cid, cname in [(c1_id, c1_name), (c2_id, c2_name), (c3_id, c3_name)]:
        if cid:
            await _notify_cleaner(context, cid, cname, dm_text, photo_path=approved_meme, dorm_id=dorm_id, topic_id=topic_id)

async def confirm_not_cleaned_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver confirms the final result: Not Cleaned (Fines recorded)."""
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
    fine_total = dorm.get("fine_amount", 100) if dorm else getattr(config, "FINE_TOTAL", 100)
    fine_each = fine_total // 2

    week_record = await db.get_week(week_start, dorm_id=dorm_id)
    if not week_record:
        await query.answer("Week record not found.", show_alert=True)
        return

    approver_id = week_record["approver_id"]
    approver = await db.get_dorm_member(dorm_id, approver_id)
    approver_name = approver["name"] if approver else f"ID {approver_id}"

    is_admin = bool(dorm and user.id == dorm.get("admin_id")) or (user.id == getattr(config, "ADMIN_ID", 0))
    if user.id != approver_id and not is_admin:
        await query.answer(
            f"Only the assigned approver ({approver_name}) or Admin can confirm this.",
            show_alert=True
        )
        return

    if week_record.get("final_status") in ("completed", "fined"):
        await query.answer("Status has already been decided.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

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
    await _safe_edit_message(
        query,
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

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approver or Admin executes /approve [stars] to immediately approve cleaning duty and award Tsidat tokens."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    # Determine dorm_id
    dorm_id = None
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        dorm_id = update.effective_chat.id
    if not dorm_id:
        dorm_id = await db.resolve_dorm_for_user(user.id) or getattr(config, "GROUP_ID", -1002378440774)

    dorm = await db.get_dorm(dorm_id)
    if not dorm:
        await msg.reply_text("Dorm configuration not found.")
        return

    admin_id = dorm.get("admin_id") or getattr(config, "ADMIN_ID", 6163528558)
    topic_id = dorm.get("topic_id")

    pairs = await db.get_dorm_pairs(dorm_id)
    members = await db.get_dorm_members(dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    from scheduler import get_dorm_week_info
    week_info = get_dorm_week_info(dorm, pairs, members_by_id)
    if not week_info:
        await msg.reply_text("Cleaning rotation schedule not found.")
        return

    week_start = week_info.week_start
    week_record = await db.get_week(week_start, dorm_id=dorm_id)
    if not week_record:
        eligible_approver_ids = [m["user_id"] for m in members if m["user_id"] not in week_info.cleaner_ids]
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

    approver_id = week_record["approver_id"]
    is_approver = (user.id == approver_id)
    is_admin = (user.id == admin_id or user.id == getattr(config, "ADMIN_ID", 0))

    if not is_approver and not is_admin:
        approver_name = members_by_id.get(approver_id, f"ID {approver_id}")
        await msg.reply_text(f"⛔ Only the assigned approver ({approver_name}) or Dorm Admin can approve cleaning.")
        return

    # Parse optional stars: /approve 5, /approve 4
    stars = 5
    if context.args and len(context.args) > 0:
        try:
            stars = int(context.args[0])
            stars = max(1, min(5, stars))
        except ValueError:
            stars = 5

    review_comment = RATING_COMMENTS.get(stars, "Sparkling clean! 5-star hotel standard.")
    stars_display = RATING_STARS_DISPLAY.get(stars, f"{stars} Stars")
    tokens_earned = stars * 5

    # 1. Update database: mark approved, completed, rating, review_text
    await db.update_photo_status(
        week_start,
        status="approved",
        final_status="completed",
        dorm_id=dorm_id,
        rating=stars,
        review_text=review_comment
    )

    # 2. Award Tsidat tokens: cleaners get stars*5, approver gets +5
    c1_id = week_record["cleaner1_id"]
    c2_id = week_record.get("cleaner2_id", 0)
    c3_id = week_record.get("cleaner3_id", 0)
    await db.add_tsidat_tokens(dorm_id, c1_id, tokens_earned)
    if c2_id:
        await db.add_tsidat_tokens(dorm_id, c2_id, tokens_earned)
    if c3_id:
        await db.add_tsidat_tokens(dorm_id, c3_id, tokens_earned)
    await db.add_tsidat_tokens(dorm_id, approver_id, 5)

    c1_name = members_by_id.get(c1_id, f"ID {c1_id}")
    c2_name = members_by_id.get(c2_id, f"ID {c2_id}") if c2_id else ""
    c3_name = members_by_id.get(c3_id, f"ID {c3_id}") if c3_id else ""
    cleaner_names = [n for n in (c1_name, c2_name, c3_name) if n]
    c_disp = " & ".join(cleaner_names)
    actor_name = members_by_id.get(user.id, user.first_name)

    # 3. Post celebration in dorm group chat
    group_msg = (
        f"🎉 **Well Done! Cleaning Approved**\n\n"
        f"⭐ **Approved by:** {actor_name} ({'Approver' if is_approver else 'Admin'})\n"
        f"⭐ **Rating:** {stars_display} — _{review_comment}_\n"
        f"👏 **Cleaners:** {c_disp} — duty completed for the week!\n"
        f"🪙 **Reward:** +{tokens_earned} Tsidat tokens awarded to each cleaner."
    )
    photo_file_id = week_record.get("photo_file_id")
    approved_meme = config.get_meme_file("approved")
    try:
        if photo_file_id:
            await context.bot.send_photo(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                photo=photo_file_id,
                caption=group_msg,
                parse_mode=ParseMode.MARKDOWN
            )
        elif approved_meme and approved_meme.is_file():
            with open(approved_meme, "rb") as pf:
                await context.bot.send_photo(
                    chat_id=dorm_id,
                    message_thread_id=topic_id,
                    photo=pf,
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
    except Exception as ge:
        logger.warning("Could not send group celebration message: %s", ge)

    # 4. Notify cleaners in DM
    done_dm_msg = (
        f"🎉 **Duty Completed & Approved!**\n\n"
        f"Your cleaning for Week {week_info.week_number} was approved with **{stars_display}**!\n"
        f"Earned: **+{tokens_earned} Tsidat tokens**.\n\n"
        f"You are completely done for the week. Enjoy your weekend! 🌟"
    )
    await _notify_cleaner(context, c1_id, c1_name, done_dm_msg, dorm_id=dorm_id, topic_id=topic_id)
    if c2_id:
        await _notify_cleaner(context, c2_id, c2_name, done_dm_msg, dorm_id=dorm_id, topic_id=topic_id)
    if c3_id:
        await _notify_cleaner(context, c3_id, c3_name, done_dm_msg, dorm_id=dorm_id, topic_id=topic_id)

    # Reply to sender
    target_chat = update.effective_chat
    if target_chat and target_chat.id != dorm_id:
        await msg.reply_text(f"✅ Cleaning approved! Awarded +{tokens_earned} Tsidat tokens to {c_disp}.")
    else:
        await msg.reply_text(f"✅ Cleaning approved for {c_disp}!")

def register_approval_handlers(application: Application) -> None:
    """Attach approver decision handlers."""
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("approve_clean", approve_command))
    application.add_handler(CallbackQueryHandler(approve_photo_callback, pattern=r"^approve_"))
    application.add_handler(CallbackQueryHandler(rate_photo_callback, pattern=r"^rate_\d+_"))
    application.add_handler(CallbackQueryHandler(reject_photo_callback, pattern=r"^reject_"))
    application.add_handler(CallbackQueryHandler(confirm_cleaned_callback, pattern=r"^fconfirm_clean_"))
    application.add_handler(CallbackQueryHandler(confirm_not_cleaned_callback, pattern=r"^fconfirm_notclean_"))
