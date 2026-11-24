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

def _has_markdown_formatting(text: str) -> bool:
    """Check if text contains markdown formatting markers (*bold*, `code`, etc.) that need parsing."""
    return bool(re.search(r"(\*[^*]+\*|`[^`]+`|_([^_]+)_|^•\s+)", text, flags=re.MULTILINE))

async def send_safe_reply(message, text: str, **kwargs) -> None:
    """Send text reply safely with Markdown rendering, splitting into complete chunks if exceeding Telegram limit."""
    if not text:
        return

    async def _send_chunk(chunk_text: str, **kw_args) -> None:
        call_kw = dict(kw_args)
        if "parse_mode" not in call_kw and _has_markdown_formatting(chunk_text):
            call_kw["parse_mode"] = ParseMode.MARKDOWN
        try:
            await message.reply_text(chunk_text, **call_kw)
        except Exception as e:
            logger.warning("Failed to render markdown reply (%s), sending as plain text", e)
            call_kw.pop("parse_mode", None)
            await message.reply_text(chunk_text, **call_kw)

    if len(text) <= 4000:
        await _send_chunk(text, **kwargs)
        return

    # Split cleanly on paragraph or sentence boundaries
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 4000:
            if current:
                chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)

    for idx, chunk in enumerate(chunks):
        kw = dict(kwargs)
        if idx < len(chunks) - 1:
            kw.pop("reply_markup", None)
        await _send_chunk(chunk, **kw)

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

    try:
        await db.upsert_user(user.id, user_name, chat_id=chat.id, username=user.username)
    except Exception:
        pass

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
        await send_safe_reply(
            message,
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

    try:
        await db.upsert_user(user.id, user_name, chat_id=chat.id, username=user.username)
    except Exception:
        pass

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
        await send_safe_reply(
            message,
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
    await message.reply_text("Chat memory cleared! Clean slate. Don't make me repeat the dorm rules. What do you need?")

async def ai_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Diagnostic command to report AI engine status, provider, and troubleshooting info."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if not user or not chat or not message:
        return

    is_group = chat.type in ("group", "supergroup")
    dorm_id = chat.id if is_group else None
    from handlers.commands import _resolve_target_dorm
    dorm = await _resolve_target_dorm(dorm_id=dorm_id, user_id=user.id)
    target_dorm_id = dorm["dorm_id"] if dorm else (dorm_id or getattr(config, "DEFAULT_GROUP_ID", -1002378440774))
    dorm_name = dorm["dorm_name"] if dorm else "Default Dorm"

    from services.ai_service import get_api_key
    dorm_key = await db.get_dorm_ai_key(target_dorm_id)
    env_key = get_api_key()

    default_gid = getattr(config, "DEFAULT_GROUP_ID", -1002378440774)
    is_primary = target_dorm_id in (default_gid, getattr(config, "GROUP_ID", 0), 0)

    active_key = dorm_key or (env_key if is_primary else "")
    key_source = "Dorm Database (/set_ai_key)" if dorm_key else ("Server Environment (.env)" if (env_key and is_primary) else "None")

    if active_key:
        if active_key.startswith("gsk_"):
            provider = "Groq Cloud (LLaMA 3.3 70B - Free & Ultra-Fast)"
        elif active_key.startswith("sk-or-"):
            provider = "OpenRouter (Free Tier Models)"
        elif active_key.startswith("sk-"):
            provider = "OpenAI (gpt-4o-mini)"
        else:
            provider = "Google Gemini (gemini-1.5-flash / gemini-2.0-flash)"
        masked = f"...{active_key[-4:]}" if len(active_key) >= 4 else "configured"
        status_line = "🟢 **ONLINE (Conversational AI Active)**"
        details = (
            f"• **Dorm:** {dorm_name}\n"
            f"• **Provider:** {provider}\n"
            f"• **API Key:** `{masked}` (Source: {key_source})\n"
            f"• **Conversational Mode:** ChatGPT-style multi-turn memory active\n\n"
            "I follow your chats, remember conversation history, answer dorm questions, and keep you disciplined!"
        )
    else:
        status_line = "⚠️ **OFFLINE FALLBACK MODE (No API Key)**"
        details = (
            f"• **Dorm:** {dorm_name}\n"
            f"• **Provider:** Built-in Offline Rules Script\n"
            f"• **API Key:** None configured\n\n"
            "**Why am I repeating canned responses?**\n"
            "Without an active AI key, I cannot generate freeform ChatGPT-style replies. I can only provide fixed offline status answers.\n\n"
            "**How to activate full ChatGPT-style AI (100% Free):**\n"
            "1. **Google Gemini (Recommended):** Get a free key at [Google AI Studio](https://aistudio.google.com/)\n"
            "2. **Groq (Llama 3.3 70B):** Get a free key at [Groq Console](https://console.groq.com/)\n"
            "3. Send `/set_ai_key <your_key>` in this chat or DM"
        )

    await message.reply_text(
        f"🤖 **Dorm Inspector AI Status**\n\n"
        f"Status: {status_line}\n\n"
        f"{details}",
        parse_mode=ParseMode.MARKDOWN
    )

def register_ai_handlers(application: Application) -> None:
    """Register AI chat handler for group mentions, commands, and replies."""
    application.add_handler(CommandHandler("inspector", inspector_command))
    application.add_handler(CommandHandler("ask", inspector_command))
    application.add_handler(CommandHandler("ai_status", ai_status_command))
    application.add_handler(CommandHandler("ai", ai_status_command))
    application.add_handler(CommandHandler("clear_ai", reset_ai_command))
    application.add_handler(CommandHandler("reset_ai", reset_ai_command))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_ai_mention
        ),
        group=2
    )
