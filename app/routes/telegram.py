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

    if data.startswith("goaltime:"):
        _handle_goaltime_callback(chat_id, data)
        return

    if data.startswith("setrace_dist:"):
        dist = data.split(":", 1)[1]
        _handle_setrace_step(chat_id, dist, is_callback=True)
        return

    if data.startswith("goalreview:"):
        _handle_goalreview_callback(chat_id, data)
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
            if msg and msg.startswith("SWAP_PICK:"):
                # Show a day picker — "SWAP_PICK:<from_day>:<week>:<day1>,<day2>,..."
                _, from_day, wk, days_csv = msg.split(":", 3)
                other_days = days_csv.split(",")
                keyboard = [
                    [{"text": d, "callback_data": f"swap_confirm:{from_day}:{wk}:{d}"}]
                    for d in other_days
                ]
                telegram_client.send_message_with_keyboard(
                    chat_id,
                    f"Move {from_day}'s session to which day?",
                    keyboard,
                )
            elif msg:
                telegram_client.send_message(chat_id, msg)

    if data.startswith("swap_confirm:"):
        # "swap_confirm:<from_day>:<week_number>:<to_day>"
        parts = data.split(":")
        if len(parts) == 4:
            _, from_day, week_str, to_day = parts
            try:
                week_number = int(week_str)
            except ValueError:
                week_number = 1
            msg = plan_adjuster.apply_swap(chat_id, from_day, to_day, week_number)
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

    # Active /setrace flow takes priority over regular chat
    if user_store.get_setrace_state(chat_id).get("active"):
        _handle_setrace_step(chat_id, text)
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

    elif command == "/setrace":
        if not user_store.is_onboarded(chat_id):
            telegram_client.send_message(chat_id, "Finish onboarding first — type /start.")
        else:
            user_store.save_setrace_state(chat_id, {"active": True, "step": 1, "data": {}})
            telegram_client.send_message(
                chat_id,
                "Let's update your race goal.\n\n"
                "What race are you training for? Reply with the name and date:\n"
                "e.g. Berlin Marathon, September 27 2026"
            )

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


def _handle_setrace_step(chat_id: str, text: str, is_callback: bool = False) -> None:
    """3-step flow: race name+date → distance → goal time → regenerate plan."""
    from app.coaching.onboarding import _parse_race_name_and_date, _normalise_goal_time
    from app import pipelines
    import threading

    state = user_store.get_setrace_state(chat_id)
    step = state.get("step", 1)
    data = state.get("data", {})

    if step == 1:
        # Expecting race name + date
        parsed = _parse_race_name_and_date(text)
        if not parsed:
            telegram_client.send_message(
                chat_id,
                "Couldn't parse that — try: Berlin Marathon, September 27 2026"
            )
            return
        data["race_name"] = parsed["name"]
        data["race_date"] = parsed["date"]
        state["step"] = 2
        state["data"] = data
        user_store.save_setrace_state(chat_id, state)

        keyboard = [
            [{"text": "5k", "callback_data": "setrace_dist:5k"}],
            [{"text": "10k", "callback_data": "setrace_dist:10k"}],
            [{"text": "Half Marathon", "callback_data": "setrace_dist:half_marathon"}],
            [{"text": "Marathon", "callback_data": "setrace_dist:marathon"}],
        ]
        telegram_client.send_message_with_keyboard(chat_id, "What distance?", keyboard)

    elif step == 2:
        # Expecting distance (from button callback)
        if text not in ("5k", "10k", "half_marathon", "marathon"):
            keyboard = [
                [{"text": "5k", "callback_data": "setrace_dist:5k"}],
                [{"text": "10k", "callback_data": "setrace_dist:10k"}],
                [{"text": "Half Marathon", "callback_data": "setrace_dist:half_marathon"}],
                [{"text": "Marathon", "callback_data": "setrace_dist:marathon"}],
            ]
            telegram_client.send_message_with_keyboard(chat_id, "Tap one of the options below:", keyboard)
            return
        data["race_distance"] = text
        state["step"] = 3
        state["data"] = data
        user_store.save_setrace_state(chat_id, state)
        telegram_client.send_message(
            chat_id,
            "What's your goal time?\n"
            "Any format works — 3:30:00, 3h30m, sub 3:30, or \"finish\""
        )

    elif step == 3:
        # Expecting goal time
        data["goal_time"] = _normalise_goal_time(text)
        user_store.clear_setrace_state(chat_id)

        # Update profile with new race details
        from app.coaching import plan_generator
        profile = user_store.update_profile(chat_id, {
            "race_name": data["race_name"],
            "race_date": data["race_date"],
            "race_distance": data["race_distance"],
            "goal_time": data["goal_time"],
            "paces": {},  # will be recalculated
        })

        # Recalculate paces
        try:
            paces = plan_generator.calculate_paces(profile)
            if paces:
                user_store.update_profile(chat_id, {"paces": paces})
        except Exception:
            pass

        dist_label = data["race_distance"].replace("_", " ").title()
        telegram_client.send_message(
            chat_id,
            f"Got it — {data['race_name']} ({dist_label}) on {data['race_date']} "
            f"in {data['goal_time']}.\n\n"
            f"Regenerating your full plan now — give me about 2 minutes..."
        )

        threading.Thread(
            target=pipelines.generate_and_send_plan,
            args=(chat_id,),
            kwargs={"stated_goal": data["goal_time"]},
            daemon=True,
        ).start()


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
        "/setrace — change your target race, distance or goal time\n"
        "/forgetme — delete your account and data\n"
        "/help — show this message\n\n"
        "You can also just chat with me — ask about your training, "
        "request adjustments, or tell me how you're feeling."
    )
    telegram_client.send_message(chat_id, msg)


def _handle_goaltime_callback(chat_id: str, data: str) -> None:
    """Handle goal selection shown after plan generation."""
    from app.coaching import plan_generator

    goal = data.split(":", 1)[1]
    profile = user_store.update_profile(chat_id, {"goal_time": goal})

    try:
        paces = plan_generator.calculate_paces(profile)
        if paces:
            user_store.update_profile(chat_id, {"paces": paces})
    except Exception:
        pass

    def _fmt(t):
        parts = t.split(":")
        return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else t

    telegram_client.send_message(
        chat_id,
        f"Goal locked in: *{_fmt(goal)}* 🎯\n\n"
        f"Training paces updated — type /pace to see your zones.\n\n"
        f"Let's get to work."
    )
    logger.info(f"Goal time set to {goal} for {chat_id}")


def _handle_goalreview_callback(chat_id: str, data: str) -> None:
    """Handle goal review button — accept a new goal time or keep the current one."""
    from app.coaching import plan_generator

    value = data.split(":", 1)[1]

    if value == "keep":
        profile = user_store.get_profile(chat_id)
        current = profile.get("goal_time", "your current goal")
        telegram_client.send_message(
            chat_id,
            f"Got it — keeping {current}. Keep up the consistent work! 💪"
        )
        return

    # value is a new goal time string e.g. "3:38:00"
    new_goal = value
    profile = user_store.update_profile(chat_id, {"goal_time": new_goal})

    try:
        paces = plan_generator.calculate_paces(profile)
        if paces:
            user_store.update_profile(chat_id, {"paces": paces})
    except Exception:
        pass

    dist_label = profile.get("race_distance", "").replace("_", " ").title()
    telegram_client.send_message(
        chat_id,
        f"Goal updated to *{new_goal}* for your {dist_label}. ✅\n\n"
        f"Pace zones recalculated — type /pace to see them.\n\n"
        f"Want me to regenerate your training plan with the new goal? "
        f"Just say so and I'll get started."
    )
    logger.info(f"Goal review accepted: {chat_id} updated goal to {new_goal}")


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
