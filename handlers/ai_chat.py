"""
AI Chat Handler for Group Mentions and Direct Roommate Interaction.
Responds when @mentioned or when replying to bot messages in dorm groups.
"""

import logging
import re
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

import config
import db
from services.ai_service import ask_ai

logger = logging.getLogger(__name__)

async def handle_ai_mention(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle @mentions, replies to the bot, or direct private questions."""
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

    if is_group:
        if bot_username and f"@{bot_username}" in text.lower():
            is_mention = True
        elif (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == bot.id
        ):
            is_reply_to_bot = True

        # In groups, only answer if explicitly mentioned or replied to
        if not is_mention and not is_reply_to_bot:
            return
    else:
        # In private chat, only respond if registered in a dorm or config
        user_dorms = await db.get_user_dorms(user.id)
        if not user_dorms and not config.is_member(user.id) and user.id != getattr(config, "ADMIN_ID", 6163528558):
            return

    # Strip @bot_username from prompt text
    cleaned_prompt = text
    if bot_username:
        cleaned_prompt = re.sub(rf"@{re.escape(bot_username)}", "", cleaned_prompt, flags=re.IGNORECASE).strip()
    if not cleaned_prompt:
        cleaned_prompt = "Hello!"

    # Show typing indicator
    try:
        await bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    # Resolve target dorm
    dorm_id = chat.id if is_group else getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    dorm = await db.get_dorm(dorm_id)
    if not dorm and not is_group:
        user_dorms = await db.get_user_dorms(user.id)
        if user_dorms:
            dorm_id = user_dorms[0]["dorm_id"]
        else:
            dorm_id = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)

    user_name = user.first_name or "Roommate"
    member = await db.get_dorm_member(dorm_id, user.id)
    if member:
        user_name = member["name"]

    response_text = await ask_ai(cleaned_prompt, dorm_id=dorm_id, user_name=user_name)

    # Reply to the user's message
    try:
        await message.reply_text(
            response_text,
            reply_to_message_id=message.message_id,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning("Markdown reply failed (%s), falling back to plain text", e)
        await message.reply_text(
            response_text,
            reply_to_message_id=message.message_id
        )

def register_ai_handlers(application: Application) -> None:
    """Register AI chat handler for group mentions and replies."""
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_ai_mention
        ),
        group=2
    )
