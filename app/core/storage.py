"""Thin wrapper — all per-user data operations are in user_store."""

from app.core.user_store import (
    get_profile, save_profile, update_profile,
    get_plan, save_plan, get_current_week_plan, get_todays_session,
    get_strava_tokens, save_strava_tokens, has_strava,
    append_chat_message, get_recent_chat, get_today_chat,
    get_memory, save_memory, append_memory_note, get_memory_text,
    get_last_weekly_report, save_weekly_report,
    get_last_activity_id, save_last_activity_id,
    get_scheduler_state, save_scheduler_state,
    all_user_ids,
)


def get_claude_messages(chat_id: str, max_messages: int = 20) -> list:
    history = get_recent_chat(chat_id, max_messages)
    return [{"role": m["role"], "content": m["content"]} for m in history]
