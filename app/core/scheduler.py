"""Multi-user scheduler — per-user timezone-aware jobs."""

import logging
import threading
import time
from datetime import datetime

import pytz

from app.core import user_store

logger = logging.getLogger(__name__)

POLL_INTERVAL = 300  # 5 minutes


def _now_in_tz(tz_name: str) -> datetime:
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.UTC
    return datetime.now(tz)


def _poll_strava_for_user(chat_id: str) -> None:
    try:
        from app.integrations import strava_client
        from app import pipelines

        if not user_store.has_strava(chat_id):
            return
        activities = strava_client.get_activities(chat_id, per_page=5, max_pages=1)
        if not activities:
            return
        latest = activities[0]
        latest_id = latest["id"]
        last_id = user_store.get_last_activity_id(chat_id)
        if last_id == latest_id:
            return
        user_store.save_last_activity_id(chat_id, latest_id)
        if last_id is None:
            logger.info(f"Scheduler: First poll for {chat_id} — saving baseline")
            return
        logger.info(f"Scheduler: New activity {latest_id} for {chat_id}")
        pipelines.daily_analysis(chat_id, latest_id)
    except Exception as e:
        logger.error(f"Scheduler: Strava poll failed for {chat_id}: {e}")


def _should_run_job(chat_id: str, job_key: str, hour: int,
                    minute_range: tuple, weekday: int = None) -> bool:
    profile = user_store.get_profile(chat_id)
    tz_name = profile.get("timezone", "UTC")
    now = _now_in_tz(tz_name)
    if weekday is not None and now.weekday() != weekday:
        return False
    if not (now.hour == hour and minute_range[0] <= now.minute <= minute_range[1]):
        return False
    state = user_store.get_scheduler_state(chat_id)
    last_run = state.get(job_key)
    today_str = now.strftime("%Y-%m-%d")
    if job_key == "weekly_report":
        week_str = now.strftime("%Y-W%W")
        if last_run == week_str:
            return False
        state[job_key] = week_str
    else:
        if last_run == today_str:
            return False
        state[job_key] = today_str
    user_store.save_scheduler_state(chat_id, state)
    return True


def start_scheduler() -> threading.Thread:
    def run():
        from app import pipelines

        last_poll_times = {}
        while True:
            now_ts = time.time()
            for chat_id in user_store.all_user_ids():
                profile = user_store.get_profile(chat_id)
                if not profile.get("onboarding_complete"):
                    continue

                if now_ts - last_poll_times.get(chat_id, 0) >= POLL_INTERVAL:
                    last_poll_times[chat_id] = now_ts
                    threading.Thread(target=_poll_strava_for_user,
                                     args=(chat_id,), daemon=True).start()

                if _should_run_job(chat_id, "morning_reminder", 6, (0, 1)):
                    threading.Thread(target=pipelines.morning_reminder,
                                     args=(chat_id,), daemon=True).start()

                if _should_run_job(chat_id, "daily_digest", 22, (0, 1)):
                    threading.Thread(target=pipelines.daily_digest,
                                     args=(chat_id,), daemon=True).start()

                if _should_run_job(chat_id, "weekly_report", 20, (30, 31), weekday=6):
                    threading.Thread(target=pipelines.weekly_report,
                                     args=(chat_id,), daemon=True).start()

            time.sleep(30)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    logger.info("Scheduler started — polling every 5min, reminders/digest/weekly per user TZ")
    return thread
