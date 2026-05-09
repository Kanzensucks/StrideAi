"""Telegram routes — incoming messages and button callbacks."""

import logging

from flask import Blueprint, request

from app.core import user_store
from app.integrations import telegram_client

logger = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram", __name__, url_prefix="/telegram")


@telegram_bp.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming Telegram messages and button callbacks."""
    from app.coaching import onboarding, plan_adjuster
    from app import pipelines

    data = request.json
    logger.info(f"Telegram webhook: {data}")

    callback = data.get("callback_query")
    if callback:
        _handle_callback(callback)
        return "OK", 200

    message = data.get("message", {})
    text = message.get("text", "")
    from_user = message.get("from", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    msg_timestamp = message.get("date")

    if not chat_id or from_user.get("is_bot", False) or not text.strip():
        return "OK", 200

    try:
        _handle_message(chat_id, text.strip(), from_user, msg_timestamp)
    except Exception as e:
        logger.error(f"Message handler failed for {chat_id}: {e}", exc_info=True)

    return "OK", 200


def _handle_callback(callback: dict) -> None:
    """Route button callbacks."""
    from app.coaching import onboarding, plan_adjuster

    chat_id = str(callback.get("message", {}).get("chat", {}).get("id", ""))
    callback_query_id = callback.get("id", "")
    data = callback.get("data", "")

    telegram_client.answer_callback_query(callback_query_id)

    if not chat_id or not data:
        return

    # Handle forgetme at any point (even during onboarding)
    if data == "forgetme_confirm":
        _execute_forgetme(chat_id)
        return
    elif data == "forgetme_cancel":
        telegram_client.send_message(chat_id, "No worries — your data is safe.")
        return

    if user_store.user_exists(chat_id) and not user_store.is_onboarded(chat_id):
        onboarding.handle_onboarding_message(chat_id, "", callback_data=data)
        return

    if data.startswith("session_action:"):
        parts = data.split(":")
        if len(parts) == 4:
            _, day, week_str, action = parts
            try:
                week_number = int(week_str)
            except ValueError:
                week_number = 1
            msg = plan_adjuster.apply_button_action(chat_id, day, week_number, action)
            if msg:
                telegram_client.send_message(chat_id, msg)

    # (forgetme handled above before onboarding check)


def _handle_message(chat_id: str, text: str, from_user: dict, msg_timestamp) -> None:
    """Route an incoming text message."""
    from app.coaching import onboarding
    from app import pipelines

    if not user_store.user_exists(chat_id):
        if user_store.is_at_capacity():
            telegram_client.send_message(chat_id, user_store.beta_full_message())
            return
        user_store.create_user(chat_id)

    # Commands always take priority — even during onboarding
    if text.startswith("/"):
        _handle_command(chat_id, text, from_user)
        return

    if not user_store.is_onboarded(chat_id):
        onboarding.handle_onboarding_message(chat_id, text)
        return

    pipelines.handle_chat(chat_id, text, msg_timestamp=msg_timestamp)


def _handle_command(chat_id: str, text: str, from_user: dict) -> None:
    """Handle bot commands."""
    from app.coaching import onboarding

    parts = text.split()
    command = parts[0].lower().split("@")[0]

    if command == "/start":
        if user_store.is_onboarded(chat_id):
            telegram_client.send_message(chat_id, "You're already set up! Chat with me anytime, or use /help.")
        elif user_store.user_exists(chat_id):
            onboarding.resume_onboarding(chat_id)
        else:
            if not user_store.user_exists(chat_id):
                if not user_store.create_user(chat_id):
                    telegram_client.send_message(chat_id, user_store.beta_full_message())
                    return
            onboarding.start_onboarding(chat_id)

    elif command == "/plan":
        _handle_plan_command(chat_id, parts)

    elif command == "/pace":
        profile = user_store.get_profile(chat_id)
        paces = profile.get("paces", {})
        units = profile.get("units", "km")
        if paces:
            lines = [f"{k.replace('_', ' ').title()}: {v}" for k, v in paces.items()]
            telegram_client.send_message(chat_id, "\n".join(lines))
        else:
            telegram_client.send_message(chat_id, "Pace zones not calculated yet — finish onboarding first.")

    elif command == "/forgetme":
        keyboard = [[
            {"text": "Yes, delete everything", "callback_data": "forgetme_confirm"},
            {"text": "Cancel", "callback_data": "forgetme_cancel"},
        ]]
        telegram_client.send_message_with_keyboard(
            chat_id,
            "This will permanently delete all your data and revoke Strava access. "
            "Are you sure?",
            keyboard,
        )

    elif command == "/help":
        _send_help(chat_id)

    else:
        telegram_client.send_message(chat_id, "Unknown command. Type /help to see what I can do.")


def _handle_plan_command(chat_id: str, parts: list) -> None:
    """Handle /plan, /plan next, /plan week N."""
    if not user_store.is_onboarded(chat_id):
        telegram_client.send_message(chat_id, "Finish onboarding first — type /start.")
        return

    plan = user_store.get_plan(chat_id)
    if not plan:
        telegram_client.send_message(chat_id, "No plan found yet. Type /start to begin.")
        return

    profile = user_store.get_profile(chat_id)
    units = profile.get("units", "km")

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == "current"):
        week = user_store.get_current_week_plan(chat_id)
        label = "Current week"
    elif len(parts) == 2 and parts[1].lower() == "next":
        current = user_store.get_current_week_plan(chat_id)
        week_num = (current.get("week", 1) + 1) if current else 2
        week = user_store.get_week_plan(chat_id, week_num)
        label = f"Week {week_num}"
    elif len(parts) == 3 and parts[1].lower() == "week" and parts[2].isdigit():
        week_num = int(parts[2])
        week = user_store.get_week_plan(chat_id, week_num)
        label = f"Week {week_num}"
    else:
        telegram_client.send_message(chat_id, "Usage: /plan | /plan next | /plan week 5")
        return

    if not week:
        telegram_client.send_message(chat_id, "Couldn't find that week in your plan.")
        return

    _send_week_plan(chat_id, week, label, units)


def _send_week_plan(chat_id: str, week: dict, label: str, units: str) -> None:
    """Format and send a week's plan as plain text."""
    phase = week.get("phase", "").title()
    start = week.get("start_date", "")
    end = week.get("end_date", "")
    target = week.get(f"target_volume_{units}") or week.get("target_volume_km", "")

    header = f"{label}"
    if start and end:
        header += f" ({start} – {end})"
    if phase:
        header += f" — {phase}"
    if target:
        header += f" | Target: {target}{units}"

    lines = [header, ""]
    for s in week.get("sessions", []):
        day = s.get("day", "")
        stype = s.get("type", "rest").replace("_", " ").title()
        dist = s.get(f"distance_{units}") or s.get("distance_km", "")
        pace = s.get("pace", "")
        notes = s.get("notes", "")

        line = f"{day} — {stype}"
        if dist:
            line += f" {dist}{units}"
        if pace:
            line += f" @ {pace}"
        if notes:
            line += f"\n    {notes}"
        lines.append(line)

    telegram_client.send_message(chat_id, "\n".join(lines))


def _send_help(chat_id: str) -> None:
    msg = (
        "Here's what I can do:\n\n"
        "/start — begin or resume onboarding\n"
        "/plan — this week's sessions\n"
        "/plan next — next week's sessions\n"
        "/plan week N — any specific week\n"
        "/pace — your current training pace zones\n"
        "/forgetme — delete your account and data\n"
        "/help — show this message\n\n"
        "You can also just chat with me — ask about your training, "
        "request adjustments, or tell me how you're feeling."
    )
    telegram_client.send_message(chat_id, msg)


def _execute_forgetme(chat_id: str) -> None:
    """Wipe all user data and revoke Strava."""
    try:
        from app.integrations import strava_client
        strava_client.revoke_access(chat_id)
    except Exception:
        pass
    user_store.delete_user(chat_id)
    telegram_client.send_message(
        chat_id,
        "Done. All your data has been deleted and Strava access revoked. "
        "Type /start anytime to start fresh."
    )
    logger.info(f"User {chat_id} deleted via /forgetme")
