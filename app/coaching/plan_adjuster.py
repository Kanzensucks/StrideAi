"""Plan adjustment engine — three paths:
  1. Chat NL: user types "move long run to Sunday" → Sonnet rewrites affected sessions
  2. Button: user taps Done/Skip/Easier/Harder/Swap on a morning reminder
  3. Auto-adjust: weekly check via scheduler if sessions missed or HR drift detected
"""

import json
import logging
import os
from datetime import datetime

import anthropic

from app.core import user_store
from app.coaching import prompts as coach_prompts

logger = logging.getLogger(__name__)

MODEL_SONNET = "claude-sonnet-4-20250514"
MODEL_OPUS = "claude-opus-4-20250514"


# ─── Path 1: Chat NL adjustment ──────────────────────────────

def apply_chat_adjustment(chat_id: str, athlete_request: str) -> str:
    """Detect and apply a plan change from a natural-language request.

    Returns a plain-text confirmation message to send to the user.
    Returns empty string if the message isn't a plan adjustment request.
    """
    profile = user_store.get_profile(chat_id)
    week = user_store.get_current_week_plan(chat_id)
    if not week:
        return ""

    # Quick keyword filter before burning tokens
    keywords = [
        "move", "swap", "shift", "change", "reschedule", "skip", "rest",
        "easier", "harder", "longer", "shorter", "cancel", "drop", "add",
        "miss", "can't", "cannot", "won't be", "busy", "adjust", "modify",
    ]
    lower = athlete_request.lower()
    if not any(k in lower for k in keywords):
        return ""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system_prompt = coach_prompts.ADJUSTMENT_SYSTEM_PROMPT
    user_msg = coach_prompts.build_adjustment_user_message(
        athlete_request=athlete_request,
        current_week_plan=week,
        profile=profile,
    )

    response = client.messages.create(
        model=MODEL_SONNET,
        max_tokens=1500,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = response.content[0].text.strip()

    # Parse the JSON response from Claude
    try:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        message = result.get("message", "")
        updated_sessions = result.get("updated_sessions")

        if updated_sessions and week.get("week"):
            user_store.update_week_sessions(chat_id, week["week"], updated_sessions)
            logger.info(f"Plan adjusted for {chat_id} via chat NL")

        return message
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Plan adjustment parse failed for {chat_id}: {e}")
        return ""


# ─── Path 2: Button actions ──────────────────────────────────

def apply_button_action(chat_id: str, day: str, week_number: int, action: str) -> str:
    """Apply a quick-button action from a morning reminder.

    action: "done" | "skip" | "easier" | "harder" | "swap"
    Returns confirmation message.
    """
    profile = user_store.get_profile(chat_id)
    week = user_store.get_week_plan(chat_id, week_number)
    if not week:
        return "Couldn't find that session in your plan."

    sessions = list(week.get("sessions", []))
    target_idx = next(
        (i for i, s in enumerate(sessions) if s.get("day", "").startswith(day)),
        None
    )
    if target_idx is None:
        return "Session not found for that day."

    session = sessions[target_idx]

    if action == "done":
        session["completed"] = True
        sessions[target_idx] = session
        user_store.update_week_sessions(chat_id, week_number, sessions)
        return "Marked as done. Good work."

    elif action == "skip":
        session["type"] = "rest"
        session["completed"] = False
        session["notes"] = "Skipped."
        sessions[target_idx] = session
        user_store.update_week_sessions(chat_id, week_number, sessions)
        return f"{day}'s session marked as rest. Rest well."

    elif action == "swap":
        # Find the next rest day and swap
        rest_idx = next(
            (i for i, s in enumerate(sessions)
             if s.get("type") == "rest" and i != target_idx),
            None
        )
        if rest_idx is not None:
            sessions[target_idx], sessions[rest_idx] = sessions[rest_idx], sessions[target_idx]
            user_store.update_week_sessions(chat_id, week_number, sessions)
            new_day = sessions[target_idx].get("day", "another day")
            return f"Swapped. Session moved to {new_day}."
        return "No rest day available to swap with this week."

    elif action in ("easier", "harder"):
        return _soften_or_harden_session(
            chat_id, session, sessions, target_idx, week_number, action, profile
        )

    return "Action not recognised."


def _soften_or_harden_session(
    chat_id: str, session: dict, sessions: list,
    idx: int, week_number: int, action: str, profile: dict
) -> str:
    """Use Sonnet to reduce or increase a single session's load."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    units = profile.get("units", "km")
    direction = "10-20% easier (shorter distance, slower pace, or both)" if action == "easier" else "10-15% harder (slightly longer or add a short quality segment)"

    response = client.messages.create(
        model=MODEL_SONNET,
        max_tokens=500,
        system=(
            "You are a running coach modifying a single training session. "
            "Return JSON only: {\"updated_session\": {...}, \"message\": \"...\"}"
        ),
        messages=[{"role": "user", "content": (
            f"Make this session {direction}:\n{json.dumps(session, indent=2)}\n"
            f"Units: {units}. Keep day, type, is_key_session. "
            f"Message should be 1 plain sentence confirming the change."
        )}],
    )

    try:
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        updated = result.get("updated_session", session)
        message = result.get("message", f"Session adjusted to be {action}.")
        sessions[idx] = updated
        user_store.update_week_sessions(chat_id, week_number, sessions)
        return message
    except Exception as e:
        logger.warning(f"Session adjustment failed: {e}")
        return f"Couldn't adjust that session automatically — just ease off the pace by feel."


# ─── Path 3: Auto-adjust (weekly, from scheduler) ────────────

def auto_adjust_from_strava(chat_id: str, last_week_activities: list) -> str:
    """Check if the athlete is underperforming and soften upcoming weeks.

    Called after the weekly report. Returns a message if adjustment was made,
    empty string if no change needed.
    """
    if not last_week_activities:
        return ""

    profile = user_store.get_profile(chat_id)
    plan = user_store.get_plan(chat_id)
    if not plan or "weeks" not in plan:
        return ""

    current_week = user_store.get_current_week_plan(chat_id)
    if not current_week:
        return ""

    planned_sessions = [s for s in current_week.get("sessions", []) if s.get("type") != "rest"]
    completed = len(last_week_activities)
    missed = max(0, len(planned_sessions) - completed)

    if missed < 3:
        return ""  # Only auto-adjust if ≥3 sessions missed

    current_week_num = current_week.get("week", 1)
    weeks = plan["weeks"]
    adjusted_count = 0

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    units = profile.get("units", "km")

    for w in weeks:
        if w.get("week", 0) in (current_week_num + 1, current_week_num + 2):
            response = client.messages.create(
                model=MODEL_SONNET,
                max_tokens=2000,
                system=(
                    "You are a running coach softening an upcoming training week due to athlete fatigue. "
                    "Reduce all session volumes by 15-20%. Remove or shorten quality sessions. "
                    "Return only the modified sessions array as JSON."
                ),
                messages=[{"role": "user", "content": (
                    f"Soften this week (athlete missed {missed} sessions last week):\n"
                    f"{json.dumps(w.get('sessions', []), indent=2)}\nUnits: {units}"
                )}],
            )
            try:
                raw = response.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                updated_sessions = json.loads(raw.strip())
                user_store.update_week_sessions(chat_id, w["week"], updated_sessions)
                adjusted_count += 1
            except Exception as e:
                logger.warning(f"Auto-adjust week {w.get('week')} failed: {e}")

    if adjusted_count > 0:
        return (
            f"I've adjusted the next {adjusted_count} week(s) — you missed {missed} sessions "
            f"last week so I've softened upcoming load. Stay consistent and we'll build back up."
        )
    return ""
