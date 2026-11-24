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
    cleaner3_id: int = 0
    cleaner3_name: str = ""

    @property
    def cleaners(self) -> List[tuple[int, str]]:
        res = [(self.cleaner1_id, self.cleaner1_name)]
        if self.cleaner2_id:
            res.append((self.cleaner2_id, self.cleaner2_name))
        if self.cleaner3_id:
            res.append((self.cleaner3_id, self.cleaner3_name))
        return res

    @property
    def cleaner_ids(self) -> List[int]:
        return [cid for cid, _ in self.cleaners if cid]

    @property
    def cleaner_names(self) -> List[str]:
        return [name for cid, name in self.cleaners if cid]

    @property
    def cleaner_display(self) -> str:
        names = self.cleaner_names
        if not names:
            return "Nobody"
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} & {names[1]}"
        return f"{', '.join(names[:-1])} & {names[-1]}"

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
    c2_id = pair.get("cleaner2_id", 0)
    c3_id = pair.get("cleaner3_id", 0)

    c1_name = members_by_id.get(c1_id, f"ID {c1_id}")
    c2_name = members_by_id.get(c2_id, f"ID {c2_id}") if c2_id else ""
    c3_name = members_by_id.get(c3_id, f"ID {c3_id}") if c3_id else ""
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
        cleaner3_id=c3_id,
        cleaner3_name=c3_name,
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
    1. Progressive thread targets: topic_id -> General topic (1) -> None
    2. Photo delivery with Markdown then fallback to text delivery
    3. Text delivery with Markdown then plain text
    4. Auto-resolves chat migration and stale topic IDs
    """
    bot = context.bot
    errs = []
    clean_text = group_text.replace("**", "").replace("__", "")
    actual_dorm_id = dorm_id

    # Progressive thread candidate targets
    thread_candidates: list[Optional[int]] = []
    if topic_id is not None:
        thread_candidates.append(topic_id)
    if 1 not in thread_candidates:
        thread_candidates.append(1)
    if None not in thread_candidates:
        thread_candidates.append(None)

    # 1. Attempt photo delivery
    if meme_path and meme_path.is_file():
        for tid in thread_candidates:
            try:
                with open(meme_path, "rb") as photo:
                    await bot.send_photo(
                        chat_id=actual_dorm_id,
                        message_thread_id=tid,
                        photo=photo,
                        caption=group_text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                return True, f"Delivered photo to group (thread: {tid})."
            except Exception as e1:
                errs.append(f"Photo(tid={tid}): {e1}")
                new_chat_id = getattr(e1, "migrate_to_chat_id", None)
                if new_chat_id:
                    logger.info("Chat migrated from %s to %s. Updating...", actual_dorm_id, new_chat_id)
                    actual_dorm_id = new_chat_id
                    try:
                        await db.update_dorm_settings(dorm_id, dorm_id=new_chat_id)
                    except Exception:
                        pass

    # 2. Attempt text message delivery with progressive formatting
    for tid in thread_candidates:
        # A. Markdown format
        try:
            await bot.send_message(
                chat_id=actual_dorm_id,
                message_thread_id=tid,
                text=group_text,
                parse_mode=ParseMode.MARKDOWN
            )
            return True, f"Delivered text to group (thread: {tid})."
        except Exception as e2:
            errs.append(f"Text-MD(tid={tid}): {e2}")
            new_chat_id = getattr(e2, "migrate_to_chat_id", None)
            if new_chat_id:
                actual_dorm_id = new_chat_id

        # B. Plain text format (recovers from broken markdown tags)
        try:
            await bot.send_message(
                chat_id=actual_dorm_id,
                message_thread_id=tid,
                text=clean_text
            )
            return True, f"Delivered plain text to group (thread: {tid})."
        except Exception as e3:
            errs.append(f"Text-Plain(tid={tid}): {e3}")

    return False, f"All delivery attempts failed: {'; '.join(errs)}"

async def send_dorm_announcement(
    context: ContextTypes.DEFAULT_TYPE,
    dorm_id: int,
    now_date: Optional[date] = None,
) -> tuple[bool, str]:
    """Announce a dorm's weekly duty once, with an atomic cross-job claim."""
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

    if week_record.get("announced") == 1:
        return False, f"Week {week_info.week_number} has already been announced."
    if not await db.claim_week_announcement(week_info.week_start, dorm_id=dorm_id):
        return False, f"Week {week_info.week_number} announcement is already in progress or complete."

    approver_id = week_record["approver_id"]
    approver_name = members_by_id.get(approver_id, f"ID {approver_id}")
    cleaner_disp = week_info.cleaner_display

    # 1. Post in the dorm group chat
    group_text = (
        f"📢 **Week {week_info.week_number} Duty Announcement!**\n\n"
        f"Cleaners this week: **{cleaner_disp}**\n"
        f"Approver: **{approver_name}**\n"
        f"Deadline: Friday 8:00 PM\n\n"
        f"💬 Roommates, let's encourage **{cleaner_disp}** to keep them motivated! "
        f"Feel free to help them with cleaning if you like. 🙌"
    )

    announcement_meme = config.get_meme_file("announcement")
    topic_id = dorm.get("topic_id")

    success, detail = await post_group_announcement_safe(
        context, dorm_id, topic_id, group_text, announcement_meme
    )

    await db.mark_announced(week_info.week_start, dorm_id=dorm_id)

    if not success:
        logger.warning("Group announcement delivery failed for dorm %s: %s. Continuing with direct DMs to cleaners...", dorm_id, detail)
    else:
        # Follow-up reaction meme
        me_meme = config.get_meme_file("me")
        if me_meme and me_meme.is_file():
            try:
                with open(me_meme, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=dorm_id,
                        message_thread_id=topic_id,
                        photo=photo,
                        caption=f"**{cleaner_disp} be like:**",
                        parse_mode=ParseMode.MARKDOWN
                    )
            except Exception:
                pass

    # 2. Private DMs to Cleaners
    cleaner_btn = InlineKeyboardMarkup([
        [InlineKeyboardButton("We cleaned", callback_data=f"cleaned_{dorm_id}_{week_info.week_start}")]
    ])
    for c_id, c_name in week_info.cleaners:
        if not c_id:
            continue
        partners = [p_name for p_id, p_name in week_info.cleaners if p_id != c_id and p_id]
        partner_str = f" with **{' & '.join(partners)}**" if partners else " (Solo duty)"
        await send_dm_with_fallback(
            context,
            user_id=c_id,
            member_name=c_name,
            text=(
                f"You have cleaning duty this week{partner_str}.\n"
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

    logger.info("Monday announcement completed for dorm %s, week %s (%s)", dorm_id, week_info.week_start, detail)
    if success:
        return True, f"✅ Week {week_info.week_number} announcement sent to group and cleaners ({detail})"
    return True, f"⚠️ Week {week_info.week_number} announcement sent to cleaners via DM ({detail})"

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
            await send_dorm_announcement(context, dorm_id)
        except Exception as e:
            logger.error("Error running Monday announcement for dorm %s: %s", dorm_id, e)

# ---------------------------------------------------------------------------
# Six-Hour Cleaner Reminder
# ---------------------------------------------------------------------------
async def send_dorm_cleaner_reminder(
    context: ContextTypes.DEFAULT_TYPE,
    dorm_id: int,
    now_date: Optional[date] = None,
    now_time: Optional[datetime] = None,
) -> bool:
    """Send one cleaner reminder when the six-hour daytime cadence is due."""
    current_time = now_time or config.get_current_time()
    if now_date is None:
        now_date = current_time.date()
    if (
        now_date.weekday() > 4
        or current_time.hour < 8
        or current_time.hour > 20
        or (current_time.hour == 20 and current_time.minute > 0)
    ):
        return False

    dorm = await db.get_dorm(dorm_id)
    if not dorm or not dorm.get("is_active", 1):
        return False

    topic_id = dorm.get("topic_id")
    members = await db.get_dorm_members(dorm_id)
    pairs = await db.get_dorm_pairs(dorm_id)
    if not pairs:
        return False

    members_by_id = {m["user_id"]: m["name"] for m in members}
    week_info = get_dorm_week_info(dorm, pairs, members_by_id, now_date)
    if not week_info or week_info.week_number < 1:
        return False

    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)
    if not week_record:
        return False

    if (
        week_record.get("final_status") != "pending"
        or week_record.get("photo_status") in ("submitted", "approved")
        or now_date > week_info.friday_date
    ):
        return False

    deadline = datetime.combine(week_info.friday_date, config.CHECK_TIME)
    if current_time >= deadline:
        return False

    if not await db.claim_week_reminder(week_info.week_start, dorm_id=dorm_id, interval_hours=6):
        return False

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
    for c_id, c_name in week_info.cleaners:
        if not c_id:
            continue
        await send_dm_with_fallback(
            context, c_id, c_name, reminder_text,
            reply_markup=cleaner_btn, photo_path=reminder_meme, dorm_id=dorm_id, topic_id=topic_id
        )

    logger.info("Six-hour cleaner reminder sent for dorm %s, week %s", dorm_id, week_info.week_start)
    return True
# ---------------------------------------------------------------------------
# Friday Deadline Helpers & Job (20:00 Africa/Addis_Ababa)
# ---------------------------------------------------------------------------
async def send_dorm_friday_check(
    context: ContextTypes.DEFAULT_TYPE,
    dorm_id: int,
    now_date: Optional[date] = None,
    now_time: Optional[datetime] = None,
) -> bool:
    """Apply the deadline fine once if the week's photo is still unapproved."""
    if now_date is None:
        now_date = config.get_current_date()
    if now_time is None:
        now_time = config.get_current_time()

    dorm = await db.get_dorm(dorm_id)
    if not dorm or not dorm.get("is_active", 1):
        return False

    topic_id = dorm.get("topic_id")
    members = await db.get_dorm_members(dorm_id)
    pairs = await db.get_dorm_pairs(dorm_id)
    if not pairs:
        return False

    members_by_id = {m["user_id"]: m["name"] for m in members}
    week_info = get_dorm_week_info(dorm, pairs, members_by_id, now_date)
    if not week_info or week_info.week_number < 1:
        return False

    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)
    if not week_record or week_record.get("final_status") != "pending":
        return False

    deadline = datetime.combine(week_info.friday_date, config.CHECK_TIME)
    if now_time < deadline:
        return False

    outcome = await db.finalize_week_fined(
        week_info.week_start,
        dorm_id=dorm_id,
        fine_total=dorm.get("fine_amount", config.FINE_TOTAL)
    )
    if not outcome:
        return False

    cleaner_disp = " & ".join(outcome["cleaner_names"])
    fine_meme = config.get_meme_file("fined")
    group_msg = (
        f"⏰ **Cleaning deadline passed**\n\n"
        f"No approved photo was received for **{cleaner_disp}** by Friday 8:00 PM.\n"
        f"Fine: {outcome['fine_each']} ETB each "
        f"({outcome['fine_total']} ETB total). Next week's rotation continues as scheduled."
    )
    await post_group_announcement_safe(context, dorm_id, topic_id, group_msg, fine_meme)

    dm_text = (
        "The cleaning week closed without an approved photo by Friday 8:00 PM.\n"
        f"A fine of **{outcome['fine_each']} ETB** was recorded for you. "
        "The next week's rotation continues as scheduled."
    )
    for cleaner_id, cleaner_name in zip(outcome["cleaner_ids"], outcome["cleaner_names"]):
        await send_dm_with_fallback(
            context,
            cleaner_id,
            cleaner_name,
            dm_text,
            photo_path=fine_meme,
            dorm_id=dorm_id,
            topic_id=topic_id
        )

    logger.info("Unapproved cleaning fined for dorm %s, week %s", dorm_id, week_info.week_start)
    return True


async def friday_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close each active dorm's unapproved week at the Friday deadline."""
    dorms = await db.get_all_active_dorms()
    if not dorms and getattr(config, "GROUP_ID", 0):
        await db.seed_default_dorm()
        dorms = await db.get_all_active_dorms()

    now_date = config.get_current_date()
    now_time = config.get_current_time()

    for dorm in dorms:
        dorm_id = dorm["dorm_id"]
        try:
            await send_dorm_friday_check(context, dorm_id, now_date, now_time)
        except Exception as e:
            logger.error("Error in Friday check for dorm %s: %s", dorm_id, e)

# ---------------------------------------------------------------------------
# Background Monitor & Fallback Job (Runs every 60 seconds)
# ---------------------------------------------------------------------------
async def monitor_and_fallback_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Heartbeat monitor across all active dorms:
    1. Triggers a missed Monday announcement.
    2. Sends daytime reminders every six hours until photo submission.
    3. Closes unapproved cleaning at Friday 8:00 PM.
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
                    await send_dorm_announcement(context, dorm_id)
                    week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)

            if not week_record:
                continue

            # 2. Send reminders every six hours during daytime until a photo is submitted.
            if week_record and week_record.get("final_status") == "pending":
                await send_dorm_cleaner_reminder(context, dorm_id, today, now)
                week_record = await db.get_week(week_info.week_start, dorm_id=dorm_id)

            # 3. Close unapproved cleaning at the Friday deadline
            check_naive_time = config.CHECK_TIME.replace(tzinfo=None)
            check_target = datetime.combine(week_info.friday_date, check_naive_time).replace(tzinfo=config.TIMEZONE)

            if now >= check_target and week_record.get("final_status") == "pending":
                logger.info("Friday deadline reached for dorm %s, week %s. Closing unapproved duty...", dorm_id, week_info.week_start)
                await send_dorm_friday_check(context, dorm_id, today, now)
        except Exception as e:
            logger.error("Error in monitor_and_fallback_job for dorm %s: %s", dorm_id, e)

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

    # 2. Friday deadline
    check_day_ptb = config.DAY_MAP_PTB.get(config.CHECK_DAY, 5)
    job_queue.run_daily(
        callback=friday_check_job,
        time=config.CHECK_TIME,
        days=(check_day_ptb,),
        name="friday_deadline"
    )

    # 3. Background monitor, six-hour reminders, and missed deadline fallback.
    job_queue.run_repeating(
        callback=monitor_and_fallback_job,
        interval=60,
        first=5,
        name="monitor_and_fallback"
    )

    logger.info("Multi-tenant scheduler initialized successfully.")
