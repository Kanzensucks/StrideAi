"""StrideAI Flask application factory."""

import logging
import os

from flask import Flask

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)

    # Initialise data directory
    from app.core.init_data import init
    init()

    # Register blueprints
    from app.routes.strava import strava_bp
    from app.routes.telegram import telegram_bp
    from app.routes.admin import admin_bp
    app.register_blueprint(strava_bp)
    app.register_blueprint(telegram_bp)
    app.register_blueprint(admin_bp)

    # Start background scheduler
    from app.core.scheduler import start_scheduler
    start_scheduler()

    # Register Telegram webhook with the platform
    _setup_telegram_webhook()

    logger.info("StrideAI application created")
    return app


def _setup_telegram_webhook() -> None:
    railway_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN") or os.environ.get("PUBLIC_DOMAIN")
    if railway_url:
        webhook_url = f"https://{railway_url}/telegram/webhook"
        try:
            from app.integrations import telegram_client
            result = telegram_client.set_webhook(webhook_url)
            logger.info(f"Telegram webhook set: {webhook_url} — {result}")
        except Exception as e:
            logger.error(f"Failed to set Telegram webhook: {e}")
