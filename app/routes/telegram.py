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

    # Silently save Telegram first name if not already stored
    first_name = from_user.get("first_name", "")
    if first_name and not user_store.get_profile(chat_id).get("first_name"):
        user_store.update_profile(chat_id, {"first_name": first_name})

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
    elif len(parts) == 2 and parts[1].lower() == "all":
        _send_all_weeks_plan(chat_id, plan, units)
        return
    elif len(parts) == 3 and parts[1].lower() == "week" and parts[2].isdigit():
        week_num = int(parts[2])
        week = user_store.get_week_plan(chat_id, week_num)
        label = f"Week {week_num}"
    else:
        telegram_client.send_message(chat_id, "Usage: /plan | /plan next | /plan all | /plan week 5")
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

    # Compact header
    header_parts = [label]
    if start and end:
        # Shorten dates: "May 12 – Jun 18"
        try:
            from datetime import datetime
            s = datetime.strptime(start, "%Y-%m-%d").strftime("%b %d")
            e = datetime.strptime(end, "%Y-%m-%d").strftime("%b %d")
            header_parts.append(f"{s} – {e}")
        except Exception:
            pass
    if phase:
        header_parts.append(phase)
    if target:
        header_parts.append(f"{target}{units}")

    lines = [" · ".join(header_parts), ""]

    for s in week.get("sessions", []):
        day = s.get("day", "")
        stype = s.get("type", "rest").replace("_", " ").title()
        dist = s.get(f"distance_{units}") or s.get("distance_km", "")
        pace = s.get("pace", "")
        notes = s.get("notes", "")

        if stype.lower() == "rest":
            lines.append(f"{day} — Rest")
            continue

        line = f"{day} — {stype}"
        if dist:
            line += f" {dist}{units}"
        if pace:
            line += f" @ {pace}"
        # Notes inline in brackets, keep it short
        if notes:
            short_note = notes.split(".")[0]  # first sentence only
            line += f" ({short_note})"
        lines.append(line)

    telegram_client.send_message(chat_id, "\n".join(lines))


def _send_all_weeks_plan(chat_id: str, plan: dict, units: str) -> None:
    """Send a compact one-line-per-week overview of the full plan."""
    from datetime import datetime

    weeks = plan.get("weeks", [])
    if not weeks:
        telegram_client.send_message(chat_id, "No plan found.")
        return

    total = len(weeks)
    profile = user_store.get_profile(chat_id)
    race_name = profile.get("race_name", "Race")
    race_date = profile.get("race_date", "")

    lines = [f"Full plan — {total} weeks to {race_name}", ""]

    current_phase = None
    for w in weeks:
        num = w.get("week", "")
        phase = w.get("phase", "").title()
        start = w.get("start_date", "")
        end = w.get("end_date", "")
        vol = w.get(f"target_volume_{units}") or w.get("target_volume_km", "")

        # Phase header when it changes
        if phase and phase != current_phase:
            if current_phase is not None:
                lines.append("")
            lines.append(f"── {phase} ──")
            current_phase = phase

        # Date range short form
        try:
            s = datetime.strptime(start, "%Y-%m-%d").strftime("%b %d")
            e = datetime.strptime(end, "%Y-%m-%d").strftime("%b %d")
            date_str = f"{s}–{e}"
        except Exception:
            date_str = start

        vol_str = f"  {vol}{units}" if vol else ""
        race_tag = "  🏁 RACE" if race_date and end and end >= race_date >= start else ""
        lines.append(f"Wk {num:>2}  {date_str}{vol_str}{race_tag}")

    # Telegram has a 4096 char limit — split if needed
    msg = "\n".join(lines)
    if len(msg) <= 4096:
        telegram_client.send_message(chat_id, msg)
    else:
        # Split at midpoint on a blank line
        mid = len(weeks) // 2
        lines1 = [f"Full plan — {total} weeks (Part 1)", ""]
        lines2 = [f"Full plan (Part 2)", ""]
        current_phase = None
        for w in weeks[:mid]:
            _append_week_line(w, units, lines1, current_phase)
        current_phase = None
        for w in weeks[mid:]:
            _append_week_line(w, units, lines2, current_phase)
        telegram_client.send_message(chat_id, "\n".join(lines1))
        telegram_client.send_message(chat_id, "\n".join(lines2))


def _append_week_line(w: dict, units: str, lines: list, current_phase) -> None:
    """Helper for split /plan all."""
    num = w.get("week", "")
    vol = w.get(f"target_volume_{units}") or w.get("target_volume_km", "")
    start = w.get("start_date", "")
    try:
        from datetime import datetime
        s = datetime.strptime(start, "%Y-%m-%d").strftime("%b %d")
    except Exception:
        s = start
    vol_str = f"  {vol}{units}" if vol else ""
    lines.append(f"Wk {num:>2}  {s}{vol_str}")


def _send_help(chat_id: str) -> None:
    msg = (
        "Here's what I can do:\n\n"
        "/start — begin or resume onboarding\n"
        "/plan — this week's sessions\n"
        "/plan next — next week's sessions\n"
        "/plan all — full plan overview\n"
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
