"""First-deploy initialisation — creates the users directory."""

import logging
import os

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", ".")


def init():
    """Ensure the /data/users directory exists."""
    users_dir = os.path.join(DATA_DIR, "users")
    os.makedirs(users_dir, exist_ok=True)
    logger.info(f"init_data: data directory ready at {DATA_DIR}")
