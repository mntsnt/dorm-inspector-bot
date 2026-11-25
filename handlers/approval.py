"""
Approver Actions Handler (Multi-Tenant Platform)
Handles photo approval and rejection. The approver reviews submitted photos;
unapproved weeks are closed automatically at the Friday deadline.
Properly scoped per dorm with group topic and DM notifications.
"""

import logging
from pathlib import Path
from typing import Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.helpers import escape_markdown
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
    allow_group_fallback: bool = False,
) -> None:
    """Send private DM to cleaner. Does not spam group if allow_group_fallback is False."""
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
        if not allow_group_fallback:
            return
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

async def publish_approved_cleaning(
    context: ContextTypes.DEFAULT_TYPE,
    dorm_id: int,
    week_record: dict,
    reward: dict,
    rating: int,
    review_text: str,
    approver_name: str,
    automated: bool = False,
) -> str:
    """Send the shared group celebration and cleaner confirmations after approval."""
    dorm = await db.get_dorm(dorm_id)
    topic_id = dorm.get("topic_id") if dorm else None
    member_ids = [
        week_record.get("cleaner1_id"),
        week_record.get("cleaner2_id"),
        week_record.get("cleaner3_id"),
    ]
    cleaners = []
    for cleaner_id in member_ids:
        if not cleaner_id:
            continue
        member = await db.get_dorm_member(dorm_id, cleaner_id)
        cleaners.append((
            cleaner_id,
            member["name"] if member else f"ID {cleaner_id}"
        ))

    cleaner_display = " & ".join(name for _, name in cleaners)
    cleaned_at = config.format_local_datetime(
        week_record.get("done_reported_at") or week_record.get("photo_submitted_at")
    )
    stars_display = RATING_STARS_DISPLAY.get(rating, f"{rating} Stars")
    base_tokens = rating * 5
    early_bonus = reward["early_bonus"]
    tokens_each = reward["tokens_each"]
    approver_bonus = reward.get("approver_bonus", 0)
    actor_label = f"{approver_name} (automatic fallback)" if automated else approver_name
    safe_actor_label = escape_markdown(actor_label)
    safe_cleaner_display = escape_markdown(cleaner_display)
    approver_reward_text = f" Approver earned +{approver_bonus} tokens." if approver_bonus else ""
    safe_review_text = escape_markdown(review_text)
    group_message = (
        f"🎉 **Well Done! Cleaning Approved**\n\n"
        f"⭐ **Approver:** {safe_actor_label}\n"
        f"⭐ **Rating:** {stars_display} — _{safe_review_text}_\n"
        f"🕒 **Cleaned at:** {cleaned_at or 'time not recorded'}\n"
        f"👏 **Cleaners:** {safe_cleaner_display} — duty completed for the week!\n"
        f"🪙 **Reward:** +{base_tokens} rating tokens"
        f"{f' + {early_bonus} early-cleaning bonus' if early_bonus else ''} "
        f"(+{tokens_each} each).{approver_reward_text}"
    )
    approved_meme = config.get_meme_file("approved")
    try:
        photo_file_id = week_record.get("photo_file_id")
        if photo_file_id:
            await context.bot.send_photo(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                photo=photo_file_id,
                caption=group_message,
                parse_mode=ParseMode.MARKDOWN
            )
        elif approved_meme:
            with open(approved_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=dorm_id,
                    message_thread_id=topic_id,
                    photo=photo,
                    caption=group_message,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                text=group_message,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error("Failed to send approved cleaning photo to group: %s", e)

    cleaner_message = (
        f"🎉 **Well Done! Inspection Approved**\n\n"
        f"Approver: **{safe_actor_label}**\n"
        f"Rating: {stars_display} — _{safe_review_text}_\n"
        f"Cleaned at: **{cleaned_at or 'time not recorded'}**\n"
        f"Earned: **+{tokens_each} Tsidat tokens**"
        f"{f' (includes +{early_bonus} early-cleaning bonus)' if early_bonus else ''}.\n\n"
        "✅ You have completed your cleaning duty and are done for the week!"
    )
    for cleaner_id, cleaner_name in cleaners:
        await _notify_cleaner(
            context,
            cleaner_id,
            cleaner_name,
            cleaner_message,
            photo_path=approved_meme,
            dorm_id=dorm_id,
            topic_id=topic_id
        )
    return cleaner_display

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
    if week_record.get("final_status") != "pending" or week_record.get("photo_status") != "submitted":
        await query.answer("There is no pending cleaning photo to review.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    cleaner_names = []
    for cleaner_id in (
        week_record["cleaner1_id"],
        week_record.get("cleaner2_id", 0),
        week_record.get("cleaner3_id", 0)
    ):
        if cleaner_id:
            cleaner = await db.get_dorm_member(dorm_id, cleaner_id)
            cleaner_names.append(cleaner["name"] if cleaner else f"ID {cleaner_id}")
    cleaner_display = " & ".join(cleaner_names)

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
        f"⭐ **Rate This Week's Cleaning ({cleaner_display})**\n\n"
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

    if week_record.get("final_status") != "pending" or week_record.get("photo_status") != "submitted":
        await query.answer("This photo is no longer awaiting approval.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    stars_clamped = max(1, min(5, stars))
    review_comment = RATING_COMMENTS.get(stars_clamped, "Passed inspection.")
    stars_display = RATING_STARS_DISPLAY.get(stars_clamped, f"{stars_clamped} Stars")
    reward = await db.approve_week_and_award_tokens(
        week_start,
        dorm_id=dorm_id,
        rating=stars_clamped,
        review_text=review_comment
    )
    if reward is None:
        await _safe_edit_message(query, "This photo was already reviewed or the cleaning week is closed.")
        return

    await publish_approved_cleaning(
        context,
        dorm_id,
        week_record,
        reward,
        stars_clamped,
        review_comment,
        approver_name
    )
    approver_bonus = reward.get("approver_bonus", 0)
    approver_bonus_text = f" You earned +{approver_bonus} tokens." if approver_bonus else ""
    early_bonus = reward["early_bonus"]
    early_bonus_text = f" (includes +{early_bonus} early-cleaning bonus)" if early_bonus else ""
    confirmation_text = (
        f"✅ **Inspection Approved**\n\n"
        f"Rating: {stars_display}\n"
        f"Cleaners received **+{reward['tokens_each']} tokens each**"
        f"{early_bonus_text}."
        f"{approver_bonus_text}"
    )
    await _safe_edit_message(query, confirmation_text)

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
    if week_record.get("final_status") != "pending" or week_record.get("photo_status") != "submitted":
        await query.answer("This photo is no longer awaiting review.", show_alert=True)
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

    if not await db.reject_week_photo(week_start, dorm_id=dorm_id):
        await query.answer("This photo was already reviewed or the week is closed.", show_alert=True)
        return
    await query.answer()

    cleaner_names = []
    for cleaner_id in (
        week_record["cleaner1_id"],
        week_record.get("cleaner2_id", 0),
        week_record.get("cleaner3_id", 0)
    ):
        if cleaner_id:
            cleaner = await db.get_dorm_member(dorm_id, cleaner_id)
            cleaner_names.append(cleaner["name"] if cleaner else f"ID {cleaner_id}")
    cleaner_display = " & ".join(cleaner_names)

    # Update approver's message
    await _safe_edit_message(query, "You rejected the cleaning photo. Cleaners have been notified.")

    # Post in group chat (with rejected meme)
    group_msg = (
        f"**{approver_name}** rejected the cleaning photo.\n"
        f"**{cleaner_display}** need to clean again and send a new photo before Friday 8:00 PM."
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
    cleaner_ids = [
        cleaner_id for cleaner_id in (
            week_record["cleaner1_id"],
            week_record.get("cleaner2_id", 0),
            week_record.get("cleaner3_id", 0)
        ) if cleaner_id
    ]
    for cleaner_id, cleaner_name in zip(cleaner_ids, cleaner_names):
        if cleaner_id:
            await _notify_cleaner(
                context, cleaner_id, cleaner_name, dm_text,
                photo_path=rejected_meme, dorm_id=dorm_id, topic_id=topic_id
            )

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

async def expired_friday_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tell users that legacy Friday confirmation buttons are no longer active."""
    query = update.callback_query
    if query:
        await query.answer("This Friday check has expired. Use /status for the current result.", show_alert=True)

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
    if week_record.get("final_status") != "pending" or week_record.get("photo_status") != "submitted":
        await msg.reply_text("There is no pending cleaning photo to approve. Cleaners must submit a photo first.")
        return

    # Parse optional rating and note: /approve 5 Floors and counters were spotless.
    stars = 5
    if context.args and len(context.args) > 0:
        try:
            stars = int(context.args[0])
        except ValueError:
            await msg.reply_text("Usage: `/approve [1-5] [note]`", parse_mode=ParseMode.MARKDOWN)
            return
        if not 1 <= stars <= 5:
            await msg.reply_text("Rating must be between 1 and 5.", parse_mode=ParseMode.MARKDOWN)
            return

    review_comment = (
        " ".join(context.args[1:]).strip()
        if context.args and len(context.args) > 1
        else RATING_COMMENTS.get(stars, "Sparkling clean! 5-star hotel standard.")
    )
    reward = await db.approve_week_and_award_tokens(
        week_start,
        dorm_id=dorm_id,
        rating=stars,
        review_text=review_comment
    )
    if reward is None:
        await msg.reply_text("This photo was already reviewed or the cleaning week is closed.")
        return
    tokens_earned = reward["tokens_each"]
    actor_name = members_by_id.get(user.id, user.first_name)
    cleaner_display = await publish_approved_cleaning(
        context,
        dorm_id,
        week_record,
        reward,
        rating=stars,
        review_text=review_comment,
        approver_name=f"{actor_name} ({'Approver' if is_approver else 'Admin'})"
    )
    approver_bonus = reward.get("approver_bonus", 0)
    bonus_text = f" You earned +{approver_bonus} Tsidat tokens." if approver_bonus else ""

    # Reply to sender
    target_chat = update.effective_chat
    if target_chat and target_chat.id != dorm_id:
        await msg.reply_text(
            f"✅ Cleaning approved! Awarded +{tokens_earned} Tsidat tokens to {cleaner_display}.{bonus_text}"
        )
    else:
        await msg.reply_text(f"✅ Cleaning approved for {cleaner_display}.{bonus_text}")

def register_approval_handlers(application: Application) -> None:
    """Attach approver decision handlers."""
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("approve_clean", approve_command))
    application.add_handler(CallbackQueryHandler(approve_photo_callback, pattern=r"^approve_"))
    application.add_handler(CallbackQueryHandler(rate_photo_callback, pattern=r"^rate_\d+_"))
    application.add_handler(CallbackQueryHandler(reject_photo_callback, pattern=r"^reject_"))
    application.add_handler(
        CallbackQueryHandler(expired_friday_callback, pattern=r"^(?:fconfirm|fcheck)_")
    )
