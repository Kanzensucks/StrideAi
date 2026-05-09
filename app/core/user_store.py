"""Per-user data access layer.

Every user's data lives under DATA_DIR/users/<chat_id>/
Files: profile.json, plan.json, strava_tokens.json,
       chat_history.jsonl, memory.json, onboarding_state.json
"""

import json
import logging
import os
import shutil

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", ".")
MAX_USERS = int(os.environ.get("MAX_USERS", "50"))

_BETA_FULL_MSG = (
    "Thanks for your interest in StrideAI! The beta is currently full (50 athletes). "
    "Reply with your email and I'll add you to the waitlist for the next cohort."
)


def _user_dir(chat_id: str) -> str:
    return os.path.join(DATA_DIR, "users", str(chat_id))


def _ensure_user_dir(chat_id: str) -> str:
    path = _user_dir(chat_id)
    os.makedirs(path, exist_ok=True)
    return path


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ─────────────────────────────────────────────────────────────
# Signup / user management
# ─────────────────────────────────────────────────────────────

def count_users() -> int:
    """Count how many users have started onboarding (have a user dir)."""
    users_dir = os.path.join(DATA_DIR, "users")
    if not os.path.isdir(users_dir):
        return 0
    return sum(1 for d in os.scandir(users_dir) if d.is_dir())


def is_at_capacity() -> bool:
    return count_users() >= MAX_USERS


def user_exists(chat_id: str) -> bool:
    return os.path.isdir(_user_dir(chat_id))


def create_user(chat_id: str) -> bool:
    """Create a user directory. Returns False if at capacity and user doesn't exist."""
    if not user_exists(chat_id) and is_at_capacity():
        return False
    _ensure_user_dir(chat_id)
    return True


def beta_full_message() -> str:
    return _BETA_FULL_MSG


# ─────────────────────────────────────────────────────────────
# Profile
# ─────────────────────────────────────────────────────────────

def get_profile(chat_id: str) -> dict:
    path = os.path.join(_user_dir(chat_id), "profile.json")
    return _read_json(path, {})


def save_profile(chat_id: str, profile: dict) -> None:
    _ensure_user_dir(chat_id)
    path = os.path.join(_user_dir(chat_id), "profile.json")
    _write_json(path, profile)


def update_profile(chat_id: str, updates: dict) -> dict:
    profile = get_profile(chat_id)
    profile.update(updates)
    save_profile(chat_id, profile)
    return profile


def is_onboarded(chat_id: str) -> bool:
    profile = get_profile(chat_id)
    return profile.get("onboarding_complete", False)


# ─────────────────────────────────────────────────────────────
# Onboarding state
# ─────────────────────────────────────────────────────────────

def get_onboarding_state(chat_id: str) -> dict:
    path = os.path.join(_user_dir(chat_id), "onboarding_state.json")
    return _read_json(path, {"step": 0, "data": {}})


def save_onboarding_state(chat_id: str, state: dict) -> None:
    _ensure_user_dir(chat_id)
    path = os.path.join(_user_dir(chat_id), "onboarding_state.json")
    _write_json(path, state)


# ─────────────────────────────────────────────────────────────
# Plan
# ─────────────────────────────────────────────────────────────

def get_plan(chat_id: str) -> dict:
    path = os.path.join(_user_dir(chat_id), "plan.json")
    return _read_json(path, {})


def save_plan(chat_id: str, plan: dict) -> None:
    _ensure_user_dir(chat_id)
    path = os.path.join(_user_dir(chat_id), "plan.json")
    _write_json(path, plan)


def get_current_week_plan(chat_id: str) -> dict | None:
    """Return the plan dict for the current week, or None if not found."""
    from datetime import datetime

    plan = get_plan(chat_id)
    if not plan or "weeks" not in plan:
        return None

    today = datetime.now().date()
    first_week = None
    for week in plan["weeks"]:
        start = week.get("start_date")
        end = week.get("end_date")
        if start and end:
            try:
                w_start = datetime.strptime(start, "%Y-%m-%d").date()
                w_end = datetime.strptime(end, "%Y-%m-%d").date()
                if first_week is None:
                    first_week = week
                if w_start <= today <= w_end:
                    return week
                # If today is before the plan starts, return week 1
                if today < w_start and first_week is not None:
                    return first_week
            except ValueError:
                continue
    # Fallback: return first week
    return first_week


def get_week_plan(chat_id: str, week_number: int) -> dict | None:
    """Return plan for a specific week number (1-indexed)."""
    plan = get_plan(chat_id)
    if not plan or "weeks" not in plan:
        return None
    weeks = plan["weeks"]
    if 1 <= week_number <= len(weeks):
        return weeks[week_number - 1]
    return None


def get_todays_session(chat_id: str) -> dict:
    """Return today's planned session, or rest if no plan."""
    from datetime import datetime
    week = get_current_week_plan(chat_id)
    if not week:
        return {"type": "rest", "notes": "No plan found"}

    today_name = datetime.now().strftime("%a")  # "Mon", "Tue", etc.
    for session in week.get("sessions", []):
        if session.get("day", "").startswith(today_name):
            return session
    return {"type": "rest"}


def update_week_sessions(chat_id: str, week_number: int, updated_sessions: list) -> None:
    """Overwrite sessions for a specific week (used by plan adjuster)."""
    plan = get_plan(chat_id)
    if not plan or "weeks" not in plan:
        return
    weeks = plan["weeks"]
    if 1 <= week_number <= len(weeks):
        weeks[week_number - 1]["sessions"] = updated_sessions
        save_plan(chat_id, plan)


def weeks_to_race(chat_id: str) -> int:
    """Return number of weeks until race date."""
    from datetime import datetime
    profile = get_profile(chat_id)
    race_date_str = profile.get("race_date", "")
    if not race_date_str:
        return 0
    try:
        race_dt = datetime.strptime(race_date_str, "%Y-%m-%d")
        delta = (race_dt - datetime.now()).days
        return max(0, delta // 7)
    except ValueError:
        return 0


# ─────────────────────────────────────────────────────────────
# Strava tokens
# ─────────────────────────────────────────────────────────────

def get_strava_tokens(chat_id: str) -> dict:
    path = os.path.join(_user_dir(chat_id), "strava_tokens.json")
    return _read_json(path, {})


def save_strava_tokens(chat_id: str, tokens: dict) -> None:
    _ensure_user_dir(chat_id)
    path = os.path.join(_user_dir(chat_id), "strava_tokens.json")
    _write_json(path, tokens)


def has_strava(chat_id: str) -> bool:
    tokens = get_strava_tokens(chat_id)
    return bool(tokens.get("access_token"))


# ─────────────────────────────────────────────────────────────
# Chat history
# ─────────────────────────────────────────────────────────────

def append_chat_message(chat_id: str, role: str, content: str) -> None:
    """Append a message to the user's chat history (JSONL)."""
    _ensure_user_dir(chat_id)
    path = os.path.join(_user_dir(chat_id), "chat_history.jsonl")
    entry = json.dumps({"role": role, "content": content}) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)


def get_recent_chat(chat_id: str, max_messages: int = 20) -> list:
    """Return the last N messages from chat history."""
    path = os.path.join(_user_dir(chat_id), "chat_history.jsonl")
    messages = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return messages[-max_messages:]


def get_today_chat(chat_id: str) -> list:
    """Return messages from today only (for digest)."""
    import time
    from datetime import datetime

    path = os.path.join(_user_dir(chat_id), "chat_history.jsonl")
    messages = []
    today = datetime.now().date()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        msg = json.loads(line)
                        ts = msg.get("timestamp")
                        if ts:
                            msg_date = datetime.fromtimestamp(ts).date()
                            if msg_date == today:
                                messages.append(msg)
                        else:
                            messages.append(msg)
                    except json.JSONDecodeError:
                        pass
    except FileNotFoundError:
        pass
    return messages


# ─────────────────────────────────────────────────────────────
# Memory (coach notes / long-term context)
# ─────────────────────────────────────────────────────────────

def get_memory(chat_id: str) -> dict:
    path = os.path.join(_user_dir(chat_id), "memory.json")
    return _read_json(path, {"notes": [], "updated_at": None})


def save_memory(chat_id: str, memory: dict) -> None:
    _ensure_user_dir(chat_id)
    path = os.path.join(_user_dir(chat_id), "memory.json")
    _write_json(path, memory)


def append_memory_note(chat_id: str, note: str) -> None:
    from datetime import datetime
    memory = get_memory(chat_id)
    memory.setdefault("notes", []).append({
        "note": note,
        "added_at": datetime.now().isoformat(),
    })
    memory["updated_at"] = datetime.now().isoformat()
    if len(memory["notes"]) > 50:
        memory["notes"] = memory["notes"][-50:]
    save_memory(chat_id, memory)


def get_memory_text(chat_id: str) -> str:
    memory = get_memory(chat_id)
    notes = memory.get("notes", [])
    if not notes:
        return ""
    return "\n".join(f"- {n['note']}" for n in notes[-20:])


# ─────────────────────────────────────────────────────────────
# Weekly report storage
# ─────────────────────────────────────────────────────────────

def get_last_weekly_report(chat_id: str) -> str:
    path = os.path.join(_user_dir(chat_id), "last_weekly_report.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def save_weekly_report(chat_id: str, report: str) -> None:
    _ensure_user_dir(chat_id)
    path = os.path.join(_user_dir(chat_id), "last_weekly_report.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)


# ─────────────────────────────────────────────────────────────
# Scheduler state (last-processed activity per user)
# ─────────────────────────────────────────────────────────────

def get_last_activity_id(chat_id: str):
    path = os.path.join(_user_dir(chat_id), "last_activity.json")
    data = _read_json(path, {})
    return data.get("last_activity_id")


def save_last_activity_id(chat_id: str, activity_id) -> None:
    _ensure_user_dir(chat_id)
    path = os.path.join(_user_dir(chat_id), "last_activity.json")
    _write_json(path, {"last_activity_id": activity_id})


def get_scheduler_state(chat_id: str) -> dict:
    path = os.path.join(_user_dir(chat_id), "scheduler_state.json")
    return _read_json(path, {})


def save_scheduler_state(chat_id: str, state: dict) -> None:
    _ensure_user_dir(chat_id)
    path = os.path.join(_user_dir(chat_id), "scheduler_state.json")
    _write_json(path, state)


# ─────────────────────────────────────────────────────────────
# /forgetme — wipe everything
# ─────────────────────────────────────────────────────────────

def delete_user(chat_id: str) -> None:
    """Delete all data for a user. Irreversible."""
    user_path = _user_dir(chat_id)
    if os.path.isdir(user_path):
        shutil.rmtree(user_path)
        logger.info(f"Deleted all data for user {chat_id}")


def all_user_ids() -> list:
    """Return list of all chat_id strings (for scheduler iteration)."""
    users_dir = os.path.join(DATA_DIR, "users")
    if not os.path.isdir(users_dir):
        return []
    return [d.name for d in os.scandir(users_dir) if d.is_dir()]
