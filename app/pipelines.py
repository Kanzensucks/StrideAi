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
# PIPELINE 0: CHATBOT — agent loop with tool use
# ─────────────────────────────────────────────────────────────

PROGRESS_KEYWORDS = [
    "show progress", "my progress", "progress chart", "progress graph",
    "show chart", "show graph", "training chart", "weekly chart", "training load",
]

# Tools Claude can call during chat — each wraps an existing data function
CHAT_TOOLS = [
    {
        "name": "get_todays_session",
        "description": "Get the athlete's planned training session for today from their plan.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_current_week",
        "description": (
            "Get the full current training week — all sessions with type, "
            "distance, pace, and notes."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_plan_week",
        "description": "Get a specific week from the training plan by week number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "week_number": {
                    "type": "integer",
                    "description": "Week number (1 = first week of plan)",
                }
            },
            "required": ["week_number"],
        },
    },
    {
        "name": "get_recent_runs",
        "description": (
            "Get the athlete's recent Strava runs. Use this to check training load, "
            "missed sessions, actual paces, or recovery status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "How many days back to fetch (7, 14, or 28). Default 14.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_weekly_load",
        "description": (
            "Get week-by-week training volume (km) for the last N weeks. "
            "Use this to identify trends, overtraining, or undertraining."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "weeks": {
                    "type": "integer",
                    "description": "Number of weeks to look back (max 8). Default 4.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_training_paces",
        "description": (
            "Get the athlete's current training pace zones — "
            "easy, threshold, intervals, and goal race pace."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "update_goal_and_paces",
        "description": (
            "Update the athlete's goal time and recalculate all training pace zones. "
            "Use this when the athlete asks to change their goal, target time, or paces. "
            "This actually saves the change — use it instead of just describing what the paces would be."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_time": {
                    "type": "string",
                    "description": (
                        "Goal finish time in h:mm:ss format, e.g. '3:40:00'. "
                        "Convert 'sub 3:40' → '3:40:00', '3h40m' → '3:40:00'."
                    ),
                }
            },
            "required": ["goal_time"],
        },
    },
]


def _execute_chat_tool(chat_id: str, name: str, inputs: dict) -> str:
    """Execute a tool call from the agent loop and return a plain-text result."""
    try:
        if name == "get_todays_session":
            session = user_store.get_todays_session(chat_id)
            if not session or session.get("type") == "rest":
                return "Today is a rest day — no session planned."
            dist = session.get("distance_km") or session.get("distance_mi")
            dist_str = f"{dist} {user_store.get_profile(chat_id).get('units','km')}" if dist else ""
            pace_str = f" @ {session['pace']}" if session.get("pace") else ""
            notes = session.get("notes", "")
            return (
                f"Today: {session['day']} — {session['type'].replace('_',' ').title()} "
                f"{dist_str}{pace_str}\n{notes}"
            ).strip()

        elif name == "get_current_week":
            week = user_store.get_current_week_plan(chat_id)
            if not week:
                return "No current week plan found."
            profile = user_store.get_profile(chat_id)
            units = profile.get("units", "km")
            lines = [
                f"Week {week['week']} · {week.get('phase','').title()} · "
                f"{week.get('target_volume_km') or week.get('target_volume_mi', '?')}"
                f"{units}"
            ]
            for s in week.get("sessions", []):
                dist = s.get("distance_km") or s.get("distance_mi")
                dist_str = f" {dist}{units}" if dist else ""
                pace_str = f" @ {s['pace']}" if s.get("pace") else ""
                lines.append(
                    f"{s['day']} — {s['type'].replace('_',' ').title()}"
                    f"{dist_str}{pace_str}"
                )
            return "\n".join(lines)

        elif name == "get_plan_week":
            wn = int(inputs.get("week_number", 1))
            week = user_store.get_week_plan(chat_id, wn)
            if not week:
                return f"Week {wn} not found in plan."
            profile = user_store.get_profile(chat_id)
            units = profile.get("units", "km")
            lines = [
                f"Week {week['week']} · {week.get('phase','').title()} · "
                f"{week.get('target_volume_km') or week.get('target_volume_mi','?')}"
                f"{units}"
            ]
            for s in week.get("sessions", []):
                dist = s.get("distance_km") or s.get("distance_mi")
                dist_str = f" {dist}{units}" if dist else ""
                pace_str = f" @ {s['pace']}" if s.get("pace") else ""
                lines.append(
                    f"{s['day']} — {s['type'].replace('_',' ').title()}"
                    f"{dist_str}{pace_str}"
                )
            return "\n".join(lines)

        elif name == "get_recent_runs":
            days = int(inputs.get("days", 14))
            days = min(days, 28)
            after = datetime.now() - timedelta(days=days)
            activities = strava_client.get_activities(
                chat_id, after_epoch=after.timestamp(), per_page=50, max_pages=1
            )
            if not activities:
                return f"No Strava runs found in the last {days} days."
            profile = user_store.get_profile(chat_id)
            units = profile.get("units", "km")
            runs = [
                a for a in activities
                if "run" in (a.get("sport_type") or a.get("type") or "").lower()
            ]
            if not runs:
                return f"No runs logged in the last {days} days."
            lines = [f"Last {days} days — {len(runs)} run(s):"]
            for a in sorted(runs, key=lambda x: x["start_date_local"])[-10:]:
                lines.append(strava_client.format_activity_line(a, units))
            return "\n".join(lines)

        elif name == "get_weekly_load":
            weeks = min(int(inputs.get("weeks", 4)), 8)
            after = datetime.now() - timedelta(weeks=weeks)
            activities = strava_client.get_activities(
                chat_id, after_epoch=after.timestamp(), per_page=200, max_pages=2
            )
            if not activities:
                return f"No Strava activities found in the last {weeks} weeks."
            profile = user_store.get_profile(chat_id)
            units = profile.get("units", "km")
            # Group by ISO week
            from collections import defaultdict
            week_buckets: dict = defaultdict(float)
            week_counts: dict = defaultdict(int)
            for a in activities:
                if "run" not in (a.get("sport_type") or a.get("type") or "").lower():
                    continue
                try:
                    from datetime import datetime as _dt
                    d = _dt.strptime(a["start_date_local"][:10], "%Y-%m-%d")
                    wk = d.strftime("%Y-W%W")
                    label = d.strftime("Wk %b %-d")
                except Exception:
                    continue
                dist_km = (a.get("distance") or 0) / 1000
                week_buckets[wk] += dist_km
                week_counts[wk] += 1
            if not week_buckets:
                return "No run data found."
            lines = [f"Weekly run volume (last {weeks} weeks):"]
            for wk in sorted(week_buckets.keys()):
                km = week_buckets[wk]
                cnt = week_counts[wk]
                display = round(km * 0.621371, 1) if units == "mi" else round(km, 1)
                lines.append(f"  {wk}: {display}{units} ({cnt} run{'s' if cnt != 1 else ''})")
            return "\n".join(lines)

        elif name == "get_training_paces":
            profile = user_store.get_profile(chat_id)
            paces = profile.get("paces", {})
            if not paces:
                return "No pace zones set yet. Use /pace or set a goal time."
            lines = ["Current training paces:"]
            for zone, pace in paces.items():
                lines.append(f"  {zone.replace('_', ' ').title()}: {pace}")
            return "\n".join(lines)

        elif name == "update_goal_and_paces":
            goal_time = str(inputs.get("goal_time", "")).strip()
            if not goal_time:
                return "Error: goal_time is required."
            # Normalise common formats → h:mm:ss
            import re as _re
            # "3:40" → "3:40:00"
            if _re.match(r'^\d:\d{2}$', goal_time) or _re.match(r'^\d{1,2}:\d{2}$', goal_time):
                goal_time = goal_time + ":00"
            profile = user_store.get_profile(chat_id)
            profile["goal_time"] = goal_time
            paces = plan_generator.calculate_paces(profile)
            if not paces:
                return f"Could not calculate paces for {goal_time} — check the format."
            user_store.update_profile(chat_id, {"goal_time": goal_time, "paces": paces})
            lines = [f"Goal updated to {goal_time}. New pace zones:"]
            for zone, pace in paces.items():
                lines.append(f"  {zone.replace('_', ' ').title()}: {pace}")
            logger.info(f"[agent] update_goal_and_paces: {goal_time} → paces saved [{chat_id}]")
            return "\n".join(lines)

        else:
            return f"Unknown tool: {name}"

    except Exception as e:
        logger.warning(f"Tool {name} failed for {chat_id}: {e}")
        return f"Could not retrieve {name.replace('_', ' ')} right now."


def handle_chat(chat_id: str, athlete_message: str, msg_timestamp=None) -> None:
    """Conversational coach — agent loop with tool use.

    Claude decides which tools to call based on the question.
    Each tool call is logged so the agent pattern is visible in Railway logs.
    """
    logger.info(f"Pipeline 0: Chat [{chat_id}] — {athlete_message[:60]}...")

    telegram_client.send_typing(chat_id)

    # Plan adjuster intercepts direct modification requests (unchanged)
    adjustment_msg = plan_adjuster.apply_chat_adjustment(chat_id, athlete_message)
    if adjustment_msg:
        storage.append_chat_message(chat_id, "user", athlete_message)
        storage.append_chat_message(chat_id, "assistant", adjustment_msg)
        telegram_client.send_message(chat_id, adjustment_msg)
        return

    storage.append_chat_message(chat_id, "user", athlete_message)

    # Progress chart shortcut (unchanged)
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
    memory_text = user_store.get_memory_text(chat_id)
    system_prompt = coach_prompts.build_chat_agent_system_prompt(
        profile, memory_text, msg_timestamp
    )

    api_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = storage.get_claude_messages(chat_id)

    # Keep typing indicator alive during the agent loop
    stop_typing = threading.Event()

    def _keep_typing():
        while not stop_typing.is_set():
            telegram_client.send_typing(chat_id)
            stop_typing.wait(4)

    typing_thread = threading.Thread(target=_keep_typing, daemon=True)
    typing_thread.start()

    reply = ""
    try:
        # ── Agent loop ───────────────────────────────────────────
        loop_messages = list(messages)  # don't mutate storage copy
        max_iterations = 6  # safety cap on tool rounds
        for _ in range(max_iterations):
            response = api_client.messages.create(
                model=MODEL_SONNET,
                max_tokens=1024,
                system=system_prompt,
                tools=CHAT_TOOLS,
                messages=loop_messages,
            )

            if response.stop_reason == "end_turn":
                # Claude finished — extract text reply
                for block in response.content:
                    if hasattr(block, "text"):
                        reply = block.text
                        break
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info(
                            f"[agent] tool={block.name} input={block.input} chat={chat_id}"
                        )
                        result = _execute_chat_tool(chat_id, block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                # Append this assistant turn + tool results, loop again
                loop_messages = loop_messages + [
                    {"role": "assistant", "content": response.content},
                    {"role": "user", "content": tool_results},
                ]
                continue

            # Unexpected stop reason — break
            break

    finally:
        stop_typing.set()

    if not reply:
        reply = "Something went wrong — try again in a moment."

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

def _send_post_plan_goal_options(chat_id: str, profile: dict) -> None:
    """After plan is delivered, show goal options so user can lock in a target.

    Reads A/B/C goals from the saved plan (Brain 1 output).
    Falls back to predict_goal_times() if Brain 1 didn't produce goals.
    """
    plan = user_store.get_plan(chat_id)
    goals = plan.get("goals", {}) if plan else {}
    planner_summary = (plan.get("planner_summary", "") or "") if plan else ""

    con = goals.get("C", "")
    rea = goals.get("B", "")
    agg = goals.get("A", "")

    # Fallback: Brain 1 didn't produce goals — use predict_goal_times()
    if not (con and rea and agg):
        try:
            predictions = plan_generator.predict_goal_times(profile)
        except Exception as e:
            logger.warning(f"Goal prediction fallback failed for {chat_id}: {e}")
            return
        con = predictions["conservative"]
        rea = predictions["realistic"]
        agg = predictions["aggressive"]
        planner_summary = f"Based on {predictions.get('rationale', 'your training data')}."

    dist_label = profile.get("race_distance", "marathon").replace("_", " ").title()

    def _fmt(t):
        parts = t.split(":")
        return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else t

    # Check if user has a stated goal that differs meaningfully from all 3 predictions
    stated_goal = profile.get("goal_time", "")
    stated_s = plan_generator._time_str_to_seconds(stated_goal) if stated_goal else 0
    prediction_seconds = set()
    for t in (con, rea, agg):
        s = plan_generator._time_str_to_seconds(t)
        if s > 0:
            prediction_seconds.add(s)

    show_stated = (
        stated_s > 0
        and bool(prediction_seconds)
        and all(abs(stated_s - p) > 3 * 60 for p in prediction_seconds)
    )

    summary_line = f"\n\n_{planner_summary}_" if planner_summary else ""
    stated_note = (
        f"\n\n⚡ *Your goal*  {_fmt(stated_goal)}\n"
        f"    Tougher than data suggests — possible if training holds"
        if show_stated else ""
    )
    msg = (
        f"*What's your goal for the {dist_label}?*{summary_line}\n\n"
        f"🟢 *Conservative*  {_fmt(con)}\n"
        f"    High probability, solid race\n\n"
        f"🎯 *Realistic*  {_fmt(rea)}\n"
        f"    Achievable with consistent training\n\n"
        f"🔥 *Aggressive*  {_fmt(agg)}\n"
        f"    Possible if everything clicks"
        f"{stated_note}\n\n"
        f"Your plan is built around the Realistic target. "
        f"Tapping a goal locks in your training paces."
    )
    keyboard = [
        [{"text": f"🟢 Conservative  {_fmt(con)}", "callback_data": f"goaltime:{con}"}],
        [{"text": f"🎯 Realistic  {_fmt(rea)}", "callback_data": f"goaltime:{rea}"}],
        [{"text": f"🔥 Aggressive  {_fmt(agg)}", "callback_data": f"goaltime:{agg}"}],
    ]
    if show_stated:
        keyboard.append([{"text": f"⚡ My goal  {_fmt(stated_goal)}", "callback_data": f"goaltime:{stated_goal}"}])
    telegram_client.send_message_with_keyboard(chat_id, msg, keyboard)


def generate_and_send_plan(chat_id: str, stated_goal: str = None) -> None:
    """Generate full plan and send welcome summary to user."""
    logger.info(f"Pipeline 4: Generating plan [{chat_id}]")

    try:
        profile = user_store.get_profile(chat_id)

        # Confirm Strava is live and show recent activity count
        try:
            from datetime import datetime, timedelta
            recent = strava_client.get_activities(chat_id, per_page=5, max_pages=1)
            if recent:
                latest = recent[0]
                units = profile.get("units", "km")
                activity_line = strava_client.format_activity_line(latest, units)
                telegram_client.send_message(
                    chat_id,
                    f"Strava connected. Last activity: {activity_line}\n\nGenerating your plan now..."
                )
            else:
                telegram_client.send_message(chat_id, "Strava connected — no recent activities found yet, but your plan is on its way.")
        except Exception:
            pass

        # Inject chat_id so generate_plan() can fetch Strava history
        profile["_chat_id"] = chat_id
        plan = plan_generator.generate_plan(profile)
        user_store.save_plan(chat_id, plan)

        # Save A/B/C goals and update profile goal_time to Brain 1's B-goal
        plan_goals = plan.get("goals", {})
        b_goal = plan_goals.get("B") or plan.get("goal_time")
        if b_goal:
            user_store.update_profile(chat_id, {"goal_time": b_goal})
            profile["goal_time"] = b_goal

        # Ensure paces are saved to profile (based on B-goal)
        paces = plan_generator.calculate_paces(profile)
        if paces:
            user_store.update_profile(chat_id, {"paces": paces})

        weeks = plan.get("total_weeks", len(plan.get("weeks", [])))
        race_name = profile.get("race_name", "your race")
        race_date = profile.get("race_date", "TBD")
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
            f"✅ Your *{weeks}-week plan* is ready!\n\n"
            f"*{race_name}* — {dist} on {race_date}\n\n"
            f"*Week 1:*\n{week1_text}\n\n"
            f"Use /plan to view any week. Chat with me anytime about your training.\n"
            f"I'll reach out after every Strava activity and every Sunday evening with a weekly report."
        )
        telegram_client.send_message(chat_id, message)

        if stated_goal:
            # /setrace flow — user already declared their goal, lock it in directly
            paces = plan_generator.calculate_paces({**profile, "goal_time": stated_goal})
            if paces:
                user_store.update_profile(chat_id, {"goal_time": stated_goal, "paces": paces})
            telegram_client.send_message(
                chat_id,
                f"Goal set: {stated_goal}. Training paces updated — type /pace to see your zones.\n\nLet's get to work."
            )
        else:
            # Onboarding flow — no stated goal, show Brain 1 predictions as buttons
            _send_post_plan_goal_options(chat_id, profile)

    except Exception as e:
        logger.error(f"Plan generation failed for {chat_id}: {e}", exc_info=True)
        telegram_client.send_message(
            chat_id,
            "Something went wrong generating your plan. Reply /start to try again."
        )


# ─────────────────────────────────────────────────────────────
# PIPELINE 5: MORNING REMINDER
# ─────────────────────────────────────────────────────────────

def _parse_pace_str(pace_str: str) -> float:
    """Parse '6:00/km' or '6:00/mi' → seconds (per unit, ignoring the unit suffix)."""
    pace_str = pace_str.split("/")[0].strip()
    parts = pace_str.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        pass
    return 0.0


# ─────────────────────────────────────────────────────────────
# PIPELINE 6: GOAL REVIEW (every 4 weeks)
# ─────────────────────────────────────────────────────────────

def goal_review(chat_id: str) -> None:
    """4-week fitness check — compare actual easy paces to planned, offer updated goal."""
    logger.info(f"Pipeline 6: Goal review [{chat_id}]")

    if not user_store.has_strava(chat_id):
        return

    profile = user_store.get_profile(chat_id)
    units = profile.get("units", "km")
    current_goal = profile.get("goal_time", "")

    if not current_goal or current_goal.lower() == "finish":
        return

    # Fetch last 4 weeks of activities
    try:
        four_weeks_ago = datetime.now() - timedelta(weeks=4)
        activities = strava_client.get_activities(
            chat_id,
            after_epoch=four_weeks_ago.timestamp(),
            per_page=100,
            max_pages=2,
        )
    except Exception as e:
        logger.error(f"Goal review Strava fetch failed for {chat_id}: {e}")
        return

    # Collect easy runs (short: 2–12 km) and their paces in s/km
    easy_paces = []
    for a in activities:
        sport = (a.get("sport_type") or a.get("type") or "").lower()
        if "run" not in sport:
            continue
        dist_km = (a.get("distance") or 0) / 1000
        moving_time = a.get("moving_time") or 0
        if 2.0 < dist_km < 12.0 and moving_time > 0:
            easy_paces.append(moving_time / dist_km)  # seconds per km

    if len(easy_paces) < 3:
        logger.info(f"Goal review: not enough easy runs for {chat_id} ({len(easy_paces)} found)")
        return

    avg_easy_s_km = sum(easy_paces) / len(easy_paces)

    # Load planned easy pace
    paces = profile.get("paces", {})
    planned_easy_str = paces.get("easy", "")
    if not planned_easy_str:
        return

    # planned_easy_str is "6:00/km" or "6:00/mi" — parse to seconds per unit
    planned_easy_s = _parse_pace_str(planned_easy_str)
    if planned_easy_s <= 0:
        return

    # If miles, convert actual pace to s/mi for a fair comparison
    if units == "mi":
        avg_easy_compare = avg_easy_s_km * 1.60934
    else:
        avg_easy_compare = avg_easy_s_km

    # If actual pace is NOT >8% faster than planned, nothing to report
    pace_ratio = avg_easy_compare / planned_easy_s
    if pace_ratio >= 0.92:
        logger.info(f"Goal review: pace within range for {chat_id} (ratio={pace_ratio:.2f})")
        return

    # Fitness is ahead — re-predict goal times
    predictions = plan_generator.predict_goal_times(profile)
    new_realistic_str = predictions.get("realistic", "")
    if not new_realistic_str:
        return

    current_s = plan_generator._time_str_to_seconds(current_goal)
    new_realistic_s = plan_generator._time_str_to_seconds(new_realistic_str)

    if current_s <= 0 or new_realistic_s <= 0:
        return

    diff_s = abs(current_s - new_realistic_s)
    if diff_s < 300:  # <5 min — not worth bothering the user
        logger.info(f"Goal review: goal unchanged for {chat_id} (diff={diff_s}s)")
        return

    conservative = predictions.get("conservative", "")
    realistic = predictions.get("realistic", "")
    aggressive = predictions.get("aggressive", "")
    rationale = predictions.get("rationale", "your recent training")

    direction = "faster" if new_realistic_s < current_s else "slower"
    diff_min = diff_s // 60

    msg = (
        f"🔄 *4-week fitness check*\n\n"
        f"Based on {rationale}, your fitness suggests a different target.\n\n"
        f"Current goal: *{current_goal}*\n"
        f"Updated prediction: *{realistic}* ({diff_min} min {direction})\n\n"
        f"Want to update your goal?"
    )

    keyboard = [
        [{"text": f"🟢 Conservative  {conservative}", "callback_data": f"goalreview:{conservative}"}],
        [{"text": f"🎯 Realistic  {realistic}", "callback_data": f"goalreview:{realistic}"}],
        [{"text": f"🔥 Aggressive  {aggressive}", "callback_data": f"goalreview:{aggressive}"}],
        [{"text": f"Keep current goal ({current_goal})", "callback_data": "goalreview:keep"}],
    ]
    telegram_client.send_message_with_keyboard(chat_id, msg, keyboard)
    logger.info(f"Goal review offer sent to {chat_id}")


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
