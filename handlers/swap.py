"""
Peer Duty Swap Handler (Temporary One-Week Overrides)
Allows a cleaner with an exam or emergency to swap duty with another roommate for one week only.
Normal rotation pairings in dorm_pairs are preserved for subsequent cycles.
"""

import logging
from typing import Optional
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config
import db
from scheduler import get_dorm_week_info

logger = logging.getLogger(__name__)

async def swap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiate a peer duty swap request for this week only."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm = None
    if is_group:
        dorm_id = chat.id
        dorm = await db.get_dorm(dorm_id)
        if dorm:
            member = await db.get_dorm_member(dorm_id, user.id)
            if not member and user.id != getattr(config, "ADMIN_ID", 6163528558):
                await message.reply_text("You are not a member of this dorm.")
                return
    else:
        user_dorms = await db.get_user_dorms(user.id)
        if user_dorms:
            dorm_id = user_dorms[0]["dorm_id"]
            dorm = await db.get_dorm(dorm_id)
        elif config.is_member(user.id) or user.id == getattr(config, "ADMIN_ID", 6163528558):
            dorm_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
            dorm = await db.get_dorm(dorm_id)

    if not dorm:
        await message.reply_text("🧼 No active dorm found. Send `/setup` to initialize.", parse_mode=ParseMode.MARKDOWN)
        return

    target_dorm_id = dorm["dorm_id"]
    pairs = await db.get_dorm_pairs(target_dorm_id)
    members = await db.get_dorm_members(target_dorm_id)
    members_by_id = {m["user_id"]: m["name"] for m in members}

    week_info = get_dorm_week_info(dorm, pairs, members_by_id)
    if not week_info or week_info.week_number < 1:
        await message.reply_text("Cleaning schedule has not started yet. Swaps can only be requested once duty is active.")
        return

    week_record = await db.get_week(week_info.week_start, dorm_id=target_dorm_id)
    if not week_record:
        # Pre-create week record
        week_record = await db.get_or_create_week(
            week_start=week_info.week_start,
            week_number=week_info.week_number,
            cleaner1_id=week_info.cleaner1_id,
            cleaner2_id=week_info.cleaner2_id,
            eligible_approver_ids=[m["user_id"] for m in members if m["user_id"] not in (week_info.cleaner1_id, week_info.cleaner2_id)],
            dorm_id=target_dorm_id
        )

    cleaner1_id = week_record["cleaner1_id"]
    cleaner2_id = week_record["cleaner2_id"]

    # Verify that the person requesting the swap is actually one of the cleaners on duty
    if user.id not in (cleaner1_id, cleaner2_id):
        c1_name = members_by_id.get(cleaner1_id, f"ID {cleaner1_id}")
        c2_name = members_by_id.get(cleaner2_id, f"ID {cleaner2_id}")
        await message.reply_text(
            f"You are not assigned to clean this week!\n"
            f"Current cleaners: **{c1_name}** & **{c2_name}**.\n"
            "Only cleaners scheduled for this week can request a duty swap.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Check argument
    if not context.args or len(context.args) < 1:
        await message.reply_text(
            "Usage: `/swap <roommate_name>`\n"
            "Example: `/swap Hassan` or `/swap with Hassan`\n\n"
            "*(Note: Swaps are temporary for this week only — your normal pairing continues next cycle!)*",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    target_input = " ".join(context.args).strip()
    if target_input.lower().startswith("with "):
        target_input = target_input[5:].strip()
    target_input = target_input.lstrip("@")

    # Resolve target member
    target_member = None
    if target_input.isdigit():
        target_member = await db.get_dorm_member(target_dorm_id, int(target_input))
    if not target_member:
        target_member = await db.get_dorm_member_by_name(target_dorm_id, target_input)

    if not target_member:
        member_names = ", ".join(m["name"] for m in members if m["user_id"] != user.id)
        await message.reply_text(
            f"Roommate '{target_input}' not found in this dorm.\n"
            f"Available roommates: {member_names}"
        )
        return

    target_id = target_member["user_id"]
    target_name = target_member["name"]
    requester_name = members_by_id.get(user.id, user.first_name)

    if target_id == user.id:
        await message.reply_text("You cannot swap duty with yourself!")
        return

    if target_id in (cleaner1_id, cleaner2_id):
        await message.reply_text(f"**{target_name}** is already your cleaning partner this week!")
        return

    # Create swap request in DB
    swap_id = await db.create_swap_request(target_dorm_id, week_info.week_start, user.id, target_id)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Accept Swap (This Week Only)", callback_data=f"swap_acc_{swap_id}"),
            InlineKeyboardButton("❌ Decline", callback_data=f"swap_dec_{swap_id}"),
        ]
    ])

    dates_str = f"{week_info.monday_date.strftime('%b %d')} – {week_info.friday_date.strftime('%b %d')}"
    swap_prompt = (
        f"🔄 **TEMPORARY DUTY SWAP REQUEST** 🔄\n\n"
        f"**{requester_name}** is requesting **{target_name}** to take over their cleaning duty for **this week only ({dates_str})**!\n\n"
        f"💡 **Exam & Schedule Rule:**\n"
        f"This swap affects **Week {week_info.week_number} only**. "
        f"Normal pairings will continue as usual in future cycles.\n\n"
        f"👉 **{target_name}**, do you accept this one-week swap?"
    )

    await message.reply_text(swap_prompt, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

async def swap_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle roommate response to a swap proposal."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = query.from_user
    data = query.data

    parts = data.split("_")
    if len(parts) < 3:
        return

    action = parts[1]  # "acc" or "dec"
    try:
        swap_id = int(parts[2])
    except ValueError:
        return

    swap_req = await db.get_swap_request(swap_id)
    if not swap_req:
        await query.answer("This swap request is no longer valid.", show_alert=True)
        return

    dorm_id = swap_req["dorm_id"]
    target_id = swap_req["target_id"]
    requester_id = swap_req["requester_id"]

    # Security check: Only the targeted roommate can click Accept or Decline
    if user.id != target_id:
        target_member = await db.get_dorm_member(dorm_id, target_id)
        t_name = target_member["name"] if target_member else f"ID {target_id}"
        await query.answer(f"Only {t_name} can respond to this swap request!", show_alert=True)
        return

    if swap_req["status"] != "pending":
        await query.answer(f"This swap was already {swap_req['status']}.", show_alert=True)
        return

    try:
        await query.answer()
    except Exception:
        pass

    target_member = await db.get_dorm_member(dorm_id, target_id)
    requester_member = await db.get_dorm_member(dorm_id, requester_id)
    t_name = target_member["name"] if target_member else user.first_name
    r_name = requester_member["name"] if requester_member else f"ID {requester_id}"

    if action == "acc":
        success, msg = await db.execute_swap(swap_id)
        if not success:
            await query.answer(msg, show_alert=True)
            return

        # Fetch updated week record
        w_record = await db.get_week(swap_req["week_start"], dorm_id=dorm_id)
        c1 = await db.get_dorm_member(dorm_id, w_record["cleaner1_id"])
        c2 = await db.get_dorm_member(dorm_id, w_record["cleaner2_id"])
        c1_name = c1["name"] if c1 else f"ID {w_record['cleaner1_id']}"
        c2_name = c2["name"] if c2 else f"ID {w_record['cleaner2_id']}"

        result_text = (
            f"🎉 **Duty Swap Confirmed for This Week Only!**\n\n"
            f"**{t_name}** accepted the swap from **{r_name}**.\n\n"
            f"👥 **Updated Cleaners for This Week:**\n"
            f"• **{c1_name}** & **{c2_name}**\n\n"
            f"_(Normal rotation pairings will resume as usual next cycle)._"
        )
        if query.message:
            try:
                await query.edit_message_text(result_text, parse_mode=ParseMode.MARKDOWN)
            except (BadRequest, TelegramError):
                pass

    elif action == "dec":
        await db.decline_swap(swap_id)
        decline_text = (
            f"❌ **Swap Declined.**\n\n"
            f"**{t_name}** was unable to swap duty this week.\n"
            f"**{r_name}** remains scheduled for cleaning duty."
        )
        if query.message:
            try:
                await query.edit_message_text(decline_text, parse_mode=ParseMode.MARKDOWN)
            except (BadRequest, TelegramError):
                pass

def register_swap_handlers(application: Application) -> None:
    """Attach duty swap command and callback handlers."""
    application.add_handler(CommandHandler("swap", swap_command))
    application.add_handler(CallbackQueryHandler(swap_callback_handler, pattern=r"^swap_(acc|dec)_"))
