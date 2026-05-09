"""Onboarding state machine — Runna-style interview in Telegram.

Steps (persisted in onboarding_state.json):
  0  → welcome + units
  1  → goal race name + date
  2  → race distance
  3  → goal time
  4  → days/week
  5  → long run day
  6  → experience level
  7  → current weekly mileage + longest recent run
  8  → recent PRs
  9  → cross-training prefs
  10 → injury history
  11 → strava connect (sends OAuth link — plan generated after callback)
  99 → complete
"""

import logging
import os
from datetime import datetime

from app.integrations import telegram_client
from app.core import user_store

logger = logging.getLogger(__name__)

PUBLIC_DOMAIN = os.environ.get("PUBLIC_DOMAIN", "localhost")

# ─── Step definitions ─────────────────────────────────────────

STEPS = {
    0: {
        "question": (
            "Welcome to StrideAI — your personal AI running coach.\n\n"
            "Let's build your training plan. First question:\n\n"
            "Do you train in kilometres or miles?"
        ),
        "buttons": [["Kilometres", "km"], ["Miles", "mi"]],
        "field": "units",
        "type": "button",
    },
    1: {
        "question": (
            "What race are you training for?\n\n"
            "Reply with the race name and date, like:\n"
            "Gold Coast Marathon, July 5 2026"
        ),
        "field": None,  # custom parsing
        "type": "text",
    },
    2: {
        "question": "What distance is the race?",
        "buttons": [
            ["5k", "5k"],
            ["10k", "10k"],
            ["Half Marathon", "half_marathon"],
            ["Marathon", "marathon"],
        ],
        "field": "race_distance",
        "type": "button",
    },
    3: {
        "question": (
            "What's your goal time?\n\n"
            "Type it in h:mm:ss format (e.g. 4:00:00 for a marathon) "
            "or just say \"finish\" if completion is the goal."
        ),
        "field": "goal_time",
        "type": "text",
    },
    4: {
        "question": "How many days per week can you train?",
        "buttons": [["3 days", "3"], ["4 days", "4"], ["5 days", "5"], ["6 days", "6"]],
        "field": "days_per_week",
        "type": "button",
        "transform": int,
    },
    5: {
        "question": "Which day do you prefer for your long run?",
        "buttons": [
            ["Saturday", "Saturday"],
            ["Sunday", "Sunday"],
            ["Friday", "Friday"],
            ["Other (tell me)", "other"],
        ],
        "field": "long_run_day",
        "type": "button",
    },
    6: {
        "question": "What's your running experience level?",
        "buttons": [
            ["Beginner (< 1 year)", "beginner"],
            ["Intermediate (1–3 years)", "intermediate"],
            ["Advanced (3+ years)", "advanced"],
        ],
        "field": "experience_level",
        "type": "button",
    },
    7: {
        "question": (
            "Two quick fitness questions:\n\n"
            "1. What's your current weekly running volume? (e.g. 40km or 25mi)\n"
            "2. What's the longest run you've done in the last 6 weeks? (e.g. 18km or 11mi)\n\n"
            "Reply like: 40km, 18km"
        ),
        "field": None,  # custom parsing
        "type": "text",
    },
    8: {
        "question": (
            "Do you have any recent race times or PRs? These help calibrate your training paces.\n\n"
            "Reply with any you have, e.g: 5k 22:30, 10k 48:00, HM 1:54:50\n\n"
            "Or just say \"skip\" if you don't have any."
        ),
        "field": None,  # custom parsing
        "type": "text",
    },
    9: {
        "question": (
            "What cross-training do you have access to? (Select all that apply)\n\n"
            "Type the numbers, e.g: 1 3 or just \"none\""
            "\n\n1. Bike (road or indoor)\n2. Swimming\n3. Gym / strength\n4. None"
        ),
        "field": None,  # custom parsing
        "type": "text",
    },
    10: {
        "question": (
            "Any current injuries or niggles I should know about?\n\n"
            "Be honest — I'll build around them. Or just say \"none\"."
        ),
        "field": "injury_notes",
        "type": "text",
    },
    11: {
        "question": None,  # built dynamically — sends Strava OAuth link
        "field": None,
        "type": "strava",
    },
}

TOTAL_STEPS = 12  # 0–11


# ─── Main handler ─────────────────────────────────────────────

def handle_onboarding_message(chat_id: str, text: str, callback_data: str = None) -> bool:
    """Process an incoming message during onboarding.

    Returns True if onboarding is still in progress, False if complete.
    callback_data is set when the message came from an inline button press.
    """
    state = user_store.get_onboarding_state(chat_id)
    step = state.get("step", 0)
    data = state.get("data", {})

    if step > 11:
        return False  # Already done

    step_def = STEPS.get(step)
    if step_def is None:
        return False

    # Process the incoming answer
    answer = callback_data if callback_data else text.strip()

    if step == 0:
        if answer in ("km", "mi"):
            data["units"] = answer
        else:
            _send_question(chat_id, step)
            return True

    elif step == 1:
        parsed = _parse_race_name_and_date(answer)
        if parsed:
            data["race_name"] = parsed["name"]
            data["race_date"] = parsed["date"]
        else:
            telegram_client.send_message(
                chat_id,
                "I couldn't quite parse that. Try: Gold Coast Marathon, July 5 2026"
            )
            return True

    elif step == 2:
        if answer in ("5k", "10k", "half_marathon", "marathon"):
            data["race_distance"] = answer
        else:
            _send_question(chat_id, step)
            return True

    elif step == 3:
        data["goal_time"] = answer

    elif step == 4:
        if answer.isdigit() and 3 <= int(answer) <= 6:
            data["days_per_week"] = int(answer)
        else:
            _send_question(chat_id, step)
            return True

    elif step == 5:
        data["long_run_day"] = answer

    elif step == 6:
        if answer in ("beginner", "intermediate", "advanced"):
            data["experience_level"] = answer
        else:
            _send_question(chat_id, step)
            return True

    elif step == 7:
        parsed = _parse_volume_and_longest(answer, data.get("units", "km"))
        if parsed:
            data.update(parsed)
        else:
            telegram_client.send_message(
                chat_id,
                "Couldn't parse that. Try: 40km, 18km (or 25mi, 11mi)"
            )
            return True

    elif step == 8:
        if answer.lower() == "skip":
            data["prs"] = {}
        else:
            data["prs"] = _parse_prs(answer)

    elif step == 9:
        data["cross_training_prefs"] = _parse_cross_training(answer)

    elif step == 10:
        data["injury_notes"] = answer if answer.lower() != "none" else "none reported"

    # Advance step
    step += 1
    state["step"] = step
    state["data"] = data
    user_store.save_onboarding_state(chat_id, state)

    # Send next step or finalise
    if step == 11:
        _send_strava_step(chat_id, data)
    elif step >= 12:
        _finalise_onboarding(chat_id, data)
        return False
    else:
        _send_question(chat_id, step)

    return True


def start_onboarding(chat_id: str) -> None:
    """Initialise onboarding for a new user and send step 0."""
    state = {"step": 0, "data": {}}
    user_store.save_onboarding_state(chat_id, state)
    _send_question(chat_id, 0)


def resume_onboarding(chat_id: str) -> None:
    """Re-send the current step question (e.g. on /start with existing state)."""
    state = user_store.get_onboarding_state(chat_id)
    step = state.get("step", 0)
    if step < 11:
        _send_question(chat_id, step)
    elif step == 11:
        _send_strava_step(chat_id, state.get("data", {}))


def complete_onboarding_after_strava(chat_id: str) -> None:
    """Called from the Strava OAuth callback once tokens are saved."""
    state = user_store.get_onboarding_state(chat_id)
    data = state.get("data", {})
    _finalise_onboarding(chat_id, data)


# ─── Internal helpers ──────────────────────────────────────────

def _send_question(chat_id: str, step: int) -> None:
    step_def = STEPS[step]
    question = step_def["question"]
    buttons = step_def.get("buttons")

    progress = f"({step + 1}/{TOTAL_STEPS}) "

    if buttons:
        keyboard = [[{"text": label, "callback_data": value}] for label, value in buttons]
        telegram_client.send_message_with_keyboard(chat_id, progress + question, keyboard)
    else:
        telegram_client.send_message(chat_id, progress + question)


def _send_strava_step(chat_id: str, data: dict) -> None:
    """Send the Strava connect link."""
    oauth_url = f"https://{PUBLIC_DOMAIN}/strava/connect?chat_id={chat_id}"
    msg = (
        f"(12/{TOTAL_STEPS}) Almost done!\n\n"
        "Connect your Strava so I can automatically analyse every run and build your "
        "personalised plan.\n\n"
        f"Tap here to connect: {oauth_url}\n\n"
        "Once connected, I'll generate your full training plan. Takes about 10 seconds."
    )
    telegram_client.send_message(chat_id, msg)


def _finalise_onboarding(chat_id: str, data: dict) -> None:
    """Save profile, mark onboarding complete, trigger plan generation."""
    from app.utils.timezone_helper import guess_timezone
    from app.coaching import plan_generator

    data["onboarding_complete"] = True
    data["created_at"] = datetime.now().isoformat()

    try:
        tz = guess_timezone(data.get("race_name", ""))
    except Exception:
        tz = "UTC"
    data.setdefault("timezone", tz)

    user_store.save_profile(chat_id, data)

    try:
        paces = plan_generator.calculate_paces(data)
        user_store.update_profile(chat_id, {"paces": paces})
    except Exception as e:
        logger.warning(f"Pace calculation failed for {chat_id}: {e}")

    telegram_client.send_message(
        chat_id,
        "All set! Building your personalised training plan now — give me about 10 seconds..."
    )

    import threading
    from app import pipelines
    threading.Thread(
        target=pipelines.generate_and_send_plan,
        args=(chat_id,),
        daemon=True,
    ).start()


# ─── Parsers ──────────────────────────────────────────────────

def _parse_race_name_and_date(text: str) -> dict | None:
    """Parse race name + date from free-form text.

    Handles many orderings:
      - Gold Coast Marathon, July 5 2026
      - July 5 2026, Gold Coast Marathon
      - Nike Melbourne Marathon October 11 2026
      - Gold Coast Marathon July 5 2026
    """
    import re

    # Date patterns to try extracting from anywhere in the string
    date_patterns = [
        r"(\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{4})",
        r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})",
        r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}\s+\d{4})",
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{1,2}/\d{1,2}/\d{4})",
    ]

    formats = [
        "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
        "%B %dst %Y", "%B %dnd %Y", "%B %drd %Y", "%B %dth %Y",
        "%d %B, %Y", "%B %d, %Y", "%b %d, %Y",
        "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
    ]

    text = text.strip()

    for pattern in date_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            date_str = m.group(1).strip().rstrip(",")
            # Clean ordinal suffixes
            date_str_clean = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", date_str)
            for fmt in formats:
                try:
                    dt = datetime.strptime(date_str_clean.strip(), fmt)
                    # Name is everything except the matched date
                    name = text[:m.start()].strip().strip(",").strip()
                    if not name:
                        name = text[m.end():].strip().strip(",").strip()
                    if not name:
                        name = "Goal Race"
                    return {"name": name, "date": dt.strftime("%Y-%m-%d")}
                except ValueError:
                    continue

    return None


def _parse_volume_and_longest(text: str, units: str) -> dict | None:
    """Parse '40km, 18km' or '25mi, 11mi' into weekly_km and longest_recent_run_km."""
    import re

    numbers = re.findall(r"(\d+(?:\.\d+)?)\s*(?:km|mi|k)?", text.lower())
    if len(numbers) < 2:
        numbers = re.findall(r"(\d+(?:\.\d+)?)", text)

    if len(numbers) < 2:
        return None

    try:
        weekly = float(numbers[0])
        longest = float(numbers[1])

        if "mi" in text.lower() or units == "mi":
            weekly = round(weekly * 1.60934, 1)
            longest = round(longest * 1.60934, 1)

        return {
            "current_weekly_km": weekly,
            "longest_recent_run_km": longest,
        }
    except (ValueError, IndexError):
        return None


def _parse_prs(text: str) -> dict:
    """Parse PR text like '5k 22:30, 10k 48:00, HM 1:54:50' into a dict."""
    import re
    prs = {}

    patterns = [
        (r"5\s*k[^\d]*(\d+:\d{2}(?::\d{2})?)", "5k"),
        (r"10\s*k[^\d]*(\d+:\d{2}(?::\d{2})?)", "10k"),
        (r"(?:hm|half[^\d]*marathon|half)[^\d]*(\d+:\d{2}(?::\d{2})?)", "half_marathon"),
        (r"(?:fm|full[^\d]*marathon|marathon)[^\d]*(\d+:\d{2}(?::\d{2})?)", "marathon"),
    ]

    text_lower = text.lower()
    for pattern, key in patterns:
        m = re.search(pattern, text_lower)
        if m:
            prs[key] = m.group(1)

    return prs


def _parse_cross_training(text: str) -> list:
    """Parse cross-training preferences from numbered or keyword input."""
    text_lower = text.lower()
    if "none" in text_lower or text.strip() == "4":
        return []

    prefs = []
    if "1" in text or "bike" in text_lower or "cycling" in text_lower:
        prefs.append("bike")
    if "2" in text or "swim" in text_lower:
        prefs.append("swim")
    if "3" in text or "gym" in text_lower or "strength" in text_lower or "weight" in text_lower:
        prefs.append("gym")

    return prefs
