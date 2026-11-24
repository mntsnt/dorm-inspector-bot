"""
Universal Onboarding & Dorm Setup Handler
Enables any student to add the bot to their dorm group, run /setup,
invite roommates with a one-tap deep-link, auto-generate pairs, and launch their cleaning rotation.
"""

from datetime import date, timedelta
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db

logger = logging.getLogger(__name__)

def _get_next_or_current_monday(d: Optional[date] = None) -> date:
    """Return the upcoming Monday (or today if today is Monday)."""
    if d is None:
        d = config.get_current_date()
    weekday = d.weekday()
    if weekday == 0:
        return d
    return d + timedelta(days=(7 - weekday))

# ---------------------------------------------------------------------------
# Bot Added to Group Greeting Handler
# ---------------------------------------------------------------------------
async def new_chat_members_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Greet when bot is added to a new Telegram group."""
    msg = update.effective_message
    if not msg or not msg.new_chat_members:
        return

    bot_info = await context.bot.get_me()
    bot_added = any(m.id == bot_info.id for m in msg.new_chat_members)
    if not bot_added:
        return

    chat = update.effective_chat
    thread_id = msg.message_thread_id

    if chat.id == getattr(config, "DEFAULT_GROUP_ID", -1002378440774):
        greeting = (
            "👋 **Hi everyone! I am Dorm Inspector Bot.**\n\n"
            "Dorm cleaning schedule is active and running for this dorm!\n\n"
            "Use /status to view duty, or /schedule to see upcoming pairs."
        )
        await context.bot.send_message(
            chat_id=chat.id,
            message_thread_id=thread_id or getattr(config, "DEFAULT_TOPIC_ID", 2031),
            text=greeting,
            parse_mode=ParseMode.MARKDOWN
        )
        return

    greeting = (
        "👋 **Hi everyone! I am Dorm Inspector Bot.**\n\n"
        "I automate weekly cleaning schedules, send reminders, collect photo proofs with memes, and track fines.\n\n"
        "To set up this room, the room admin can tap /setup here."
    )
    await context.bot.send_message(
        chat_id=chat.id,
        message_thread_id=thread_id,
        text=greeting,
        parse_mode=ParseMode.MARKDOWN
    )

# ---------------------------------------------------------------------------
# /setup Command
# ---------------------------------------------------------------------------
async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initialize a dorm setup in a group or topic."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message

    if not chat or not user or not msg:
        return

    # Must be run in a group or supergroup
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text(
            "Please run `/setup` inside your dorm's Telegram group chat.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    dorm_id = chat.id
    topic_id = msg.message_thread_id
    dorm_name = chat.title or "Our Dorm"

    # Default next Monday as starting date
    start_monday = _get_next_or_current_monday()
    start_monday_str = start_monday.strftime("%Y-%m-%d")

    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    # The default saved dorm is always active and protected against resetting
    if dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0):
        dorm = await db.seed_default_dorm_for_group(dorm_id)
        pairs = await db.get_dorm_pairs(dorm_id)
        members = await db.get_dorm_members(dorm_id)
        members_by_id = {m["user_id"]: m["name"] for m in members}

        pair_lines = []
        for p in pairs:
            c1_id = p["cleaner1_id"]
            c2_id = p["cleaner2_id"]
            c1_name = members_by_id.get(c1_id, f"ID {c1_id}")
            c2_name = members_by_id.get(c2_id, f"ID {c2_id}")
            pair_lines.append(f"• Week {p['pair_number']}: **{c1_name}** & **{c2_name}**")
        pairs_str = "\n".join(pair_lines)

        topic_display = dorm.get("topic_id") or getattr(config, "TOPIC_ID", 2031)
        await msg.reply_text(
            f"🧼 **{dorm['dorm_name']}** is already set up and active!\n\n"
            f"All 8 roommates and pairs are configured:\n{pairs_str}\n\n"
            f"• Default Fine: 100 ETB total (50 ETB each)\n"
            f"• Forum Topic: Dorm Cleaning Schedule ({topic_display})\n\n"
            "Use /status to view duty, or /schedule to see upcoming weeks.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Check single-dorm membership for the admin running setup
    user_dorms = await db.get_user_dorms(user.id)
    if user_dorms:
        existing_other = [d for d in user_dorms if d["dorm_id"] != dorm_id]
        if existing_other:
            await msg.reply_text(
                f"⚠️ **Cannot set up a new dorm.**\n\n"
                f"You are already a member of **{existing_other[0]['dorm_name']}**.\n\n"
                "A student can only belong to one dorm at a time. "
                "If you are moving rooms, please leave your current dorm first with `/leave`.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

    # Create or update dorm record in database
    existing_dorm = await db.get_dorm(dorm_id)
    if existing_dorm and existing_dorm.get("status") == "active":
        await msg.reply_text(
            f"**{existing_dorm['dorm_name']}** is already set up and active!\n\n"
            "Use /status to view duty, or /schedule to see upcoming pairs.\n"
            "To reset, the admin can send `/resetdorm`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    is_new_dorm = existing_dorm is None
    if existing_dorm:
        if existing_dorm.get("status") != "setup":
            await msg.reply_text(
                "This dorm is not in setup mode. Ask the dorm admin to reset it before reconfiguring."
            )
            return
        if user.id != existing_dorm.get("admin_id"):
            await msg.reply_text("Only the dorm admin who started setup can continue onboarding.")
            return
        dorm = existing_dorm
        admin_id = existing_dorm["admin_id"]
        dorm_name = existing_dorm["dorm_name"]
        topic_id = existing_dorm.get("topic_id")
    else:
        admin_id = user.id
        dorm = await db.create_or_update_dorm(
            dorm_id=dorm_id,
            admin_id=admin_id,
            dorm_name=dorm_name,
            topic_id=topic_id,
            fine_amount=100,
            start_date=start_monday_str,
            status="setup"
        )

    # Automatically register the admin as member #1
    if not await db.get_dorm_member(dorm_id, admin_id):
        success, err_msg = await db.add_dorm_member(
            dorm_id=dorm_id,
            user_id=admin_id,
            name=user.first_name,
            username=user.username,
            is_admin=1
        )
        if not success:
            await msg.reply_text(f"⚠️ {err_msg}")
            return

    # Build shareable join link
    bot_info = await context.bot.get_me()
    bot_username = bot_info.username or "DormInspectorBot"
    param_id = str(abs(dorm_id))
    join_url = f"https://t.me/{bot_username}?start=join_{param_id}"
    connected_count, member_count, _, _ = await db.get_connection_status(dorm_id)
    expected_members = dorm.get("expected_members")
    roster_count = f"{connected_count}/{expected_members}" if expected_members else "not set"
    team_size = dorm.get("cleaners_per_week", 2)
    fine_amount = dorm.get("fine_amount", 100)

    setup_text = (
        f"🧼 **Dorm Inspector Setup: {dorm_name}**\n\n"
        f"Admin: **{user.first_name}** (Send `/name <your_name>` to adjust display name)\n"
        f"Cleaners: **{team_size} member{'s' if team_size > 1 else ''} per week** (Select below to adjust)\n"
        f"Fine: **{fine_amount} ETB total** ({fine_amount // team_size} ETB each)\n\n"
        f"Connected roommates: **{roster_count}**\n"
        "Set the expected roommate count, then invite everyone (including yourself) to connect before launching:"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tap to Join Dorm", url=join_url)],
        [InlineKeyboardButton(
            f"👥 Expected roommates: {expected_members or 'Set count'}",
            callback_data=f"setup_roster_{dorm_id}"
        )],
        [
            InlineKeyboardButton(f"👥 Members ({member_count})", callback_data=f"setup_members_{dorm_id}"),
            InlineKeyboardButton("⚙️ Settings", callback_data=f"setup_settings_{dorm_id}")
        ],
        [
            InlineKeyboardButton(
                f"{'✅ ' if team_size == 1 else ''}1 Cleaner",
                callback_data=f"setteam_{dorm_id}_1"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if team_size == 2 else ''}2 Cleaners",
                callback_data=f"setteam_{dorm_id}_2"
            ),
            InlineKeyboardButton(
                f"{'✅ ' if team_size == 3 else ''}3 Cleaners",
                callback_data=f"setteam_{dorm_id}_3"
            ),
        ],
        [InlineKeyboardButton("🚀 Launch when roster is ready", callback_data=f"setup_launch_{dorm_id}")]
    ])

    await msg.reply_text(setup_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

    # Prompt admin in DM to confirm/set their preferred name (only for new dorms)
    if is_new_dorm and not (
        dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)
        or config.is_member(admin_id)
    ):
        try:
            admin_dm = (
                f"👤 **Dorm Admin Setup: {dorm_name}**\n\n"
                f"What is your name?\n"
                f"Currently registered as: **{user.first_name}**\n\n"
                "Please reply with your real name (or send `/name <your_name>`) so your roommates can recognize you on the chore schedule."
            )
            await context.bot.send_message(
                chat_id=admin_id,
                text=admin_dm,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

async def _save_expected_members(dorm_id: int, user_id: int, count: int) -> tuple[bool, str]:
    dorm = await db.get_dorm(dorm_id)
    if not dorm:
        return False, "Dorm not found."
    if user_id != dorm.get("admin_id"):
        return False, "Only the dorm admin can set the roster size."
    if dorm.get("status") != "setup":
        return False, "The roster can only be changed before the schedule launches."
    if not 1 <= count <= 100:
        return False, "Choose a roommate count from 1 to 100."

    if not await db.set_dorm_expected_members(dorm_id, count):
        return False, "The setup changed. Refresh the setup card and try again."
    connected_count, member_count, _, _ = await db.get_connection_status(dorm_id)
    return True, (
        f"Expected roster set to {count}. "
        f"{member_count} registered; {connected_count}/{count} connected. "
        "Launch is enabled only after everyone has connected."
    )

async def set_expected_members_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the total expected roommate count for a dorm in setup."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("Use `/setmembers <total>` in your dorm group during setup.")
        return
    if not context.args or len(context.args) != 1:
        await msg.reply_text(
            "Usage: `/setmembers <total>`\nExample: `/setmembers 8` (include the admin)",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        count = int(context.args[0])
    except ValueError:
        await msg.reply_text("Roommate count must be a whole number from 1 to 100.")
        return

    success, message = await _save_expected_members(chat.id, user.id, count)
    await msg.reply_text(message, parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Setup Inline Callbacks (Members List, Settings, Launch)
# ---------------------------------------------------------------------------
async def setup_roster_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        dorm_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid dorm reference.", show_alert=True)
        return

    dorm = await db.get_dorm(dorm_id)
    if not dorm or query.from_user.id != dorm.get("admin_id"):
        await query.answer("Only the dorm admin can set the roster size.", show_alert=True)
        return
    if dorm.get("status") != "setup":
        await query.answer("The roster is locked after launch.", show_alert=True)
        return

    buttons = [
        InlineKeyboardButton(
            str(count),
            callback_data=f"expected_{dorm_id}_{count}"
        )
        for count in range(1, 13)
    ]
    rows = [buttons[index:index + 4] for index in range(0, len(buttons), 4)]
    rows.append([InlineKeyboardButton("« Back", callback_data=f"setup_back_{dorm_id}")])
    await query.answer()
    await query.edit_message_text(
        "How many roommates are in this dorm, including you? "
        "Choose 1–12, or use `/setmembers <total>` for a larger roster.",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN
    )

async def set_expected_members_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    parts = query.data.split("_")
    try:
        dorm_id = int(parts[1])
        count = int(parts[2])
    except (ValueError, IndexError):
        await query.answer("Invalid roster selection.", show_alert=True)
        return

    success, message = await _save_expected_members(dorm_id, query.from_user.id, count)
    await query.answer(message, show_alert=not success)
    if success:
        await _render_setup_card(query, context, dorm_id)

async def setup_members_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the list of members who have joined so far."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user

    try:
        dorm_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid dorm reference.", show_alert=True)
        return

    member = await db.get_dorm_member(dorm_id, user.id)
    if not member:
        await query.answer("Only members of this dorm can view this list.", show_alert=True)
        return

    members = await db.get_dorm_members(dorm_id)
    dorm = await db.get_dorm(dorm_id)
    dorm_name = dorm["dorm_name"] if dorm else "Dorm"

    expected_members = dorm.get("expected_members") if dorm else None
    connected_count, _, _, _ = await db.get_connection_status(dorm_id)
    roster_summary = f"{connected_count}/{expected_members} connected" if expected_members else "expected count not set"
    lines = [f"👥 **{dorm_name} Members ({len(members)}): {roster_summary}**\n"]
    for idx, m in enumerate(members, start=1):
        adm_tag = " (Admin)" if m.get("is_admin") else ""
        lines.append(f"{idx}. **{m['name']}**{adm_tag}")

    lines.append("\nNeed more roommates? Share the join button above!")
    await query.answer("\n".join(lines), show_alert=True)

async def setup_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow admin to toggle settings like fine amounts."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user

    parts = query.data.split("_")
    dorm_id = int(parts[2])

    dorm = await db.get_dorm(dorm_id)
    if not dorm:
        await query.answer("Dorm not found.", show_alert=True)
        return

    if user.id != dorm["admin_id"]:
        await query.answer("Only the dorm admin can change settings.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    current_fine = dorm.get("fine_amount", 100)
    current_team = dorm.get("cleaners_per_week", 2)
    expected_members = dorm.get("expected_members") or "Not set"
    settings_text = (
        f"⚙️ **Settings for {dorm['dorm_name']}:**\n\n"
        f"• Cleaning Team Size: **{current_team} cleaner(s) per week**\n"
        f"• Fine Amount: **{current_fine} ETB total** ({current_fine // current_team} ETB each cleaner)\n\n"
        f"• Expected Roommates: **{expected_members}**\n"
        "Use `/setmembers <total>` to set the roster size (include the admin).\n\n"
        "Choose an option below to adjust:"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 1 Cleaner/Wk", callback_data=f"setteam_{dorm_id}_1"),
            InlineKeyboardButton("👥 2 Cleaners/Wk", callback_data=f"setteam_{dorm_id}_2"),
            InlineKeyboardButton("👥+ 3 Cleaners/Wk", callback_data=f"setteam_{dorm_id}_3"),
        ],
        [
            InlineKeyboardButton("50 ETB Fine", callback_data=f"setfine_{dorm_id}_50"),
            InlineKeyboardButton("100 ETB Fine", callback_data=f"setfine_{dorm_id}_100"),
            InlineKeyboardButton("200 ETB Fine", callback_data=f"setfine_{dorm_id}_200"),
        ],
        [
            InlineKeyboardButton("No Fines (0 ETB)", callback_data=f"setfine_{dorm_id}_0"),
        ],
        [InlineKeyboardButton("« Back to Setup", callback_data=f"setup_back_{dorm_id}")]
    ])

    try:
        await query.edit_message_text(settings_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        pass
    except Exception as e:
        logger.warning("Could not edit settings message: %s", e)

async def setfine_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update fine amount."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user

    parts = query.data.split("_")
    dorm_id = int(parts[1])
    new_fine = int(parts[2])

    dorm = await db.get_dorm(dorm_id)
    if not dorm or user.id != dorm["admin_id"]:
        await query.answer("Only the dorm admin can adjust fines.", show_alert=True)
        return

    await db.update_dorm_settings(dorm_id, fine_amount=new_fine)
    try:
        await query.answer(f"Fine updated to {new_fine} ETB total!", show_alert=True)
    except Exception:
        pass

    # Return to setup card
    await _render_setup_card(query, context, dorm_id)

async def setteam_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Update team size (cleaners per week: 1, 2, or 3)."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user

    parts = query.data.split("_")
    dorm_id = int(parts[1])
    new_team_size = int(parts[2])

    dorm = await db.get_dorm(dorm_id)
    if not dorm or user.id != dorm["admin_id"]:
        await query.answer("Only the dorm admin can adjust cleaners per week.", show_alert=True)
        return
    if dorm.get("status") != "setup":
        await query.answer("Cleaner team size is locked after launch.", show_alert=True)
        return

    if new_team_size not in (1, 2, 3):
        new_team_size = 2

    if not await db.set_dorm_team_size(dorm_id, new_team_size):
        await query.answer("The schedule has already launched.", show_alert=True)
        return
    try:
        await query.answer(f"Cleaning team set to {new_team_size} cleaner(s) per week!", show_alert=True)
    except Exception:
        pass

    # Return to setup card
    await _render_setup_card(query, context, dorm_id)

async def setup_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to main setup card."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except Exception:
        pass

    parts = query.data.split("_")
    dorm_id = int(parts[2])
    await _render_setup_card(query, context, dorm_id)

async def _render_setup_card(query, context, dorm_id: int) -> None:
    """Re-render the main setup card."""
    dorm = await db.get_dorm(dorm_id)
    if not dorm:
        return

    admin = await db.get_dorm_member(dorm_id, dorm["admin_id"])
    admin_name = admin["name"] if admin else "Admin"

    bot_info = await context.bot.get_me()
    bot_username = bot_info.username or "DormInspectorBot"
    param_id = str(abs(dorm_id))
    join_url = f"https://t.me/{bot_username}?start=join_{param_id}"

    current_fine = dorm.get("fine_amount", 100)
    team_size = dorm.get("cleaners_per_week", 2)
    fine_each = current_fine // team_size
    expected_members = dorm.get("expected_members")
    connected_count, member_count, _, _ = await db.get_connection_status(dorm_id)
    roster_count = f"{connected_count}/{expected_members}" if expected_members else "not set"

    setup_text = (
        f"🧼 **Dorm Inspector Setup: {dorm['dorm_name']}**\n\n"
        f"Admin: **{admin_name}**\n"
        f"Cleaners: **{team_size} member{'s' if team_size > 1 else ''} per week**\n"
        f"Fine: **{current_fine} ETB total** ({fine_each} ETB each cleaner)\n"
        f"Connected roommates: **{roster_count}**\n"
        "Set the expected total (including the admin), then invite everyone to connect before launching:"
    )

    b1_label = "✅ 1 Cleaner" if team_size == 1 else "1 Cleaner"
    b2_label = "✅ 2 Cleaners" if team_size == 2 else "2 Cleaners"
    b3_label = "✅ 3 Cleaners" if team_size == 3 else "3 Cleaners"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Tap to Join Dorm", url=join_url)],
        [InlineKeyboardButton(
            f"👥 Expected roommates: {expected_members or 'Set count'}",
            callback_data=f"setup_roster_{dorm_id}"
        )],
        [
            InlineKeyboardButton(f"👥 Members ({member_count})", callback_data=f"setup_members_{dorm_id}"),
            InlineKeyboardButton("⚙️ Settings", callback_data=f"setup_settings_{dorm_id}")
        ],
        [
            InlineKeyboardButton(b1_label, callback_data=f"setteam_{dorm_id}_1"),
            InlineKeyboardButton(b2_label, callback_data=f"setteam_{dorm_id}_2"),
            InlineKeyboardButton(b3_label, callback_data=f"setteam_{dorm_id}_3"),
        ],
        [InlineKeyboardButton("🚀 Launch when roster is ready", callback_data=f"setup_launch_{dorm_id}")]
    ])

    try:
        await query.edit_message_text(setup_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    except BadRequest:
        pass
    except Exception as e:
        logger.warning("Could not render setup card: %s", e)

# ---------------------------------------------------------------------------
# Launch & Auto-Pair Handler
# ---------------------------------------------------------------------------
async def setup_launch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-pair members and officially activate the dorm schedule."""
    query = update.callback_query
    if not query:
        return
    user = query.from_user

    try:
        dorm_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("Invalid dorm reference.", show_alert=True)
        return

    dorm = await db.get_dorm(dorm_id)
    if not dorm:
        await query.answer("Dorm record not found.", show_alert=True)
        return
    if dorm.get("status") != "setup":
        await query.answer("This dorm's schedule has already launched.", show_alert=True)
        return

    # Check permission
    if user.id != dorm["admin_id"]:
        await query.answer("Only the dorm admin can launch the schedule.", show_alert=True)
        return

    members = await db.get_dorm_members(dorm_id)
    team_size = dorm.get("cleaners_per_week", 2)
    expected_members = dorm.get("expected_members")
    if not expected_members:
        await query.answer(
            "Set the expected roommate count first with the roster button or /setmembers <total>.",
            show_alert=True
        )
        return
    if expected_members < team_size:
        await query.answer(
            f"The roster has {expected_members} people but the team size is {team_size}. "
            "Reduce the cleaners per week first.",
            show_alert=True
        )
        return
    connected_count, member_count, _, _ = await db.get_connection_status(dorm_id)
    if member_count != expected_members:
        await query.answer(
            f"Roster is {member_count}/{expected_members}. Invite everyone or correct the expected count.",
            show_alert=True
        )
        return
    if connected_count != expected_members:
        await query.answer(
            f"Only {connected_count}/{expected_members} roommates have opened the bot. "
            "Everyone must join using the private invite link before launch.",
            show_alert=True
        )
        return

    # Generate teams automatically
    pairs = await db.auto_generate_dorm_pairs(dorm_id)
    assigned_ids = [
        user_id
        for team in pairs
        for user_id in team
        if user_id
    ]
    if len(assigned_ids) != expected_members or len(set(assigned_ids)) != expected_members:
        logger.error(
            "Generated an invalid rotation for dorm %s: %d assignments for %d members",
            dorm_id, len(assigned_ids), expected_members
        )
        await query.answer("Could not build a complete rotation. Check the member list and try again.", show_alert=True)
        return

    # Set start date to upcoming Monday
    start_monday = _get_next_or_current_monday()
    start_str = start_monday.strftime("%Y-%m-%d")

    # Atomically activate only if the expected, connected roster is still complete.
    if not await db.activate_dorm_if_ready(
        dorm_id, start_str, team_size, expected_members
    ):
        await query.answer(
            "The roster changed or another admin already launched the schedule. Refresh setup and try again.",
            show_alert=True
        )
        return
    try:
        await query.answer("Schedule launched.")
    except TelegramError:
        pass

    # Build launch announcement text
    pair_lines = []
    members_by_id = {m["user_id"]: m["name"] for m in members}
    for idx, team in enumerate(pairs, start=1):
        cleaner_uids = [uid for uid in team if uid]
        cnames = [members_by_id.get(uid, f"ID {uid}") for uid in cleaner_uids]
        if len(cnames) == 1:
            pair_lines.append(f"• **Week {idx}:** {cnames[0]}")
        elif len(cnames) == 2:
            pair_lines.append(f"• **Week {idx}:** {cnames[0]} & {cnames[1]}")
        else:
            pair_lines.append(f"• **Week {idx}:** {', '.join(cnames[:-1])} & {cnames[-1]}")

    code = dorm.get("dorm_code") or "DORM-101"
    dash_base = getattr(config, "DASHBOARD_URL", "https://inspector.app.aletcloud.com").rstrip("/")
    dash_url = f"{dash_base}/?code={code}"

    launch_msg = (
        f"🎉 **{dorm['dorm_name']} is now ACTIVE!**\n\n"
        f"🔑 Dorm Code: `{code}`\n"
        f"🌐 Live Website: [Open Dashboard]({dash_url})\n\n"
        f"Total Roommates: **{len(members)}**\n"
        f"Cleaners: **{team_size} per week**\n"
        f"Rotation starts: **Monday, {start_monday.strftime('%b %d')}**\n\n"
        "**Cleaning Schedule:**\n" + "\n".join(pair_lines) + "\n\n"
        "Duty will be announced automatically every Monday at 08:00 AM.\n"
        "Use /status anytime to check the schedule!"
    )

    welcome_meme = config.get_meme_file("welcome")
    topic_id = dorm.get("topic_id")

    try:
        if welcome_meme:
            with open(welcome_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=dorm_id,
                    message_thread_id=topic_id,
                    photo=photo,
                    caption=launch_msg,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=dorm_id,
                message_thread_id=topic_id,
                text=launch_msg,
                parse_mode=ParseMode.MARKDOWN
            )
        # Edit the setup message to show launched
        try:
            await query.edit_message_text("✅ Setup complete! Cleaning schedule has launched in the group.")
        except (BadRequest, TelegramError):
            pass
    except Exception as e:
        logger.error("Failed to post launch message for dorm %s: %s", dorm_id, e)
        try:
            await query.edit_message_text(launch_msg, parse_mode=ParseMode.MARKDOWN)
        except (BadRequest, TelegramError):
            pass

# ---------------------------------------------------------------------------
# Reset Dorm Command
# ---------------------------------------------------------------------------
async def reset_dorm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Allow admin to reset or reconfigure their dorm."""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user or chat.type not in ("group", "supergroup"):
        return

    if chat.id == getattr(config, "DEFAULT_GROUP_ID", -1002378440774):
        await update.effective_message.reply_text("The default dorm schedule and pairs are permanent and cannot be reset.")
        return

    dorm = await db.get_dorm(chat.id)
    if not dorm:
        await update.effective_message.reply_text("This group has not been set up yet. Run /setup to get started.")
        return

    if user.id != dorm["admin_id"]:
        await update.effective_message.reply_text("Only the dorm admin can reset this room.")
        return

    await db.update_dorm_status(chat.id, "setup")
    await update.effective_message.reply_text(
        "Dorm schedule paused and reset to setup mode. Run /setup anytime to reconfigure pairs or add new roommates."
    )

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_setup_handlers(application: Application) -> None:
    """Register all universal setup and onboarding handlers."""
    application.add_handler(CommandHandler("setup", setup_command))
    application.add_handler(CommandHandler("setmembers", set_expected_members_command))
    application.add_handler(CommandHandler("resetdorm", reset_dorm_command))
    application.add_handler(CallbackQueryHandler(setup_roster_callback, pattern=r"^setup_roster_"))
    application.add_handler(CallbackQueryHandler(set_expected_members_callback, pattern=r"^expected_"))
    application.add_handler(CallbackQueryHandler(setup_members_callback, pattern=r"^setup_members_"))
    application.add_handler(CallbackQueryHandler(setup_settings_callback, pattern=r"^setup_settings_"))
    application.add_handler(CallbackQueryHandler(setfine_callback, pattern=r"^setfine_"))
    application.add_handler(CallbackQueryHandler(setteam_callback, pattern=r"^setteam_"))
    application.add_handler(CallbackQueryHandler(setup_back_callback, pattern=r"^setup_back_"))
    application.add_handler(CallbackQueryHandler(setup_launch_callback, pattern=r"^setup_launch_"))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_chat_members_handler))
