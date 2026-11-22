"""
Handlers Package for Dorm Inspector Bot
"""

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .admin import register_admin_handlers
from .ai_chat import register_ai_handlers
from .approval import register_approval_handlers
from .cleaning import register_cleaning_handlers
from .commands import register_command_handlers
from .setup import register_setup_handlers
from .swap import register_swap_handlers

def register_all_handlers(application: Application) -> None:
    """Register all bot handlers with the Telegram application."""
    register_setup_handlers(application)
    register_command_handlers(application)
    register_cleaning_handlers(application)
    register_approval_handlers(application)
    register_admin_handlers(application)
    register_ai_handlers(application)
    register_swap_handlers(application)
