"""
Admin Commands Handler (Multi-Tenant Platform)
Handles /markpaid (or /paid) <member> to settle fines in the ledger.
Strictly restricted to the dorm admin or system ADMIN_ID.
"""

import logging
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

import config
import db

logger = logging.getLogger(__name__)

async def markpaid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to clear unpaid fines for a member in a dorm."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # Determine target dorm
    target_dorm_id: Optional[int] = None
    if chat.type in ("group", "supergroup"):
        target_dorm_id = chat.id
    else:
        # Private chat: check admin dorms
        user_dorms = await db.get_user_dorms(user.id)
        admin_dorms = [d for d in user_dorms if d.get("user_is_admin") == 1 or d.get("admin_id") == user.id]
        if admin_dorms:
            target_dorm_id = admin_dorms[0]["dorm_id"]
        elif user.id == getattr(config, "ADMIN_ID", 0):
            target_dorm_id = getattr(config, "GROUP_ID", 0)

    if not target_dorm_id:
        await update.effective_message.reply_text("Could not determine which dorm to manage.")
        return

    dorm = await db.get_dorm(target_dorm_id)
    dorm_admin_id = dorm["admin_id"] if dorm else getattr(config, "ADMIN_ID", 0)

    # Security check: User must be either this dorm's admin or global ADMIN_ID
    if user.id != dorm_admin_id and user.id != getattr(config, "ADMIN_ID", 0):
        await update.effective_message.reply_text("Only the dorm admin can use this command.")
        return

    # Check argument
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: `/markpaid <name_or_id>` or `/paid <name_or_id>`\nExample: `/markpaid Bilisuma`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    target_input = " ".join(context.args).strip().lstrip("@")

    # Resolve member by user_id or name in this dorm
    target_member_id: Optional[int] = None
    target_member_name: Optional[str] = None

    if target_input.isdigit():
        uid = int(target_input)
        m = await db.get_dorm_member(target_dorm_id, uid)
        if m:
            target_member_id = m["user_id"]
            target_member_name = m["name"]
        elif target_dorm_id == getattr(config, "DEFAULT_GROUP_ID", -1002378440774) and config.get_member(uid):
            cm = config.get_member(uid)
            target_member_id = cm.user_id
            target_member_name = cm.name

    if not target_member_id:
        m = await db.get_dorm_member_by_name(target_dorm_id, target_input)
        if m:
            target_member_id = m["user_id"]
            target_member_name = m["name"]
        elif target_dorm_id == getattr(config, "DEFAULT_GROUP_ID", -1002378440774) and config.get_member_by_name(target_input):
            cm = config.get_member_by_name(target_input)
            target_member_id = cm.user_id
            target_member_name = cm.name

    if not target_member_id:
        members = await db.get_dorm_members(target_dorm_id)
        if not members and target_dorm_id == getattr(config, "DEFAULT_GROUP_ID", -1002378440774):
            member_list = ", ".join(m.name for m in config.MEMBERS)
        else:
            member_list = ", ".join(m["name"] for m in members) if members else "None"
        await update.effective_message.reply_text(
            f"Member '{target_input}' not found in this dorm.\nMembers: {member_list}"
        )
        return

    # Check unpaid total
    unpaid_total = await db.get_user_unpaid_total(target_member_id, dorm_id=target_dorm_id)
    if unpaid_total == 0:
        await update.effective_message.reply_text(
            f"{target_member_name} has no unpaid fines."
        )
        return

    # Mark fines as paid in ledger
    await db.mark_fines_paid(target_member_id, admin_id=user.id, dorm_id=target_dorm_id)

    reply_msg = f"Cleared {unpaid_total} ETB in fines for **{target_member_name}**."
    await update.effective_message.reply_text(reply_msg, parse_mode=ParseMode.MARKDOWN)

    # Post notification in group chat (with paid meme if present)
    try:
        topic_id = dorm.get("topic_id") if dorm else getattr(config, "TOPIC_ID", None)
        group_notice = f"Admin marked {unpaid_total} ETB fine as paid for **{target_member_name}**."
        paid_meme = config.get_meme_file("paid")
        if paid_meme:
            with open(paid_meme, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=target_dorm_id,
                    message_thread_id=topic_id,
                    photo=photo,
                    caption=group_notice,
                    parse_mode=ParseMode.MARKDOWN
                )
        else:
            await context.bot.send_message(
                chat_id=target_dorm_id,
                message_thread_id=topic_id,
                text=group_notice,
                parse_mode=ParseMode.MARKDOWN
            )
    except Exception as e:
        logger.error("Failed to post fine payment notice in group chat %s: %s", target_dorm_id, e)

async def setid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to update a member's user_id."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    target_dorm_id = chat.id if chat.type in ("group", "supergroup") else getattr(config, "GROUP_ID", 0)
    dorm = await db.get_dorm(target_dorm_id)
    dorm_admin_id = dorm["admin_id"] if dorm else getattr(config, "ADMIN_ID", 0)

    if user.id != dorm_admin_id and user.id != getattr(config, "ADMIN_ID", 0):
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
    member = await db.get_dorm_member_by_name(target_dorm_id, name_input)
    if not member:
        if target_dorm_id == getattr(config, "DEFAULT_GROUP_ID", -1002378440774):
            cm = config.get_member_by_name(name_input)
            if cm:
                old_id = cm.user_id
                config.update_member_id(cm.name, new_id)
                await db.update_member_id_in_db(old_id=old_id, new_id=new_id)
                await update.effective_message.reply_text(
                    f"Updated **{cm.name}**'s ID to `{new_id}` (was `{old_id}`).",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

        members = await db.get_dorm_members(target_dorm_id)
        if not members and target_dorm_id == getattr(config, "DEFAULT_GROUP_ID", -1002378440774):
            member_list = ", ".join(m.name for m in config.MEMBERS)
        else:
            member_list = ", ".join(m["name"] for m in members) if members else "None"
        await update.effective_message.reply_text(
            f"Member '{name_input}' not found in this dorm.\nMembers: {member_list}"
        )
        return

    old_id = member["user_id"]
    if target_dorm_id == getattr(config, "DEFAULT_GROUP_ID", -1002378440774):
        config.update_member_id(member["name"], new_id)
    await db.update_member_id_in_db(old_id=old_id, new_id=new_id)
    await update.effective_message.reply_text(
        f"Updated **{member['name']}**'s ID to `{new_id}` (was `{old_id}`).",
        parse_mode=ParseMode.MARKDOWN
    )

async def testgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to verify group connectivity and permissions."""
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    target_group_id: Optional[int] = None
    if chat.type in ("group", "supergroup"):
        target_group_id = chat.id
    else:
        user_dorms = await db.get_user_dorms(user.id)
        admin_dorms = [d for d in user_dorms if d.get("user_is_admin") == 1 or d.get("admin_id") == user.id]
        if admin_dorms:
            target_group_id = admin_dorms[0]["dorm_id"]
        elif user.id == getattr(config, "ADMIN_ID", 0):
            target_group_id = getattr(config, "GROUP_ID", 0)

    if not target_group_id:
        await update.effective_message.reply_text("Could not determine which dorm group to verify.")
        return

    dorm = await db.get_dorm(target_group_id)
    dorm_admin_id = dorm["admin_id"] if dorm else getattr(config, "ADMIN_ID", 0)

    if user.id != dorm_admin_id and user.id != getattr(config, "ADMIN_ID", 0):
        await update.effective_message.reply_text("Only the admin can use this command.")
        return

    try:
        tg_chat = await context.bot.get_chat(target_group_id)
        chat_title = tg_chat.title or "Dorm Group"
        topic_id = dorm.get("topic_id") if dorm else getattr(config, "TOPIC_ID", None)
        topic_info = f", Topic ID: `{topic_id}`" if topic_id is not None else ""
        await context.bot.send_message(
            chat_id=target_group_id,
            message_thread_id=topic_id,
            text=f"Group verification successful! Bot is connected to {chat_title}."
        )
        await update.effective_message.reply_text(
            f"Successfully connected to group **{chat_title}** (ID: `{target_group_id}`{topic_info}).\n"
            "A test verification message was sent to the group chat.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await update.effective_message.reply_text(
            f"Failed to connect to group (ID: `{target_group_id}`):\n`{e}`",
            parse_mode=ParseMode.MARKDOWN
        )

async def set_ai_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to configure a dedicated AI API key for their dorm."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message:
        return

    # Determine target dorm
    target_dorm_id: Optional[int] = None
    if chat.type in ("group", "supergroup"):
        target_dorm_id = chat.id
    else:
        user_dorms = await db.get_user_dorms(user.id)
        admin_dorms = [d for d in user_dorms if d.get("user_is_admin") == 1 or d.get("admin_id") == user.id]
        if admin_dorms:
            target_dorm_id = admin_dorms[0]["dorm_id"]
        elif user.id == getattr(config, "ADMIN_ID", 0):
            target_dorm_id = getattr(config, "GROUP_ID", 0)

    if not target_dorm_id:
        await message.reply_text("Could not determine which dorm to configure.")
        return

    dorm = await db.get_dorm(target_dorm_id)
    dorm_admin_id = dorm["admin_id"] if dorm else getattr(config, "ADMIN_ID", 0)

    # Permission check: User must be this dorm's admin or global ADMIN_ID
    if user.id != dorm_admin_id and user.id != getattr(config, "ADMIN_ID", 0):
        await message.reply_text("Only the dorm admin can configure the AI API key.")
        return

    if not context.args or len(context.args) < 1:
        current_key = await db.get_dorm_ai_key(target_dorm_id)
        status_str = f"Active (`...{current_key[-4:]}`)" if current_key else "Not configured"
        await message.reply_text(
            f"🔑 **Dorm AI Configuration**\n\n"
            f"Current Status: **{status_str}**\n\n"
            f"Usage: `/set_ai_key <your_api_key>`\n"
            f"To remove: `/set_ai_key remove`\n\n"
            f"💡 *Tip:* Free keys can be generated at [Google AI Studio](https://aistudio.google.com/) (Gemini) or [Groq Console](https://console.groq.com/) (Llama 3.3).",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    api_key_input = context.args[0].strip()

    # If sent in group chat, attempt to delete message to protect the secret key from roommates
    if chat.type in ("group", "supergroup"):
        try:
            await message.delete()
        except Exception:
            pass

    if api_key_input.lower() in ("remove", "delete", "clear", "none"):
        await db.set_dorm_ai_key(target_dorm_id, None)
        await chat.send_message("🗑️ AI API key removed. AI responses are now disabled for this dorm.")
        return

    if len(api_key_input) < 10:
        await chat.send_message("⚠️ That doesn't look like a valid API key. Please check and try again.")
        return

    provider_name = "OpenAI" if api_key_input.startswith("sk-") else "Google Gemini"
    await db.set_dorm_ai_key(target_dorm_id, api_key_input)

    masked_key = f"{api_key_input[:4]}...{api_key_input[-4:]}"
    d_name = dorm.get("dorm_name") if dorm else "Your dorm"
    success_text = (
        f"✅ **{provider_name} API Key Activated for {d_name}!**\n\n"
        f"Key: `{masked_key}`\n\n"
        f"Roommates can now @mention the bot or reply to bot messages to chat with the AI! 🤖✨"
    )
    await chat.send_message(success_text, parse_mode=ParseMode.MARKDOWN)

async def announce_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to manually broadcast or re-trigger the weekly duty announcement in the group."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    is_group = chat.type in ("group", "supergroup")
    if is_group:
        target_dorm_id = chat.id
    else:
        user_dorms = await db.get_user_dorms(user.id)
        admin_dorms = [d for d in user_dorms if d.get("user_is_admin") == 1 or d.get("admin_id") == user.id]
        if admin_dorms:
            target_dorm_id = admin_dorms[0]["dorm_id"]
        elif user.id == getattr(config, "ADMIN_ID", 0):
            target_dorm_id = getattr(config, "GROUP_ID", 0) or getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        else:
            target_dorm_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    dorm = await db.get_dorm(target_dorm_id)
    dorm_admin_id = dorm["admin_id"] if dorm else getattr(config, "ADMIN_ID", 0)

    # Permission check
    if user.id != dorm_admin_id and user.id != getattr(config, "ADMIN_ID", 0):
        await update.effective_message.reply_text("Only the dorm admin can trigger the announcement.")
        return

    from scheduler import send_dorm_announcement
    success, msg = await send_dorm_announcement(context, target_dorm_id, force=True)
    await update.effective_message.reply_text(msg)

def register_admin_handlers(application: Application) -> None:
    """Attach admin command handlers."""
    application.add_handler(CommandHandler("markpaid", markpaid_command))
    application.add_handler(CommandHandler("paid", markpaid_command))
    application.add_handler(CommandHandler("setid", setid_command))
    application.add_handler(CommandHandler("testgroup", testgroup_command))
    application.add_handler(CommandHandler("announce", announce_command))
    application.add_handler(CommandHandler("postannouncement", announce_command))
    application.add_handler(CommandHandler("set_ai_key", set_ai_key_command))
    application.add_handler(CommandHandler("setaikey", set_ai_key_command))
    application.add_handler(CommandHandler("aikey", set_ai_key_command))
