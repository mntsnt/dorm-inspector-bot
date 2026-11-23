"""
Scheduler Module for Dorm Inspector Bot (Multi-Tenant Platform)
Manages automated weekly announcements, reminders, Friday verification, and approver timeout monitoring.
Fully multi-tenant: independently tracks and executes schedules for all registered dorms.
Idempotent and resilient against bot downtime and restarts.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

import config
import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Structures & Helpers for Dorm Schedules
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DormWeekInfo:
    week_start: str         # "YYYY-MM-DD" (Monday)
    week_number: int        # 1-indexed from dorm's start_date
    cleaner1_id: int
    cleaner2_id: int
    cleaner1_name: str
    cleaner2_name: str
    monday_date: date
    friday_date: date

def get_dorm_week_info(
    dorm: Dict[str, Any],
    pairs: List[Dict[str, Any]],
    members_by_id: Dict[int, str],
    target_date: Optional[date] = None,
) -> Optional[DormWeekInfo]:
    """Calculate the weekly cleaning duty for a specific dorm based on its start_date and pairs."""
    if not pairs:
        return None

    if target_date is None:
        target_date = config.get_current_date()

    start_date_str = dorm.get("start_date") or getattr(config, "START_DATE_STR", "2026-11-23")
    try:
        start_date_obj = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except Exception:
        start_date_obj = getattr(config, "START_BASE_MONDAY", date(2026, 11, 23))

    start_base_monday = config.get_monday_of_week(start_date_obj)
    target_monday = config.get_monday_of_week(target_date)

    days_diff = (target_monday - start_base_monday).days
    weeks_diff = days_diff // 7
    week_number = weeks_diff + 1

    pair_index = 0 if week_number < 1 else (week_number - 1) % len(pairs)
    pair = pairs[pair_index]
    c1_id = pair["cleaner1_id"]
    c2_id = pair["cleaner2_id"]

    c1_name = members_by_id.get(c1_id, f"ID {c1_id}")
    c2_name = members_by_id.get(c2_id, f"ID {c2_id}")
    friday_date = target_monday + timedelta(days=4)

    return DormWeekInfo(
        week_start=target_monday.strftime("%Y-%m-%d"),
        week_number=week_number,
        cleaner1_id=c1_id,
        cleaner2_id=c2_id,
        cleaner1_name=c1_name,
        cleaner2_name=c2_name,
        monday_date=target_monday,
        friday_date=friday_date,
    )

# ---------------------------------------------------------------------------
# DM Helper with Group Fallback
# ---------------------------------------------------------------------------
async def send_dm_with_fallback(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    member_name: str,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    photo_path: Optional[Path] = None,
    dorm_id: Optional[int] = None,
    topic_id: Optional[int] = None,
) -> bool:
    """
    Attempt to send a private DM to the member (with optional photo).
    If it fails (e.g. user hasn't clicked /start or blocked the bot),
    fall back to mentioning them in their dorm's group chat.
    """
    try:
        if photo_path and photo_path.is_file():
            with open(photo_path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        return True
    except TelegramError as te:
        logger.warning("Could not send private DM to %s (%s): %s", member_name, user_id, te)
        target_group = dorm_id if dorm_id is not None else getattr(config, "GROUP_ID", 0)
        target_topic = topic_id if topic_id is not None else getattr(config, "TOPIC_ID", None)

        if not target_group:
            return False

        try:
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username or "the bot"
            fallback_text = (
                f"For **{member_name}**:\n"
                f"{text}\n\n"
                f"_(Could not DM you. Please message @{bot_username} and send /start)_"
            )
            if photo_path and photo_path.is_file():
                with open(photo_path, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=target_group,
                        message_thread_id=target_topic,
                        photo=photo,
                        caption=fallback_text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN
                    )
            else:
                await context.bot.send_message(
                    chat_id=target_group,
                    message_thread_id=target_topic,
                    text=fallback_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as ge:
            logger.error("Failed to post group fallback for %s in dorm %s: %s", member_name, target_group, ge)
        return False

# ---------------------------------------------------------------------------
# Robust Group Delivery Helper
# ---------------------------------------------------------------------------
async def post_group_announcement_safe(
    context: ContextTypes.DEFAULT_TYPE,
    dorm_id: int,
    topic_id: Optional[int],
    group_text: str,
    meme_path: Optional[Path] = None,
) -> tuple[bool, str]:
    """
    Deliver the group announcement with progressive resilience:
    1. Try with photo, topic_id, and Markdown
    2. Try with photo and Markdown (no topic_id, in case topic_id is invalid)
    3. Try text message with topic_id and Markdown
    4. Try text message with topic_id plain text (fixes any Markdown parsing issues)
    5. Try text message without topic_id plain text (ensures group delivery even if topic closed/invalid)
    """
    bot = context.bot
    errs = []

    # Attempt 1: Photo + Topic + Markdown
    if meme_path and meme_path.is_file():
        try:
            with open(meme_path, "rb") as photo:
                await bot.send_photo(
                    chat_id=dorm_id,
                    message_thread_id=topic_id,
                    photo=photo,
                    caption=group_text,
                    parse_mode=ParseMode.MARKDOWN
                )
            return True, "Delivered photo to group topic."
        except Exception as e1:
            errs.append(f"Photo+Topic: {e1}")
            logger.warning("Attempt 1 (photo+topic) failed for dorm %s: %s", dorm_id, e1)

        # Attempt 2: Photo + General (no topic)
        if topic_id:
            try:
                with open(meme_path, "rb") as photo:
                    await bot.send_photo(
                        chat_id=dorm_id,
                        photo=photo,
                        caption=group_text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                return True, "Delivered photo to general group chat."
            except Exception as e2:
                errs.append(f"Photo+General: {e2}")
                logger.warning("Attempt 2 (photo+general) failed for dorm %s: %s", dorm_id, e2)

    # Attempt 3: Text + Topic + Markdown
    try:
        await bot.send_message(
            chat_id=dorm_id,
            message_thread_id=topic_id,
            text=group_text,
            parse_mode=ParseMode.MARKDOWN
        )
        return True, "Delivered text message to group topic."
    except Exception as e3:
        errs.append(f"Text+Topic: {e3}")
        logger.warning("Attempt 3 (text+topic) failed for dorm %s: %s", dorm_id, e3)

    # Attempt 4: Text + Topic + Plain text
    clean_text = group_text.replace("**", "").replace("__", "")
    try:
        await bot.send_message(
            chat_id=dorm_id,
            message_thread_id=topic_id,
            text=clean_text
        )
        return True, "Delivered plain text message to group topic."
    except Exception as e4:
        errs.append(f"Text+Plain: {e4}")
        logger.warning("Attempt 4 (text+plain) failed for dorm %s: %s", dorm_id, e4)

    # Attempt 5: Text + General (no topic) + Plain text
    if topic_id:
        try:
            await bot.send_message(
                chat_id=dorm_id,
                text=clean_text
            )
            return True, "Delivered plain text to general group chat."
        except Exception as e5:
            errs.append(f"Text+General: {e5}")
            logger.error("Attempt 5 (text+general) failed for dorm %s: %s", dorm_id, e5)

    return False, f"All delivery attempts failed: {'; '.join(errs)}"

async def send_dorm_announcement(
    context: ContextTypes.DEFAULT_TYPE,
    dorm_id: int,
    force: bool = False,
    now_date: Optional[date] = None,
) -> tuple[bool, str]:
    """
    Announce cleaning duty for a dorm.
    If force=True, delivers announcement even if previously marked as announced.
    """
    if now_date is None:
        now_date = config.get_current_date()

    dorm = await db.get_dorm(dorm_id)
    if not dorm:
        return False, f"Dorm {dorm_id} not found."

    members = await db.get_dorm_members(dorm_id)
    pairs = await db.get_dorm_pairs(dorm_id)
    if not pairs:
        return False, "No pairs configured for this dorm."

    members_by_id = {m["user_id"]: m["name"] for m in members}
    week_info = get_dorm_week_info(dorm, pairs, members_by_id, now_date)
    if not week_info or week_info.week_number < 1:
        return False, "Schedule has not started yet."

    eligible_approver_ids = [
        m["user_id"] for m in members
        if m["user_id"] not in (week_info.cleaner1_id, week_info.cleaner2_id)
    ]
    if not eligible_approver_ids:
        eligible_approver_ids = [week_info.cleaner1_id]

    week_record = await db.get_or_create_week(
        week_start=week_info.week_start,
        week_number=week_info.week_number,
        cleaner1_id=week_info.cleaner1_id,
        cleaner2_id=week_info.cleaner2_id,
        eligible_approver_ids=eligible_approver_ids,
        dorm_id=dorm_id
    )

    if not force and week_record.get("announced") == 1:
        return False, f"Week {week_info.week_number} has already been announced."

    approver_id = week_record["approver_id"]
    approver_name = members_by_id.get(approver_id, f"ID {approver_id}")
    c1_name = week_info.cleaner1_name
    c2_name = week_info.cleaner2_name

    # 1. Post in the dorm group chat
    group_text = (
        f"📢 **Week {week_info.week_number} Duty Announcement!**\n\n"
        f"Cleaners this week: **{c1_name}** & **{c2_name}**\n"
        f"Approver: **{approver_name}**\n"
        f"Deadline: Friday 8:00 PM\n\n"
        f"💬 Roommates, let's encourage **{c1_name}** and **{c2_name}** to keep them motivated! "
        f"Feel free to help them with cleaning if you like. 🙌"
    )

    announcement_meme = config.get_meme_file("announcement")
    topic_id = dorm.get("topic_id")

    success, detail = await post_group_announcement_safe(
        context, dorm_id, topic_id, group_text, announcement_meme
    )

    if not success:
        logger.error("Group announcement delivery failed for dorm %s: %s", dorm_id, detail)
        return False, f"Group delivery failed: {detail}"

    # Follow-up reaction meme
    me_meme = config.get_meme_file("me")
    if me_meme and me_meme.is_file():
        try:
            with open(me_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=dorm_id,
                    message_thread_id=topic_id,
                    photo=photo,
                    caption=f"**{c1_name} and {c2_name} be like:**",
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception:
            pass

    # 2. Private DMs to Cleaners
    cleaner_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("We cleaned", callback_data=f"cleaned_{dorm_id}_{week_info.week_start}")]
    ])
    await send_dm_with_fallback(
        context,
        user_id=week_info.cleaner1_id,
        member_name=c1_name,
        text=(
            f"You have cleaning duty this week with **{c2_name}**.\n"
            f"Deadline: Friday 8:00 PM.\n\n"
            "When done, tap 'We cleaned' below and send a photo."
        ),
        reply_markup=cleaner_btn,
        dorm_id=dorm_id,
        topic_id=topic_id,
    )
    await send_dm_with_fallback(
        context,
        user_id=week_info.cleaner2_id,
        member_name=c2_name,
        text=(
            f"You have cleaning duty this week with **{c1_name}**.\n"
            f"Deadline: Friday 8:00 PM.\n\n"
            "When done, tap 'We cleaned' below and send a photo."
        ),
        reply_markup=cleaner_btn,
        dorm_id=dorm_id,
        topic_id=topic_id,
    )

    # 3. Private DM to Approver
    approver_dm_text = (
        f"You are this week's approver.\n"
        f"Cleaners: **{c1_name}** & **{c2_name}**.\n\n"
        "Please check their photo when they send it."
    )
    await send_dm_with_fallback(
        context,
        user_id=approver_id,
        member_name=approver_name,
        text=approver_dm_text,
        dorm_id=dorm_id,
        topic_id=topic_id,
    )

    await db.mark_announced(week_info.week_start, dorm_id=dorm_id)
    logger.info("Monday announcement completed for dorm %s, week %s (%s)", dorm_id, week_info.week_start, detail)
    return True, f"✅ Week {week_info.week_number} announcement sent to group and cleaners ({detail})"

# ---------------------------------------------------------------------------
# Monday Announcement Job (08:00 Africa/Addis_Ababa)
# ---------------------------------------------------------------------------
async def monday_announcement_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Announce this week's cleaning duty for all active dorms on Monday morning."""
    dorms = await db.get_all_active_dorms()
    if not dorms and getattr(config, "GROUP_ID", 0):
        await db.seed_default_dorm()
        dorms = await db.get_all_active_dorms()

    for dorm in dorms:
        dorm_id = dorm["dorm_id"]
        try:
            await send_dorm_announcement(context, dorm_id, force=False)
        except Exception as e:
            logger.error("Error running Monday announcement for dorm %s: %s", dorm_id, e)

# ---------------------------------------------------------------------------
# Thursday Reminder Job (18:00 Africa/Addis_Ababa)
# ---------------------------------------------------------------------------
async def thursday_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send reminder to cleaners if cleaning is not yet completed for all active dorms."""
    dorms = await db.get_all_active_dorms()
    if not dorms and getattr(config, "GROUP_ID", 0):
        await db.seed_default_dorm()
        dorms = await db.get_all_active_dorms()

    now_date = config.get_current_date()

    for dorm in dorms:
        dorm_id = dorm["dorm_id"]
        topic_id = dorm.get("topic_id")
        try:
            members = await db.get_dorm_members(dorm_id)
            pairs = await db.get_dorm_pairs(dorm_id)
            if not pairs:
                continue

            members_by_id = {m["user_id"]: m["name"] for m in members}
            week_info = get_dorm_week_info(dorm, pairs, members_by_id, now_date)
            if not week_info or week_info.week_number < 1:
                continue

            week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)
            if not week_record:
                continue

            # Skip if already finished or reminder already sent
            if week_record.get("final_status") == "completed" or week_record.get("photo_status") == "approved":
                continue

            if week_record.get("reminder_sent") == 1:
                continue

            cleaner_btn = InlineKeyboardMarkup([
                [InlineKeyboardButton("We cleaned", callback_data=f"cleaned_{dorm_id}_{week_info.week_start}")]
            ])
            reminder_text = (
                "Reminder: You have cleaning duty this week.\n"
                "Deadline: Friday 8:00 PM. Send a photo once done."
            )

            c1_name = members_by_id.get(week_record["cleaner1_id"], f"ID {week_record['cleaner1_id']}")
            c2_name = members_by_id.get(week_record["cleaner2_id"], f"ID {week_record['cleaner2_id']}")

            reminder_meme = config.get_meme_file("reminder")
            await send_dm_with_fallback(
                context, week_record["cleaner1_id"], c1_name, reminder_text,
                reply_markup=cleaner_btn, photo_path=reminder_meme, dorm_id=dorm_id, topic_id=topic_id
            )
            await send_dm_with_fallback(
                context, week_record["cleaner2_id"], c2_name, reminder_text,
                reply_markup=cleaner_btn, photo_path=reminder_meme, dorm_id=dorm_id, topic_id=topic_id
            )

            await db.mark_reminder_sent(week_info.week_start, dorm_id=dorm_id)
            logger.info("Thursday reminder sent for dorm %s, week %s", dorm_id, week_info.week_start)
        except Exception as e:
            logger.error("Error in Thursday reminder for dorm %s: %s", dorm_id, e)

# ---------------------------------------------------------------------------
# Friday Check Job (20:00 Africa/Addis_Ababa)
# ---------------------------------------------------------------------------
async def friday_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Execute Friday check at 8:00 PM for all active dorms:
    - Ask cleaners "Did you clean the dorm?" (Yes/No buttons)
    - If photo already approved: post in group that they cleaned.
    - Ask approver to confirm final result (Confirm Cleaned / Confirm Not Cleaned).
    - Start timeout countdown for approver response.
    """
    dorms = await db.get_all_active_dorms()
    if not dorms and getattr(config, "GROUP_ID", 0):
        await db.seed_default_dorm()
        dorms = await db.get_all_active_dorms()

    now_date = config.get_current_date()
    now_time = config.get_current_time()

    for dorm in dorms:
        dorm_id = dorm["dorm_id"]
        topic_id = dorm.get("topic_id")
        try:
            members = await db.get_dorm_members(dorm_id)
            pairs = await db.get_dorm_pairs(dorm_id)
            if not pairs:
                continue

            members_by_id = {m["user_id"]: m["name"] for m in members}
            week_info = get_dorm_week_info(dorm, pairs, members_by_id, now_date)
            if not week_info or week_info.week_number < 1:
                continue

            week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)
            if not week_record or week_record.get("check_sent") == 1:
                continue

            c1_name = members_by_id.get(week_record["cleaner1_id"], f"ID {week_record['cleaner1_id']}")
            c2_name = members_by_id.get(week_record["cleaner2_id"], f"ID {week_record['cleaner2_id']}")
            approver_name = members_by_id.get(week_record["approver_id"], f"ID {week_record['approver_id']}")

            # If photo already approved, mark completed and post in group
            if week_record.get("photo_status") == "approved" or week_record.get("final_status") == "completed":
                timeout_deadline = now_time + timedelta(hours=config.APPROVER_TIMEOUT_HOURS)
                await db.set_check_sent(week_info.week_start, timeout_deadline.isoformat(), dorm_id=dorm_id)
                approved_meme = config.get_meme_file("approved")
                app_msg = f"Friday Check: **{c1_name}** & **{c2_name}** cleaned and photo was approved. All done!"
                if approved_meme:
                    with open(approved_meme, "rb") as photo:
                        await context.bot.send_photo(chat_id=dorm_id, message_thread_id=topic_id, photo=photo, caption=app_msg, parse_mode=ParseMode.MARKDOWN)
                else:
                    await context.bot.send_message(chat_id=dorm_id, message_thread_id=topic_id, text=app_msg, parse_mode=ParseMode.MARKDOWN)
                continue

            # Set timeout deadline for approver
            timeout_deadline = now_time + timedelta(hours=config.APPROVER_TIMEOUT_HOURS)
            await db.set_check_sent(week_info.week_start, timeout_deadline.isoformat(), dorm_id=dorm_id)

            # Send Yes/No check to both cleaners
            check_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Yes", callback_data=f"fcheck_yes_{dorm_id}_{week_info.week_start}"),
                    InlineKeyboardButton("No", callback_data=f"fcheck_no_{dorm_id}_{week_info.week_start}"),
                ]
            ])
            cleaner_check_msg = "Did you clean the dorm this week?"
            await send_dm_with_fallback(context, week_record["cleaner1_id"], c1_name, cleaner_check_msg, reply_markup=check_keyboard, dorm_id=dorm_id, topic_id=topic_id)
            await send_dm_with_fallback(context, week_record["cleaner2_id"], c2_name, cleaner_check_msg, reply_markup=check_keyboard, dorm_id=dorm_id, topic_id=topic_id)

            # Send final confirmation request to Approver
            approver_confirm_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Confirm Cleaned", callback_data=f"fconfirm_clean_{dorm_id}_{week_info.week_start}"),
                    InlineKeyboardButton("Confirm Not Cleaned", callback_data=f"fconfirm_notclean_{dorm_id}_{week_info.week_start}"),
                ]
            ])
            photo_status = week_record.get("photo_status", "none")
            status_desc = "Photo sent" if photo_status == "submitted" else ("Photo rejected" if photo_status == "rejected" else "No photo sent")

            approver_confirm_msg = (
                f"Friday Check:\n"
                f"Cleaners: **{c1_name}** & **{c2_name}**\n"
                f"Status: {status_desc}\n\n"
                f"Did they clean the dorm? Confirm within {config.APPROVER_TIMEOUT_HOURS} hours:"
            )
            await send_dm_with_fallback(
                context,
                week_record["approver_id"],
                approver_name,
                approver_confirm_msg,
                reply_markup=approver_confirm_keyboard,
                dorm_id=dorm_id,
                topic_id=topic_id
            )

            logger.info("Friday check initiated for dorm %s, week %s", dorm_id, week_info.week_start)
        except Exception as e:
            logger.error("Error in Friday check for dorm %s: %s", dorm_id, e)

# ---------------------------------------------------------------------------
# Background Monitor & Fallback Job (Runs every 60 seconds)
# ---------------------------------------------------------------------------
async def monitor_and_fallback_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Heartbeat monitor across all active dorms:
    1. Triggers Monday announcement if bot was offline at 08:00.
    2. Triggers Thursday reminder if missed.
    3. Triggers Friday check if missed.
    4. Evaluates approver timeout fallback after APPROVER_TIMEOUT_HOURS if approver didn't respond.
    """
    dorms = await db.get_all_active_dorms()
    if not dorms and getattr(config, "GROUP_ID", 0):
        await db.seed_default_dorm()
        dorms = await db.get_all_active_dorms()

    now = config.get_current_time()
    today = now.date()

    for dorm in dorms:
        dorm_id = dorm["dorm_id"]
        try:
            members = await db.get_dorm_members(dorm_id)
            pairs = await db.get_dorm_pairs(dorm_id)
            if not pairs:
                continue

            members_by_id = {m["user_id"]: m["name"] for m in members}
            week_info = get_dorm_week_info(dorm, pairs, members_by_id, today)
            if not week_info or week_info.week_number < 1:
                continue

            week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)

            # 1. Catch missed Monday announcement
            announce_naive_time = config.ANNOUNCE_TIME.replace(tzinfo=None)
            announce_target = datetime.combine(week_info.monday_date, announce_naive_time).replace(tzinfo=config.TIMEZONE)

            if now >= announce_target:
                if not week_record or week_record.get("announced") != 1:
                    logger.info("Missed Monday announcement detected for dorm %s, week %s. Sending now...", dorm_id, week_info.week_start)
                    await monday_announcement_job(context)
                    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)

            if not week_record:
                continue

            # 2. Catch missed Thursday reminder
            reminder_naive_time = config.REMINDER_TIME.replace(tzinfo=None)
            reminder_target = datetime.combine(week_info.monday_date + timedelta(days=3), reminder_naive_time).replace(tzinfo=config.TIMEZONE)

            if now >= reminder_target and week_record.get("reminder_sent") == 0:
                if week_record.get("final_status") != "completed" and week_record.get("photo_status") != "approved":
                    logger.info("Missed Thursday reminder detected for dorm %s, week %s. Sending now...", dorm_id, week_info.week_start)
                    await thursday_reminder_job(context)
                    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)

            # 3. Catch missed Friday check
            check_naive_time = config.CHECK_TIME.replace(tzinfo=None)
            check_target = datetime.combine(week_info.friday_date, check_naive_time).replace(tzinfo=config.TIMEZONE)

            if now >= check_target and week_record.get("check_sent") == 0:
                if week_record.get("final_status") == "pending":
                    logger.info("Missed Friday check detected for dorm %s, week %s. Sending now...", dorm_id, week_info.week_start)
                    await friday_check_job(context)
                    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)

            # 4. Approver Timeout Fallback Check
            if (
                week_record.get("check_sent") == 1
                and week_record.get("timeout_processed") == 0
                and week_record.get("final_status") == "pending"
                and week_record.get("approver_final_decision") is None
            ):
                deadline_str = week_record.get("timeout_deadline")
                if deadline_str:
                    try:
                        deadline = datetime.fromisoformat(deadline_str)
                        if deadline.tzinfo is None:
                            deadline = deadline.replace(tzinfo=config.TIMEZONE)
                        if now >= deadline:
                            logger.info("Approver timeout reached for dorm %s, week %s. Applying fallback...", dorm_id, week_info.week_start)
                            await _process_approver_timeout(context, dorm, week_record, members_by_id)
                    except Exception as e:
                        logger.error("Error parsing timeout deadline %s: %s", deadline_str, e)
        except Exception as e:
            logger.error("Error in monitor_and_fallback_job for dorm %s: %s", dorm_id, e)

async def _process_approver_timeout(
    context: ContextTypes.DEFAULT_TYPE,
    dorm: Dict[str, Any],
    week_record: Dict[str, Any],
    members_by_id: Dict[int, str]
) -> None:
    """Fallback logic when approver fails to confirm within APPROVER_TIMEOUT_HOURS."""
    dorm_id = dorm["dorm_id"]
    topic_id = dorm.get("topic_id")
    fine_total = dorm.get("fine_amount", 100)
    fine_each = fine_total // 2

    week_start = week_record["week_start"]
    c1_id = week_record["cleaner1_id"]
    c2_id = week_record["cleaner2_id"]
    c1_name = members_by_id.get(c1_id, f"ID {c1_id}")
    c2_name = members_by_id.get(c2_id, f"ID {c2_id}")

    approver_id = week_record["approver_id"]
    approver_name = members_by_id.get(approver_id, f"ID {approver_id}")

    c1_ans = week_record.get("cleaner1_response")
    c2_ans = week_record.get("cleaner2_response")
    photo_status = week_record.get("photo_status", "none")

    cleaners_claim_yes = (c1_ans == "yes" or c2_ans == "yes")
    has_photo = photo_status in ("submitted", "approved")

    if cleaners_claim_yes and has_photo:
        await db.mark_timeout_processed(week_start, decision="timeout", final_status="completed", dorm_id=dorm_id)
        approved_meme = config.get_meme_file("approved")
        group_msg = (
            f"Approver **{approver_name}** didn't reply in time.\n"
            f"Photo was sent, so marked as cleaned. No fines."
        )
        if approved_meme:
            with open(approved_meme, "rb") as photo:
                await context.bot.send_photo(chat_id=dorm_id, message_thread_id=topic_id, photo=photo, caption=group_msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(chat_id=dorm_id, message_thread_id=topic_id, text=group_msg, parse_mode=ParseMode.MARKDOWN)
    else:
        await db.mark_timeout_processed(week_start, decision="timeout", final_status="fined", dorm_id=dorm_id)
        await db.create_fines_for_week(
            week_start=week_start,
            cleaner1_id=c1_id,
            cleaner1_name=c1_name,
            cleaner2_id=c2_id,
            cleaner2_name=c2_name,
            fine_each=fine_each,
            dorm_id=dorm_id
        )
        fined_meme = config.get_meme_file("fined")
        group_msg = (
            f"Approver **{approver_name}** didn't reply in time and no approved photo was sent.\n"
            f"Marked as NOT cleaned.\n\n"
            f"Fine: {fine_each} ETB each for **{c1_name}** & **{c2_name}** ({fine_total} ETB total)."
        )
        if fined_meme:
            with open(fined_meme, "rb") as photo:
                await context.bot.send_photo(chat_id=dorm_id, message_thread_id=topic_id, photo=photo, caption=group_msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(chat_id=dorm_id, message_thread_id=topic_id, text=group_msg, parse_mode=ParseMode.MARKDOWN)

        dm_fine_msg = (
            f"The approver didn't reply in time and no photo was approved. Duty marked as NOT cleaned.\n"
            f"Fine: **{fine_each} ETB**."
        )
        await send_dm_with_fallback(context, c1_id, c1_name, dm_fine_msg, photo_path=fined_meme, dorm_id=dorm_id, topic_id=topic_id)
        await send_dm_with_fallback(context, c2_id, c2_name, dm_fine_msg, photo_path=fined_meme, dorm_id=dorm_id, topic_id=topic_id)

# ---------------------------------------------------------------------------
# Scheduler Setup
# ---------------------------------------------------------------------------
def setup_scheduler(application: Application) -> None:
    """Register all scheduled jobs into python-telegram-bot's JobQueue."""
    job_queue = application.job_queue
    if not job_queue:
        logger.error("JobQueue is not available in Application. Make sure python-telegram-bot[job-queue] is installed.")
        return

    # 1. Monday morning announcement
    announce_day_ptb = config.DAY_MAP_PTB.get(config.ANNOUNCE_DAY, 1)
    job_queue.run_daily(
        callback=monday_announcement_job,
        time=config.ANNOUNCE_TIME,
        days=(announce_day_ptb,),
        name="monday_announcement"
    )

    # 2. Thursday reminder
    reminder_day_ptb = config.DAY_MAP_PTB.get(config.REMINDER_DAY, 4)
    job_queue.run_daily(
        callback=thursday_reminder_job,
        time=config.REMINDER_TIME,
        days=(reminder_day_ptb,),
        name="thursday_reminder"
    )

    # 3. Friday check
    check_day_ptb = config.DAY_MAP_PTB.get(config.CHECK_DAY, 5)
    job_queue.run_daily(
        callback=friday_check_job,
        time=config.CHECK_TIME,
        days=(check_day_ptb,),
        name="friday_check"
    )

    # 4. Background monitor and fallback (runs every 60 seconds)
    job_queue.run_repeating(
        callback=monitor_and_fallback_job,
        interval=60,
        first=5,
        name="monitor_and_fallback"
    )

    logger.info("Multi-tenant scheduler initialized successfully.")
