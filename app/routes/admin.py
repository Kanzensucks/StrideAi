"""Admin routes — health check, manual test endpoints, webhook setup."""

import logging
import os

from flask import Blueprint, jsonify

from app.core import user_store
from app.integrations import strava_oauth

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "strideai",
        "users": user_store.count_users(),
        "capacity": int(os.environ.get("MAX_USERS", 50)),
    })


@admin_bp.route("/test/weekly/<chat_id>", methods=["POST"])
def test_weekly(chat_id):
    from app import pipelines
    try:
        report = pipelines.weekly_report(chat_id)
        return jsonify({"status": "ok", "length": len(report)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@admin_bp.route("/test/daily/<chat_id>/<int:activity_id>", methods=["POST"])
def test_daily(chat_id, activity_id):
    from app import pipelines
    try:
        coaching = pipelines.daily_analysis(chat_id, activity_id)
        return jsonify({"status": "ok", "length": len(coaching)})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@admin_bp.route("/test/morning/<chat_id>", methods=["POST"])
def test_morning(chat_id):
    from app import pipelines
    try:
        pipelines.morning_reminder(chat_id)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@admin_bp.route("/admin/webhook/setup", methods=["POST"])
def setup_strava_webhook():
    """One-shot: register the Strava push subscription for this domain."""
    public_domain = os.environ.get("PUBLIC_DOMAIN", "")
    verify_token = os.environ.get("STRAVA_VERIFY_TOKEN", "strideai2026")
    if not public_domain:
        return jsonify({"status": "error", "message": "PUBLIC_DOMAIN not set"}), 400
    result = strava_oauth.register_webhook(verify_token, public_domain)
    return jsonify(result)
