"""
AI Chat Handler for Group Mentions, Inspector Commands, and Direct Roommate Interaction.
Responds when @mentioned, when typing /inspector or /ask, or when replying to bot messages.
"""

import logging
import re
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config
import db
from services.ai_service import ask_ai

logger = logging.getLogger(__name__)

async def handle_ai_mention(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle @mentions, replies to the bot, or natural inspector references."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.text:
        return

    bot = context.bot
    bot_username = (bot.username or "").lower()

    is_group = chat.type in ("group", "supergroup")
    is_mention = False
    is_reply_to_bot = False

    text = message.text.strip()
    lower_text = text.lower()

    if is_group:
        # In groups, only answer if explicitly @mentioned or replied to directly
        if bot_username and f"@{bot_username}" in lower_text:
            is_mention = True
        elif "@inspector" in lower_text:
            is_mention = True
        elif (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == bot.id
        ):
            is_reply_to_bot = True

        if not is_mention and not is_reply_to_bot:
            return
    else:
        # In private chat, only respond if registered in a dorm or config
        user_dorms = await db.get_user_dorms(user.id)
        if not user_dorms and not config.is_member(user.id) and user.id != getattr(config, "ADMIN_ID", 6163528558):
            await message.reply_text(
                "Hey there! I am The Inspector. To chat with me and track your cleaning schedule, "
                "join your dorm group or send /join <dorm_code>."
            )
            return

    # Strip @bot_username and @inspector from prompt text
    cleaned_prompt = text
    if bot_username:
        cleaned_prompt = re.sub(rf"@{re.escape(bot_username)}\b", "", cleaned_prompt, flags=re.IGNORECASE)
    cleaned_prompt = re.sub(r"@?inspector\b", "", cleaned_prompt, flags=re.IGNORECASE).strip()
    cleaned_prompt = re.sub(r"^[,:\s]+", "", cleaned_prompt).strip()
    if not cleaned_prompt:
        cleaned_prompt = "Hello!"

    # Show typing indicator
    try:
        await bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Resolve target dorm without cross-dorm leakage
    if is_group:
        dorm_id = chat.id
    else:
        user_dorms = await db.get_user_dorms(user.id)
        if user_dorms:
            dorm_id = user_dorms[0]["dorm_id"]
        elif config.is_member(user.id) or user.id == getattr(config, "ADMIN_ID", 6163528558):
            dorm_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        else:
            return

    user_name = user.first_name or "Roommate"
    member = await db.get_dorm_member(dorm_id, user.id)
    if member:
        user_name = member["name"]

    response_text = await ask_ai(
        cleaned_prompt,
        dorm_id=dorm_id,
        user_name=user_name,
        user_id=user.id,
        chat_id=chat.id
    )

    # Reply to the user's message as clean natural text with persistent keyboard in DM
    reply_kb = None
    if not is_group:
        try:
            from handlers.commands import get_user_week_role, get_reply_keyboard
            is_cleaner, _, _ = await get_user_week_role(user.id, dorm_id=dorm_id)
            reply_kb = get_reply_keyboard(is_cleaner=is_cleaner)
        except Exception as e:
            logger.warning("Could not resolve reply keyboard: %s", e)

    kwargs = {"reply_to_message_id": message.message_id}
    if reply_kb is not None:
        kwargs["reply_markup"] = reply_kb

    try:
        await message.reply_text(
            response_text,
            **kwargs
        )
    except Exception as e:
        logger.warning("Could not send AI chat reply: %s", e)

async def inspector_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /inspector and /ask commands directly."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        query = "Who is cleaning this week and what are your instructions?"

    is_group = chat.type in ("group", "supergroup")
    if is_group:
        dorm_id = chat.id
    else:
        user_dorms = await db.get_user_dorms(user.id)
        if user_dorms:
            dorm_id = user_dorms[0]["dorm_id"]
        elif config.is_member(user.id) or user.id == getattr(config, "ADMIN_ID", 6163528558):
            dorm_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
        else:
            await message.reply_text("This bot is only for registered dorm members.")
            return

    try:
        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    user_name = user.first_name or "Roommate"
    member = await db.get_dorm_member(dorm_id, user.id)
    if member:
        user_name = member["name"]

    response_text = await ask_ai(
        query,
        dorm_id=dorm_id,
        user_name=user_name,
        user_id=user.id,
        chat_id=chat.id
    )

    reply_kb = None
    if not is_group:
        try:
            from handlers.commands import get_user_week_role, get_reply_keyboard
            is_cleaner, _, _ = await get_user_week_role(user.id, dorm_id=dorm_id)
            reply_kb = get_reply_keyboard(is_cleaner=is_cleaner)
        except Exception as e:
            logger.warning("Could not resolve reply keyboard for inspector command: %s", e)

    kwargs = {"reply_to_message_id": message.message_id}
    if reply_kb is not None:
        kwargs["reply_markup"] = reply_kb

    try:
        await message.reply_text(
            response_text,
            **kwargs
        )
    except Exception as e:
        logger.warning("Could not send AI command reply: %s", e)

async def reset_ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear AI conversational memory for the current chat."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    await db.clear_ai_chat_history(chat.id)
    await message.reply_text("Chat memory cleared! Starting a fresh conversation. What's on your mind?")

def register_ai_handlers(application: Application) -> None:
    """Register AI chat handler for group mentions, commands, and replies."""
    application.add_handler(CommandHandler("inspector", inspector_command))
    application.add_handler(CommandHandler("ask", inspector_command))
    application.add_handler(CommandHandler("clear_ai", reset_ai_command))
    application.add_handler(CommandHandler("reset_ai", reset_ai_command))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_ai_mention
        ),
        group=2
    )
