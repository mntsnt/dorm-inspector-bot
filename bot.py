"""
Dorm Inspector Bot - Main Entry Point
A Telegram bot in Python that manages a weekly cleaning schedule for 8 dorm mates.
"""

import asyncio
import http.server
import logging
import os
import socketserver
import sys
import threading
import time

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
# Lightweight HTTP Health Check Server (for Cloud Deployment / Readiness Probes)
# ---------------------------------------------------------------------------
class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok", "service": "dorm-inspector-bot"}\n')

    def log_message(self, format, *args):
        # Silence standard HTTP access logging to keep stdout clean
        pass

def start_health_server() -> None:
    """Start a lightweight HTTP server on $PORT to satisfy cloud readiness probes."""
    port_str = os.environ.get("PORT", "3000")
    try:
        port = int(port_str)
    except ValueError:
        port = 3000

    class ReusableTCPServer(socketserver.TCPServer):
        allow_reuse_address = True

    try:
        server = ReusableTCPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info("Health check server listening on 0.0.0.0:%d", port)
    except Exception as e:
        logger.warning("Could not start health check server on port %d: %s", port, e)

# ---------------------------------------------------------------------------
# Startup Post-Init Hook
# ---------------------------------------------------------------------------
async def post_init(application: Application) -> None:
    """Run initial database setup and report bot status."""
    logger.info("Initializing database...")
    await db.init_db()

    bot_info = await application.bot.get_me()
    logger.info("Bot authenticated successfully: @%s (ID: %s)", bot_info.username, bot_info.id)
    logger.info("Dorm Group ID: %s (Topic ID: %s)", config.GROUP_ID, config.TOPIC_ID if config.TOPIC_ID is not None else "None/General")
    logger.info("Admin User ID: %s", config.ADMIN_ID)
    logger.info("Rotation Start Date: %s (%s)", config.START_DATE_STR, config.START_DATE_OBJ.strftime("%A"))
    logger.info("Timezone: %s", config.TIMEZONE)

    current_week = config.get_week_info()
    if current_week.week_number < 1:
        w1 = config.get_week_info(config.START_BASE_MONDAY)
        logger.info(
            "Schedule not started yet. Starts %s (%s): Cleaners -> %s & %s",
            w1.week_start,
            w1.monday_date.strftime("%A"),
            w1.cleaner1.name,
            w1.cleaner2.name,
        )
    else:
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
    # 1. Start health check server immediately so container readiness probes succeed
    start_health_server()

    # 2. Check essential environment configuration
    missing_vars = []
    if not config.BOT_TOKEN:
        missing_vars.append("BOT_TOKEN")
    if not config.GROUP_ID:
        missing_vars.append("GROUP_ID")
    if not config.ADMIN_ID:
        missing_vars.append("ADMIN_ID")

    if missing_vars:
        logger.critical(
            "FATAL: Missing required environment variable(s): %s! "
            "Please configure them in your cloud platform dashboard or .env file.",
            ", ".join(missing_vars),
        )
        logger.info("Health check server remains running to prevent container crash loops. Waiting for environment configuration...")
        while True:
            time.sleep(30)

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
