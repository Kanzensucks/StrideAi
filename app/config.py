"""Application configuration — loaded once at startup."""

import os


class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")

    # Strava
    STRAVA_CLIENT_ID: str = os.environ.get("STRAVA_CLIENT_ID", "")
    STRAVA_CLIENT_SECRET: str = os.environ.get("STRAVA_CLIENT_SECRET", "")
    STRAVA_VERIFY_TOKEN: str = os.environ.get("STRAVA_VERIFY_TOKEN", "strideai2026")

    # Anthropic
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

    # Deployment
    PUBLIC_DOMAIN: str = os.environ.get("PUBLIC_DOMAIN", "localhost")
    DATA_DIR: str = os.environ.get("DATA_DIR", ".")
    MAX_USERS: int = int(os.environ.get("MAX_USERS", "10"))

    # Paths
    BASE_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    KNOWLEDGE_DIR: str = os.path.join(BASE_DIR, "knowledge")
