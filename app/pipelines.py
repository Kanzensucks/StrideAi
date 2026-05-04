"""Five coaching pipelines — all take chat_id for multi-user support.

  0. handle_chat         — conversational coach (Haiku)
  1. daily_analysis      — post-run Strava feedback (Sonnet)
  2. weekly_report       — Sunday evening recap (Opus)
  3. daily_digest        — 10pm memory extraction (Sonnet)
  4. generate_and_send_plan — post-onboarding plan generation (Opus)
  5. morning_reminder    — 6am session prompt (template + Haiku for tone)
"""

import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta

import anthropic

from app.integrations import strava_client
from app.integrations import telegram_client
from app.coaching import prompts as coach_prompts
from app.core import user_store
from app.core import storage
from app.coaching import plan_generator
from app.coaching import plan_adjuster

logger = logging.getLogger(__name__)

MODEL_OPUS = "claude-opus-4-20250514"
MODEL_SONNET = "claude-sonnet-4-20250514"
MODEL_HAIKU = "claude-haiku-4-5"


def _call_claude(system_prompt: str, user_message: str, model: str = MODEL_SONNET) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _call_claude_conversation(system_prompt: str, messages: list,
                               model: str = MODEL_HAIKU) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text


def _get_recent_activities_text(chat_id: str) -> str:
    """Fetch last 2 weeks of activities grouped by day."""
    try:
        two_weeks_ago = datetime.now() - timedelta(weeks=2)
        activities = strava_client.get_activities(
            chat_id,
            after_epoch=two_weeks_ago.timestamp(),
            per_page=50,
            max_pages=2,
        )
        activities.sort(key=lambda a: a["start_date_local"])
        if not activities:
            return "No activities in the last 2 weeks."

        profile = user_store.get_profile(chat_id)
        units = profile.get("units", "km")

        days = defaultdict(list)
        for a in activities:
            date_key = a["start_date_local"][:10]
            days[date_key].append(a)

        sections = []
        for date_key in sorted(days):
            day_activities = days[date_key]
            try:
                dt = datetime.fromisoformat(date_key)
                header = dt.strftime("%a %b %d")
            except ValueError:
                header = date_key
            lines = [f"  {strava_client.format_activity_line(a, units)}" for a in day_activities]
            sections.append(f"{header}:\n" + "\n".join(lines))

        return "\n\n".join(sections)
    except Exception as e:
        logger.error(f"Failed to fetch recent activities for {chat_id}: {e}")
        return "Could not fetch recent Strava data."


def _detect_session_mode(activity: dict) -> str:
    """Classify activity into easy/cross_train/strength/key/race."""
    sport = (activity.get("sport_type") or activity.get("type") or "").lower()
    name = (activity.get("name") or "").lower()

    if any(k in sport for k in ["ride", "bike", "cycl", "swim", "row", "ellip", "weight", "gym"]):
        return "cross_train"

    if any(k in sport for k in ["weight", "gym", "strength", "workout"]):
        return "strength"

    if "race" in name or "time trial" in name or activity.get("workout_type") in (1, 2):
        return "race"

    avg_hr = activity.get("average_heartrate", 0) or 0
    suffer = activity.get("suffer_score", 0) or 0
    distance_m = activity.get("distance", 0) or 0
    distance_km = distance_m / 1000

    if distance_km > 12:
        return "key"

    if avg_hr > 155 or suffer > 50:
        return "key"

    return "easy"


def _get_week_load(chat_id: str, week_start: datetime) -> dict:
    """Get total run/bike/key sessions for the current week."""
    try:
        activities = strava_client.get_activities(
            chat_id,
            after_epoch=week_start.timestamp(),
            per_page=50, max_pages=1,
        )
    except Exception:
        return {}

    profile = user_store.get_profile(chat_id)
    units = profile.get("units", "km")

    run_km = 0.0
    run_count = 0
    bike_minutes = 0.0
    key_done = 0

    for a in activities:
        sport = (a.get("sport_type") or a.get("type") or "").lower()
        dist_km = (a.get("distance") or 0) / 1000
        dur_min = (a.get("moving_time") or 0) / 60

        if "run" in sport:
            run_km += dist_km
            run_count += 1
            if _detect_session_mode(a) == "key":
                key_done += 1
        elif any(k in sport for k in ["ride", "bike", "cycl"]):
            bike_minutes += dur_min

    if units == "mi":
        run_display = round(run_km * 0.621371, 1)
    else:
        run_display = round(run_km, 1)

    return {
        "run_km": run_display,
        "run_count": run_count,
        "bike_hours": round(bike_minutes / 60, 1),
        "key_sessions_done": key_done,
    }


# ─────────────────────────────────────────────────────────────
# PIPELINE 0: CHATBOT
# ─────────────────────────────────────────────────────────────

PROGRESS_KEYWORDS = [
    "show progress", "my progress", "progress chart", "progress graph",
    "show chart", "show graph", "training chart", "weekly chart", "training load",
]


def handle_chat(chat_id: str, athlete_message: str, msg_timestamp=None) -> None:
    """Conversational coach — full history, live Strava context, plan awareness."""
    logger.info(f"Pipeline 0: Chat [{chat_id}] — {athlete_message[:60]}...")

    telegram_client.send_typing(chat_id)

    adjustment_msg = plan_adjuster.apply_chat_adjustment(chat_id, athlete_message)
    if adjustment_msg:
        storage.append_chat_message(chat_id, "user", athlete_message)
        storage.append_chat_message(chat_id, "assistant", adjustment_msg)
        telegram_client.send_message(chat_id, adjustment_msg)
        return

    storage.append_chat_message(chat_id, "user", athlete_message)

    if any(kw in athlete_message.lower() for kw in PROGRESS_KEYWORDS):
        try:
            from app.utils import charts
            profile = user_store.get_profile(chat_id)
            after = datetime.now() - timedelta(weeks=8)
            activities = strava_client.get_activities(chat_id, after_epoch=after.timestamp())
            chart_png = charts.generate_weekly_mileage_chart(chat_id, activities)
            telegram_client.send_photo(chat_id, chart_png, caption="Your last 8 weeks")
        except Exception as e:
            logger.error(f"Chart failed for {chat_id}: {e}")

    profile = user_store.get_profile(chat_id)
    recent_activities = _get_recent_activities_text(chat_id)
    weekly_report = user_store.get_last_weekly_report(chat_id)
    weeks_left = user_store.weeks_to_race(chat_id)

    system_prompt = coach_prompts.build_chat_system_prompt(profile)
    live_context = coach_prompts.build_chat_context(
        profile, recent_activities, weekly_report, weeks_left, msg_timestamp=msg_timestamp
    )

    memory_text = user_store.get_memory_text(chat_id)
    if memory_text:
        live_context += f"\n\nPERSISTENT COACH MEMORY:\n{memory_text}"

    full_system = system_prompt + "\n\n" + live_context

    stop_typing = threading.Event()

    def _keep_typing():
        while not stop_typing.is_set():
            telegram_client.send_typing(chat_id)
            stop_typing.wait(4)

    typing_thread = threading.Thread(target=_keep_typing, daemon=True)
    typing_thread.start()

    try:
        messages = storage.get_claude_messages(chat_id)
        reply = _call_claude_conversation(full_system, messages, MODEL_HAIKU)
    finally:
        stop_typing.set()

    storage.append_chat_message(chat_id, "assistant", reply)
    telegram_client.send_message(chat_id, reply)


# ─────────────────────────────────────────────────────────────
# PIPELINE 1: DAILY ANALYSIS
# ─────────────────────────────────────────────────────────────

def daily_analysis(chat_id: str, activity_id: int) -> str:
    """Post-run analysis triggered by Strava webhook."""
    logger.info(f"Pipeline 1: Daily analysis [{chat_id}] activity {activity_id}")

    time.sleep(25)

    telegram_client.send_typing(chat_id)

    profile = user_store.get_profile(chat_id)
    units = profile.get("units", "km")

    activity = strava_client.get_activity(chat_id, activity_id)
    mode = _detect_session_mode(activity)

    activity_line = strava_client.format_activity_line(activity, units)
    laps_text = strava_client.format_laps(activity, units)

    try:
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        todays = strava_client.get_activities(
            chat_id, after_epoch=today_start.timestamp(), per_page=10, max_pages=1
        )
        todays_text = "\n".join(strava_client.format_activity_line(a, units) for a in todays)
    except Exception:
        todays_text = activity_line

    try:
        stats = strava_client.get_athlete_stats(chat_id)
        recent_runs = stats.get("recent_run_totals", {})
        recent_rides = stats.get("recent_ride_totals", {})
        run_dist = recent_runs.get("distance", 0) / 1000
        run_count = recent_runs.get("count", 0)
        run_time = recent_runs.get("moving_time", 0)
        ride_dist = recent_rides.get("distance", 0) / 1000
        ride_time = recent_rides.get("moving_time", 0)

        if units == "mi":
            run_dist_d = round(run_dist * 0.621371, 1)
            ride_dist_d = round(ride_dist * 0.621371, 1)
        else:
            run_dist_d = round(run_dist, 1)
            ride_dist_d = round(ride_dist, 1)

        stats_text = (
            f"Last 4 weeks running: {run_count} runs, {run_dist_d}{units}, "
            f"{run_time // 3600}h {(run_time % 3600) // 60}m\n"
            f"Last 4 weeks cycling: {ride_dist_d}{units}, "
            f"{ride_time // 3600}h {(ride_time % 3600) // 60}m"
        )
    except Exception:
        stats_text = "4-week stats unavailable."

    week_start = datetime.now() - timedelta(days=datetime.now().weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0)
    week_load = _get_week_load(chat_id, week_start)

    try:
        activity_date = datetime.fromisoformat(
            activity["start_date_local"].replace("Z", "+00:00")
        )
    except Exception:
        activity_date = datetime.now()

    weeks_left = user_store.weeks_to_race(chat_id)
    recent_chat = storage.get_claude_messages(chat_id, max_messages=6)
    recent_chat_text = "\n".join(
        f"{m['role'].title()}: {m['content'][:200]}" for m in recent_chat[-4:]
    )

    system_prompt = coach_prompts.build_daily_system_prompt(profile)
    user_message = coach_prompts.build_daily_user_message(
        profile=profile,
        mode=mode,
        triggering_activity=activity_line,
        todays_activities_text=todays_text,
        stats_text=stats_text,
        activity_date=activity_date,
        weeks_to_race=weeks_left,
        week_load=week_load,
        recent_chat_text=recent_chat_text,
        triggering_activity_laps=laps_text,
    )

    coaching = _call_claude(system_prompt, user_message, MODEL_SONNET)
    telegram_client.send_message(chat_id, coaching)

    return coaching


# ─────────────────────────────────────────────────────────────
# PIPELINE 2: WEEKLY REPORT
# ─────────────────────────────────────────────────────────────

def weekly_report(chat_id: str) -> str:
    """Sunday evening weekly recap + next week plan."""
    logger.info(f"Pipeline 2: Weekly report [{chat_id}]")

    telegram_client.send_typing(chat_id)

    profile = user_store.get_profile(chat_id)
    units = profile.get("units", "km")

    eight_weeks_ago = datetime.now() - timedelta(weeks=8)
    try:
        activities = strava_client.get_activities(
            chat_id, after_epoch=eight_weeks_ago.timestamp(), per_page=200
        )
    except Exception:
        activities = []

    activities.sort(key=lambda a: a["start_date_local"])
    all_text = "\n".join(strava_client.format_activity_line(a, units) for a in activities)

    week_start = datetime.now() - timedelta(days=datetime.now().weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0)
    this_week = [
        a for a in activities
        if datetime.fromisoformat(a["start_date_local"][:19]) >= week_start
    ]
    current_text = "\n".join(strava_client.format_activity_line(a, units) for a in this_week)

    week_number = len([a for a in activities if
                       datetime.fromisoformat(a["start_date_local"][:19]) < week_start]) // 5 + 1
    date_range = f"{week_start.strftime('%b %d')} – {datetime.now().strftime('%b %d, %Y')}"

    current_week_plan = user_store.get_current_week_plan(chat_id)
    plan_snapshot = ""
    if current_week_plan:
        import json
        plan_snapshot = json.dumps(current_week_plan, indent=2)[:1000]

    system_prompt = coach_prompts.build_weekly_system_prompt(profile)
    user_message = coach_prompts.build_weekly_user_message(
        profile=profile,
        all_activities_text=all_text or "(no activities)",
        current_week_text=current_text or "(no activities this week)",
        week_number=week_number,
        date_range=date_range,
        plan_snapshot=plan_snapshot,
    )

    report = _call_claude(system_prompt, user_message, MODEL_OPUS)

    user_store.save_weekly_report(chat_id, report)
    telegram_client.send_message(chat_id, report)
    telegram_client.send_message(chat_id, coach_prompts.WEEKLY_FOLLOWUP_MESSAGE)

    adjustment_msg = plan_adjuster.auto_adjust_from_strava(chat_id, this_week)
    if adjustment_msg:
        telegram_client.send_message(chat_id, adjustment_msg)

    return report


# ─────────────────────────────────────────────────────────────
# PIPELINE 3: DAILY DIGEST
# ─────────────────────────────────────────────────────────────

def daily_digest(chat_id: str) -> str | None:
    """10pm nightly digest — extract important notes to memory."""
    logger.info(f"Pipeline 3: Daily digest [{chat_id}]")

    today_messages = user_store.get_today_chat(chat_id)
    if len(today_messages) < 2:
        return None

    profile = user_store.get_profile(chat_id)
    user_message = coach_prompts.build_digest_user_message(today_messages, profile)

    result = _call_claude(
        coach_prompts.DIGEST_SYSTEM_PROMPT, user_message, MODEL_SONNET
    )

    if result.strip() == "NOTHING_TO_SAVE":
        return None

    for line in result.strip().split("\n"):
        line = line.strip().lstrip("-• ").strip()
        if line:
            user_store.append_memory_note(chat_id, line)

    return result


# ─────────────────────────────────────────────────────────────
# PIPELINE 4: PLAN GENERATION (post-onboarding)
# ─────────────────────────────────────────────────────────────

def generate_and_send_plan(chat_id: str) -> None:
    """Generate full plan and send welcome summary to user."""
    logger.info(f"Pipeline 4: Generating plan [{chat_id}]")

    try:
        profile = user_store.get_profile(chat_id)
        plan = plan_generator.generate_plan(profile)
        user_store.save_plan(chat_id, plan)

        weeks = plan.get("total_weeks", len(plan.get("weeks", [])))
        race_name = profile.get("race_name", "your race")
        race_date = profile.get("race_date", "TBD")
        goal = profile.get("goal_time", "finish")
        dist = profile.get("race_distance", "").replace("_", " ").title()

        week1 = plan["weeks"][0] if plan.get("weeks") else {}
        sessions = week1.get("sessions", [])
        week1_lines = [
            f"{s['day']} — {s['type'].replace('_', ' ').title()}"
            + (f" {s.get('distance_km') or s.get('distance_mi', '')} {profile.get('units', 'km')}" if s.get('distance_km') or s.get('distance_mi') else "")
            + (f" @ {s['pace']}" if s.get('pace') else "")
            for s in sessions
        ]
        week1_text = "\n".join(week1_lines)

        message = (
            f"Your {weeks}-week plan is ready!\n\n"
            f"Goal: {dist} — {goal} on {race_date}\n"
            f"Race: {race_name}\n\n"
            f"Week 1:\n{week1_text}\n\n"
            f"Type /plan to see any week, or just chat with me about your training. "
            f"I'll reach out after every Strava activity and every Sunday evening with a weekly report.\n\n"
            f"Let's get to work."
        )
        telegram_client.send_message(chat_id, message)

    except Exception as e:
        logger.error(f"Plan generation failed for {chat_id}: {e}", exc_info=True)
        telegram_client.send_message(
            chat_id,
            "Something went wrong generating your plan. Reply /start to try again."
        )


# ─────────────────────────────────────────────────────────────
# PIPELINE 5: MORNING REMINDER
# ─────────────────────────────────────────────────────────────

def morning_reminder(chat_id: str) -> None:
    """Send today's session as a morning reminder with action buttons."""
    logger.info(f"Pipeline 5: Morning reminder [{chat_id}]")

    profile = user_store.get_profile(chat_id)
    session = user_store.get_todays_session(chat_id)
    days_left = (user_store.weeks_to_race(chat_id) or 0) * 7

    text = coach_prompts.build_morning_reminder(session, profile, days_left)

    current_week = user_store.get_current_week_plan(chat_id)
    week_number = current_week.get("week", 1) if current_week else 1
    day = session.get("day", "Mon")

    if session.get("type") != "rest":
        telegram_client.send_session_reminder(chat_id, text, day, week_number)
    else:
        telegram_client.send_message(chat_id, text)
