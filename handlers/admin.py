"""
Admin Commands Handler
Handles /markpaid <member> to settle fines in the ledger.
Strictly restricted to ADMIN_ID.
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import db

logger = logging.getLogger(__name__)

async def markpaid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to clear unpaid fines for a member."""
    user = update.effective_user
    if not user:
        return

    # Security check: Only ADMIN_ID can execute
    if user.id != config.ADMIN_ID:
        await update.effective_message.reply_text(
            "🚫 Unauthorized. This command is restricted to the bot administrator."
        )
        return

    # Check argument
    if not context.args:
        await update.effective_message.reply_text(
            "⚠️ **Usage:** `/markpaid <member_name_or_user_id>`\n"
            "Example: `/markpaid Bilisuma` or `/markpaid 111111111`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    target_input = " ".join(context.args).strip().lstrip("@")

    # Resolve member by user_id or name
    target_member = None
    if target_input.isdigit():
        target_member = config.get_member(int(target_input))
    
    if not target_member:
        target_member = config.get_member_by_name(target_input)

    if not target_member:
        member_list = ", ".join(m.name for m in config.MEMBERS)
        await update.effective_message.reply_text(
            f"❌ Member '{target_input}' not found.\nValid members: {member_list}"
        )
        return

    # Check unpaid total
    unpaid_total = await db.get_user_unpaid_total(target_member.user_id)
    if unpaid_total == 0:
        await update.effective_message.reply_text(
            f"ℹ️ **{target_member.name}** currently has no unpaid fines."
        )
        return

    # Mark fines as paid in ledger
    count = await db.mark_fines_paid(target_member.user_id, admin_id=user.id)

    reply_msg = (
        f"✅ **Fines Cleared!**\n"
        f"Successfully marked {count} unpaid record(s) totaling **{unpaid_total} ETB** as PAID for **{target_member.name}**."
    )
    await update.effective_message.reply_text(reply_msg, parse_mode=ParseMode.MARKDOWN)

    # Post notification in group chat
    try:
        group_notice = (
            f"🧾 **Fine Settlement:**\n"
            f"Admin has marked **{unpaid_total} ETB** of fines as **PAID** for **{target_member.name}**."
        )
        await context.bot.send_message(
            chat_id=config.GROUP_ID,
            text=group_notice,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error("Failed to post fine payment notice in group chat: %s", e)

def register_admin_handlers(application: Application) -> None:
    """Attach admin command handlers."""
    application.add_handler(CommandHandler("markpaid", markpaid_command))
