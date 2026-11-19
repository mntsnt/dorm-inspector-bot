"""
Dorm Inspector Bot - Main Entry Point
A Telegram bot in Python that manages a weekly cleaning schedule for 8 dorm mates.
"""

import asyncio
import logging
import sys

from telegram import Update
from telegram.ext import Application, ApplicationBuilder

import config
import db
from handlers import register_all_handlers
from scheduler import setup_scheduler

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - [%(name)s] - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
# Reduce excessive logging from httpx and telegram network calls
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logger = logging.getLogger("DormInspectorBot")

# ---------------------------------------------------------------------------
# Startup Post-Init Hook
# ---------------------------------------------------------------------------
async def post_init(application: Application) -> None:
    """Run initial database setup and report bot status."""
    logger.info("Initializing database...")
    await db.init_db()

    bot_info = await application.bot.get_me()
    logger.info("Bot authenticated successfully: @%s (ID: %s)", bot_info.username, bot_info.id)
    logger.info("Dorm Group ID: %s", config.GROUP_ID)
    logger.info("Admin User ID: %s", config.ADMIN_ID)
    logger.info("Rotation Start Date: %s (%s)", config.START_DATE_STR, config.START_DATE_OBJ.strftime("%A"))
    logger.info("Timezone: %s", config.TIMEZONE)

    current_week = config.get_week_info()
    logger.info(
        "Current Week #%d (%s): Cleaners -> %s & %s",
        current_week.week_number,
        current_week.week_start,
        current_week.cleaner1.name,
        current_week.cleaner2.name,
    )

# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------
def main() -> None:
    """Validate environment and run the bot application."""
    if not config.BOT_TOKEN:
        logger.critical(
            "FATAL: BOT_TOKEN is missing! Please set BOT_TOKEN in your .env file or environment."
        )
        sys.exit(1)

    if not config.GROUP_ID:
        logger.critical(
            "FATAL: GROUP_ID is missing! Please set GROUP_ID in your .env file."
        )
        sys.exit(1)

    if not config.ADMIN_ID:
        logger.critical(
            "FATAL: ADMIN_ID is missing! Please set ADMIN_ID in your .env file."
        )
        sys.exit(1)

    logger.info("Starting Dorm Inspector Bot with %d registered members...", len(config.MEMBERS))

    # Build the Application
    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register handlers
    register_all_handlers(app)

    # Setup scheduled jobs
    setup_scheduler(app)

    # Start polling
    logger.info("Starting polling loop...")
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
