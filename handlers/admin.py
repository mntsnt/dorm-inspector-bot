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
            "Only the admin can use this command."
        )
        return

    # Check argument
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/markpaid <name_or_id>`\nExample: `/markpaid Bilisuma`",
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
            f"Member '{target_input}' not found.\nMembers: {member_list}"
        )
        return

    # Check unpaid total
    unpaid_total = await db.get_user_unpaid_total(target_member.user_id)
    if unpaid_total == 0:
        await update.effective_message.reply_text(
            f"{target_member.name} has no unpaid fines."
        )
        return

    # Mark fines as paid in ledger
    count = await db.mark_fines_paid(target_member.user_id, admin_id=user.id)

    reply_msg = f"Cleared {unpaid_total} ETB in fines for **{target_member.name}**."
    await update.effective_message.reply_text(reply_msg, parse_mode=ParseMode.MARKDOWN)

    # Post notification in group chat (with paid meme if present)
    try:
        group_notice = f"Admin marked {unpaid_total} ETB fine as paid for **{target_member.name}**."
        paid_meme = config.get_meme_file("paid")
        if paid_meme:
            with open(paid_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=config.GROUP_ID,
                    photo=photo,
                    caption=group_notice,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=config.GROUP_ID,
                text=group_notice,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error("Failed to post fine payment notice in group chat: %s", e)

async def setid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to update a member's user_id."""
    user = update.effective_user
    if not user or user.id != config.ADMIN_ID:
        await update.effective_message.reply_text("Only the admin can use this command.")
        return

    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: `/setid <name> <new_user_id>`\nExample: `/setid Bilisuma 123456789`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    name_input = context.args[0].strip().lstrip("@")
    id_input = context.args[1].strip()

    if not id_input.isdigit():
        await update.effective_message.reply_text("The user ID must be a number.")
        return

    new_id = int(id_input)
    target_member = config.get_member_by_name(name_input)
    if not target_member:
        member_list = ", ".join(m.name for m in config.MEMBERS)
        await update.effective_message.reply_text(
            f"Member '{name_input}' not found.\nMembers: {member_list}"
        )
        return

    old_id = target_member.user_id
    success = config.update_member_id(target_member.name, new_id)
    if success:
        await db.update_member_id_in_db(old_id=old_id, new_id=new_id)
        await update.effective_message.reply_text(
            f"Updated **{target_member.name}**'s ID to `{new_id}` (was `{old_id}`).",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.effective_message.reply_text("Failed to update ID in members.json.")

async def testgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to verify group connectivity and permissions."""
    user = update.effective_user
    if not user or user.id != config.ADMIN_ID:
        await update.effective_message.reply_text("Only the admin can use this command.")
        return

    if not config.GROUP_ID:
        await update.effective_message.reply_text("GROUP_ID is not configured in environment variables.")
        return

    try:
        chat = await context.bot.get_chat(config.GROUP_ID)
        chat_title = chat.title or "Dorm Group"
        await context.bot.send_message(
            chat_id=config.GROUP_ID,
            text=f"Group verification successful! Bot is connected to {chat_title}."
        )
        await update.effective_message.reply_text(
            f"Successfully connected to group **{chat_title}** (ID: `{config.GROUP_ID}`).\n"
            "A test verification message was sent to the group chat.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await update.effective_message.reply_text(
            f"Failed to connect to group (ID: `{config.GROUP_ID}`):\n`{e}`\n\n"
            "Troubleshooting:\n"
            "1. Make sure the bot is added as a member in the dorm group.\n"
            "2. Make sure the bot has permission to send messages (or make it an Admin).\n"
            "3. Verify that GROUP_ID in your cloud environment matches this group's ID.",
            parse_mode=ParseMode.MARKDOWN
        )

def register_admin_handlers(application: Application) -> None:
    """Attach admin command handlers."""
    application.add_handler(CommandHandler("markpaid", markpaid_command))
    application.add_handler(CommandHandler("setid", setid_command))
    application.add_handler(CommandHandler("testgroup", testgroup_command))
