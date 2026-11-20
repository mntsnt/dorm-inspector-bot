"""
Scheduler Module for Dorm Inspector Bot
Manages automated weekly announcements, reminders, Friday verification, and approver timeout monitoring.
Fully idempotent and resilient against bot downtime and restarts.
"""

from datetime import datetime, timedelta
import logging
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

import config
import db

logger = logging.getLogger(__name__)

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
) -> bool:
    """
    Attempt to send a private DM to the member (with optional photo).
    If it fails (e.g. user hasn't clicked /start or blocked the bot),
    fall back to mentioning them in the dorm group chat.
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
                        chat_id=config.GROUP_ID,
                        photo=photo,
                        caption=fallback_text,
                        reply_markup=reply_markup,
                        parse_mode=ParseMode.MARKDOWN
                    )
            else:
                await context.bot.send_message(
                    chat_id=config.GROUP_ID,
                    text=fallback_text,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as ge:
            logger.error("Failed to post group fallback for %s: %s", member_name, ge)
        return False

# ---------------------------------------------------------------------------
# Monday Announcement Job (08:00 Africa/Addis_Ababa)
# ---------------------------------------------------------------------------
async def monday_announcement_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Announce this week's cleaning duty on Monday morning.
    - Picks a random approver from the 6 non-cleaning members and stores in DB.
    - Announces duty in the dorm group.
    - DMs cleaners with deadline & 'We cleaned' button.
    - DMs approver about their review duty.
    - Idempotent: Never sends twice for the same week.
    """
    now_date = config.get_current_date()
    week_info = config.get_week_info(now_date)

    if week_info.week_number < 1:
        logger.info("Schedule has not started yet (starts %s). Skipping Monday announcement.", config.START_BASE_MONDAY)
        return

    eligible_approver_ids = [
        m.user_id for m in config.MEMBERS
        if m.user_id not in (week_info.cleaner1.user_id, week_info.cleaner2.user_id)
    ]

    week_record = await db.get_or_create_week(
        week_start=week_info.week_start,
        week_number=week_info.week_number,
        cleaner1_id=week_info.cleaner1.user_id,
        cleaner2_id=week_info.cleaner2.user_id,
        eligible_approver_ids=eligible_approver_ids
    )

    if week_record.get("announced") == 1:
        logger.info("Week %s already announced. Skipping.", week_info.week_start)
        return

    approver = config.get_member(week_record["approver_id"])
    approver_name = approver.name if approver else f"ID {week_record['approver_id']}"
    c1_name = week_info.cleaner1.name
    c2_name = week_info.cleaner2.name

    # 1. Post in the dorm group chat
    group_text = (
        f"Cleaners this week: **{c1_name}** & **{c2_name}**\n"
        f"Approver: **{approver_name}**\n"
        f"Deadline: Friday 8:00 PM"
    )
    announcement_meme = config.get_meme_file("announcement")
    try:
        if announcement_meme:
            with open(announcement_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=config.GROUP_ID,
                    photo=photo,
                    caption=group_text,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=config.GROUP_ID,
                text=group_text,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error("Failed to post Monday announcement to group %s: %s", config.GROUP_ID, e)

    # 2. Private DM to Cleaner 1 & Cleaner 2
    cleaner_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("We cleaned", callback_data=f"cleaned_{week_info.week_start}")]
    ])
    await send_dm_with_fallback(
        context,
        user_id=week_info.cleaner1.user_id,
        member_name=c1_name,
        text=(
            f"You have cleaning duty this week with **{c2_name}**.\n"
            f"Deadline: Friday 8:00 PM.\n\n"
            "When done, tap 'We cleaned' below and send a photo."
        ),
        reply_markup=cleaner_btn
    )
    await send_dm_with_fallback(
        context,
        user_id=week_info.cleaner2.user_id,
        member_name=c2_name,
        text=(
            f"You have cleaning duty this week with **{c1_name}**.\n"
            f"Deadline: Friday 8:00 PM.\n\n"
            "When done, tap 'We cleaned' below and send a photo."
        ),
        reply_markup=cleaner_btn
    )

    # 3. Private DM to Approver
    approver_dm_text = (
        f"You are this week's approver.\n"
        f"Cleaners: **{c1_name}** & **{c2_name}**.\n\n"
        "Please check their photo when they send it."
    )
    await send_dm_with_fallback(
        context,
        user_id=week_record["approver_id"],
        member_name=approver_name,
        text=approver_dm_text
    )

    # Mark as announced in DB
    await db.mark_announced(week_info.week_start)
    logger.info("Monday announcement successfully completed for week %s", week_info.week_start)

# ---------------------------------------------------------------------------
# Thursday Reminder Job (18:00 Africa/Addis_Ababa)
# ---------------------------------------------------------------------------
async def thursday_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send reminder to cleaners if cleaning is not yet completed."""
    now_date = config.get_current_date()
    week_info = config.get_week_info(now_date)

    if week_info.week_number < 1:
        logger.info("Schedule has not started yet (starts %s). Skipping Thursday reminder.", config.START_BASE_MONDAY)
        return
    week_record = await db.get_week(week_info.week_start)

    if not week_record:
        return

    # Skip if already finished or reminder already sent
    if week_record.get("final_status") == "completed" or week_record.get("photo_status") == "approved":
        logger.info("Thursday reminder skipped: week %s already completed.", week_info.week_start)
        return

    if week_record.get("reminder_sent") == 1:
        return

    cleaner_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("We cleaned", callback_data=f"cleaned_{week_info.week_start}")]
    ])
    reminder_text = (
        "Reminder: You have cleaning duty this week.\n"
        "Deadline: Friday 8:00 PM. Send a photo once done."
    )

    c1 = config.get_member(week_record["cleaner1_id"])
    c2 = config.get_member(week_record["cleaner2_id"])
    c1_name = c1.name if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2.name if c2 else f"ID {week_record['cleaner2_id']}"

    reminder_meme = config.get_meme_file("reminder")
    await send_dm_with_fallback(context, week_record["cleaner1_id"], c1_name, reminder_text, reply_markup=cleaner_btn, photo_path=reminder_meme)
    await send_dm_with_fallback(context, week_record["cleaner2_id"], c2_name, reminder_text, reply_markup=cleaner_btn, photo_path=reminder_meme)

    await db.mark_reminder_sent(week_info.week_start)
    logger.info("Thursday reminder sent for week %s", week_info.week_start)

# ---------------------------------------------------------------------------
# Friday Check Job (20:00 Africa/Addis_Ababa)
# ---------------------------------------------------------------------------
async def friday_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Execute Friday check at 8:00 PM:
    - Ask cleaners "Did you clean the dorm?" (Yes/No buttons)
    - If photo already approved: post in group that they cleaned.
    - Ask approver to confirm final result (Confirm Cleaned / Confirm Not Cleaned).
    - Start timeout countdown for approver response.
    """
    now_date = config.get_current_date()
    now_time = config.get_current_time()
    week_info = config.get_week_info(now_date)

    if week_info.week_number < 1:
        return

    week_record = await db.get_week(week_info.week_start)

    if not week_record:
        return

    if week_record.get("check_sent") == 1:
        return

    c1 = config.get_member(week_record["cleaner1_id"])
    c2 = config.get_member(week_record["cleaner2_id"])
    c1_name = c1.name if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2.name if c2 else f"ID {week_record['cleaner2_id']}"

    approver = config.get_member(week_record["approver_id"])
    approver_name = approver.name if approver else f"ID {week_record['approver_id']}"

    # If photo already approved, mark completed and post in group
    if week_record.get("photo_status") == "approved" or week_record.get("final_status") == "completed":
        timeout_deadline = now_time + timedelta(hours=config.APPROVER_TIMEOUT_HOURS)
        await db.set_check_sent(week_info.week_start, timeout_deadline.isoformat())
        approved_meme = config.get_meme_file("approved")
        app_msg = f"Friday Check: **{c1_name}** & **{c2_name}** cleaned and photo was approved. All done!"
        if approved_meme:
            with open(approved_meme, "rb") as photo:
                await context.bot.send_photo(chat_id=config.GROUP_ID, photo=photo, caption=app_msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(chat_id=config.GROUP_ID, text=app_msg, parse_mode=ParseMode.MARKDOWN)
        return

    # Set timeout deadline for approver
    timeout_deadline = now_time + timedelta(hours=config.APPROVER_TIMEOUT_HOURS)
    await db.set_check_sent(week_info.week_start, timeout_deadline.isoformat())

    # Send Yes/No check to both cleaners
    check_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes", callback_data=f"fcheck_yes_{week_info.week_start}"),
            InlineKeyboardButton("No", callback_data=f"fcheck_no_{week_info.week_start}"),
        ]
    ])
    cleaner_check_msg = "Did you clean the dorm this week?"
    await send_dm_with_fallback(context, week_record["cleaner1_id"], c1_name, cleaner_check_msg, reply_markup=check_keyboard)
    await send_dm_with_fallback(context, week_record["cleaner2_id"], c2_name, cleaner_check_msg, reply_markup=check_keyboard)

    # Send final confirmation request to Approver
    approver_confirm_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm Cleaned", callback_data=f"fconfirm_clean_{week_info.week_start}"),
            InlineKeyboardButton("Confirm Not Cleaned", callback_data=f"fconfirm_notclean_{week_info.week_start}"),
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
        reply_markup=approver_confirm_keyboard
    )

    logger.info("Friday check initiated for week %s", week_info.week_start)

# ---------------------------------------------------------------------------
# Background Monitor & Fallback Job (Runs every 60 seconds)
# ---------------------------------------------------------------------------
async def monitor_and_fallback_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Heartbeat monitor:
    1. Triggers Monday announcement if bot was offline at 08:00.
    2. Triggers Thursday reminder if missed.
    3. Triggers Friday check if missed.
    4. Evaluates approver timeout fallback after APPROVER_TIMEOUT_HOURS if approver didn't respond.
    """
    now = config.get_current_time()
    today = now.date()
    week_info = config.get_week_info(today)

    if week_info.week_number < 1:
        return

    week_record = await db.get_week(week_info.week_start)

    # 1. Catch missed Monday announcement
    announce_naive_time = config.ANNOUNCE_TIME.replace(tzinfo=None)
    announce_target = datetime.combine(week_info.monday_date, announce_naive_time).replace(tzinfo=config.TIMEZONE)

    if now >= announce_target:
        if not week_record or week_record.get("announced") != 1:
            logger.info("Missed Monday announcement detected for week %s. Sending now...", week_info.week_start)
            await monday_announcement_job(context)
            week_record = await db.get_week(week_info.week_start)

    if not week_record:
        return

    # 2. Catch missed Thursday reminder
    reminder_naive_time = config.REMINDER_TIME.replace(tzinfo=None)
    reminder_target = datetime.combine(week_info.monday_date + timedelta(days=3), reminder_naive_time).replace(tzinfo=config.TIMEZONE)

    if now >= reminder_target and week_record.get("reminder_sent") == 0:
        if week_record.get("final_status") != "completed" and week_record.get("photo_status") != "approved":
            logger.info("Missed Thursday reminder detected for week %s. Sending now...", week_info.week_start)
            await thursday_reminder_job(context)
            week_record = await db.get_week(week_info.week_start)

    # 3. Catch missed Friday check
    check_naive_time = config.CHECK_TIME.replace(tzinfo=None)
    check_target = datetime.combine(week_info.friday_date, check_naive_time).replace(tzinfo=config.TIMEZONE)

    if now >= check_target and week_record.get("check_sent") == 0:
        if week_record.get("final_status") == "pending":
            logger.info("Missed Friday check detected for week %s. Sending now...", week_info.week_start)
            await friday_check_job(context)
            week_record = await db.get_week(week_info.week_start)

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
                    logger.info("Approver timeout reached for week %s. Applying fallback...", week_info.week_start)
                    await _process_approver_timeout(context, week_record)
            except Exception as e:
                logger.error("Error parsing timeout deadline %s: %s", deadline_str, e)

async def _process_approver_timeout(context: ContextTypes.DEFAULT_TYPE, week_record: dict) -> None:
    """Fallback logic when approver fails to confirm within APPROVER_TIMEOUT_HOURS."""
    week_start = week_record["week_start"]
    c1 = config.get_member(week_record["cleaner1_id"])
    c2 = config.get_member(week_record["cleaner2_id"])
    c1_name = c1.name if c1 else f"ID {week_record['cleaner1_id']}"
    c2_name = c2.name if c2 else f"ID {week_record['cleaner2_id']}"

    approver = config.get_member(week_record["approver_id"])
    approver_name = approver.name if approver else f"ID {week_record['approver_id']}"

    c1_ans = week_record.get("cleaner1_response")
    c2_ans = week_record.get("cleaner2_response")
    photo_status = week_record.get("photo_status", "none")

    # Fallback to cleaners' own answer
    cleaners_claim_yes = (c1_ans == "yes" or c2_ans == "yes")
    has_photo = photo_status in ("submitted", "approved")

    if cleaners_claim_yes and has_photo:
        # Fallback decision: Cleaned
        await db.mark_timeout_processed(week_start, decision="timeout", final_status="completed")
        approved_meme = config.get_meme_file("approved")
        group_msg = (
            f"Approver **{approver_name}** didn't reply in time.\n"
            f"Photo was sent, so marked as cleaned. No fines."
        )
        if approved_meme:
            with open(approved_meme, "rb") as photo:
                await context.bot.send_photo(chat_id=config.GROUP_ID, photo=photo, caption=group_msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(chat_id=config.GROUP_ID, text=group_msg, parse_mode=ParseMode.MARKDOWN)
    else:
        # Fallback decision: Not cleaned -> Fined
        await db.mark_timeout_processed(week_start, decision="timeout", final_status="fined")
        await db.create_fines_for_week(
            week_start=week_start,
            cleaner1_id=week_record["cleaner1_id"],
            cleaner1_name=c1_name,
            cleaner2_id=week_record["cleaner2_id"],
            cleaner2_name=c2_name,
            fine_each=config.FINE_EACH
        )
        fined_meme = config.get_meme_file("fined")
        group_msg = (
            f"Approver **{approver_name}** didn't reply in time and no approved photo was sent.\n"
            f"Marked as NOT cleaned.\n\n"
            f"Fine: {config.FINE_EACH} ETB each for **{c1_name}** & **{c2_name}**."
        )
        if fined_meme:
            with open(fined_meme, "rb") as photo:
                await context.bot.send_photo(chat_id=config.GROUP_ID, photo=photo, caption=group_msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(chat_id=config.GROUP_ID, text=group_msg, parse_mode=ParseMode.MARKDOWN)

        # Notify cleaners of fine
        dm_fine_msg = (
            f"The approver didn't reply in time and no photo was approved. Duty marked as NOT cleaned.\n"
            f"Fine: **{config.FINE_EACH} ETB**."
        )
        await send_dm_with_fallback(context, week_record["cleaner1_id"], c1_name, dm_fine_msg, photo_path=fined_meme)
        await send_dm_with_fallback(context, week_record["cleaner2_id"], c2_name, dm_fine_msg, photo_path=fined_meme)

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

    logger.info("Scheduler initialized successfully.")
