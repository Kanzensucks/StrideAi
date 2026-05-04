"""Strava routes — webhook events, OAuth connect + callback."""

import json
import logging
import os
import threading
import time

from flask import Blueprint, request, jsonify, redirect

from app.integrations import strava_oauth
from app.integrations import telegram_client
from app.core import user_store

logger = logging.getLogger(__name__)

strava_bp = Blueprint("strava", __name__, url_prefix="/strava")

# File-based dedup for Strava webhooks
DEDUP_WINDOW = 600  # 10 min
_dedup_file = os.path.join(os.environ.get("DATA_DIR", "."), "strava_dedup.json")


def _load_dedup():
    try:
        with open(_dedup_file, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_dedup(data):
    with open(_dedup_file, "w") as f:
        json.dump(data, f)


def _find_chat_id_by_athlete(athlete_id: int) -> str | None:
    """Find a user's chat_id given their Strava athlete_id."""
    for cid in user_store.all_user_ids():
        tokens = user_store.get_strava_tokens(cid)
        if str(tokens.get("athlete_id", "")) == str(athlete_id):
            return cid
    return None


def _oauth_result_page(success: bool) -> str:
    if success:
        return """<html><body style="font-family:sans-serif;text-align:center;padding:60px">
        <h2>Strava connected!</h2>
        <p>Head back to Telegram — your training plan is being generated now.</p>
        </body></html>"""
    return """<html><body style="font-family:sans-serif;text-align:center;padding:60px">
    <h2>Something went wrong</h2>
    <p>Please go back to Telegram and try the link again, or type /start.</p>
    </body></html>"""


@strava_bp.route("/webhook", methods=["GET"])
def webhook_verify():
    """Strava webhook subscription verification."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.environ.get("STRAVA_VERIFY_TOKEN", "strideai2026")
    if mode == "subscribe" and token == verify_token:
        logger.info("Strava webhook verified")
        return jsonify({"hub.challenge": challenge})
    return "Forbidden", 403


@strava_bp.route("/webhook", methods=["POST"])
def webhook_event():
    """Handle incoming Strava activity events.

    All users share one webhook subscription. Route by matching
    athlete_id from the event to user profiles.
    """
    from app import pipelines

    data = request.json
    logger.info(f"Strava webhook event: {data}")

    if data.get("object_type") == "activity" and data.get("aspect_type") == "create":
        activity_id = data.get("object_id")
        athlete_id = data.get("owner_id")

        if activity_id and athlete_id:
            now = time.time()
            processed = _load_dedup()
            processed = {k: v for k, v in processed.items() if now - v < DEDUP_WINDOW}
            key = f"{athlete_id}:{activity_id}"
            if key in processed:
                logger.info(f"Skipping duplicate webhook {key}")
                return "OK", 200
            processed[key] = now
            _save_dedup(processed)

            chat_id = _find_chat_id_by_athlete(athlete_id)
            if not chat_id:
                logger.warning(f"No user found for Strava athlete {athlete_id}")
                return "OK", 200

            def _run(cid, aid):
                try:
                    pipelines.daily_analysis(cid, aid)
                except Exception as e:
                    logger.error(f"Daily analysis failed for {cid}: {e}", exc_info=True)

            threading.Thread(target=_run, args=(chat_id, activity_id), daemon=True).start()
            logger.info(f"Queued daily_analysis for {chat_id} activity {activity_id}")

    return "OK", 200


@strava_bp.route("/connect")
def connect():
    """Redirect user to Strava OAuth page."""
    chat_id = request.args.get("chat_id", "")
    if not chat_id:
        return "Missing chat_id", 400
    auth_url = strava_oauth.get_auth_url(chat_id)
    return redirect(auth_url)


@strava_bp.route("/callback")
def callback():
    """Handle Strava OAuth callback — exchange code, save tokens, complete onboarding."""
    from app.coaching import onboarding

    code = request.args.get("code")
    chat_id = request.args.get("state")
    error = request.args.get("error")

    if error or not code or not chat_id:
        logger.warning(f"Strava callback error: {error}, chat_id: {chat_id}")
        return _oauth_result_page(success=False)

    try:
        tokens = strava_oauth.exchange_code(code, chat_id)
        user_store.save_strava_tokens(chat_id, tokens)

        profile = user_store.get_profile(chat_id)
        if not profile.get("first_name") and tokens.get("athlete_firstname"):
            user_store.update_profile(chat_id, {"first_name": tokens["athlete_firstname"]})

        onboarding.complete_onboarding_after_strava(chat_id)
        logger.info(f"Strava connected for {chat_id}")
        return _oauth_result_page(success=True)

    except Exception as e:
        logger.error(f"Strava callback failed for {chat_id}: {e}", exc_info=True)
        telegram_client.send_message(
            chat_id,
            "Something went wrong connecting Strava. Please try the link again or type /start."
        )
        return _oauth_result_page(success=False)
