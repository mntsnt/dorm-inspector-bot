"""
Dorm Inspector Bot - Main Entry Point (Universal Multi-Tenant Platform)
Manages weekly cleaning schedules, photo inspections, and fine tracking for university dorms across Ethiopia.
Supports multi-tenant operation where any dorm can onboard via /setup,
while maintaining 100% backward compatibility for single-dorm environments.
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
from telegram.ext import Application, ApplicationBuilder, ContextTypes

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
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok", "service": "dorm-inspector-bot"}\n')

    def log_message(self, format, *args):
        # Silence standard HTTP access logging to keep stdout clean
        pass

def start_health_server() -> None:
    """Start the live status dashboard and health check server on $PORT."""
    port_str = os.environ.get("PORT", "8000")
    try:
        port = int(port_str)
    except ValueError:
        port = 8000

    try:
        import web
        thread = threading.Thread(
            target=web.run_web_server,
            kwargs={"host": "0.0.0.0", "port": port},
            daemon=True
        )
        thread.start()
        logger.info("Live Status Dashboard & Health server listening on http://0.0.0.0:%d", port)
        return
    except Exception as e:
        logger.warning("Could not launch FastAPI web dashboard, falling back to basic server: %s", e)

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
# Global Error Handler
# ---------------------------------------------------------------------------
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log uncaught errors gracefully to prevent update processor disruptions."""
    logger.error("Global exception caught handling update: %s", context.error, exc_info=context.error)

# ---------------------------------------------------------------------------
# Startup Post-Init Hook
# ---------------------------------------------------------------------------
async def post_init(application: Application) -> None:
    """Run initial database setup and report platform status."""
    logger.info("Initializing multi-tenant database...")
    await db.init_db()

    try:
        bot_info = await application.bot.get_me()
        logger.info("Bot authenticated successfully: @%s (ID: %s)", bot_info.username, bot_info.id)
    except Exception as e:
        logger.warning("Could not fetch bot information in post_init: %s", e)

    # Automatically seed the default dorm so it is always active
    await db.seed_default_dorm()
    logger.info("Default Dorm Group ID: %s (Topic ID: %s)", config.GROUP_ID, config.TOPIC_ID if config.TOPIC_ID is not None else "None/General")
    logger.info("Default Admin User ID: %s", config.ADMIN_ID)

    active_dorms = await db.get_all_active_dorms()
    logger.info("Universal Multi-Tenant Platform active with %d registered dorm(s).", len(active_dorms))
    for d in active_dorms:
        logger.info("• Dorm: '%s' (ID: %s, Admin: %s, Fine: %s ETB, Status: %s)", d["dorm_name"], d["dorm_id"], d["admin_id"], d["fine_amount"], d["status"])

    logger.info("Timezone: %s", config.TIMEZONE)

# ---------------------------------------------------------------------------
# Main Runner
# ---------------------------------------------------------------------------
def main() -> None:
    """Validate environment and run the bot application."""
    # 1. Start health check server immediately so container readiness probes succeed
    start_health_server()

    # 2. Check essential environment configuration
    if not config.BOT_TOKEN:
        logger.critical(
            "FATAL: Missing required environment variable BOT_TOKEN! "
            "Please configure it in your cloud platform dashboard or .env file."
        )
        logger.info("Health check server remains running to prevent container crash loops. Waiting for environment configuration...")
        while True:
            time.sleep(30)

    if not config.GROUP_ID or not config.ADMIN_ID:
        logger.info("GROUP_ID or ADMIN_ID not set. Running in Universal Multi-Tenant mode. Dorms can onboard via /setup.")
    else:
        logger.info("Starting Dorm Inspector Bot with default group %s and %d pre-configured members...", config.GROUP_ID, len(config.MEMBERS))

    # Build the Application
    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Register handlers
    register_all_handlers(app)

    # Register global error handler
    app.add_error_handler(global_error_handler)

    # Setup scheduled jobs
    setup_scheduler(app)

    # Start polling
    logger.info("Starting polling loop...")
    app.run_polling(drop_pending_updates=False)

if __name__ == "__main__":
    main()
