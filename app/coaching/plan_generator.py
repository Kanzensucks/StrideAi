"""Generate a full training plan from a user profile using Claude Opus.

Output: plan.json with structure:
{
  "race_date": "YYYY-MM-DD",
  "race_distance": "marathon",
  "goal_time": "3:30:00",
  "total_weeks": N,
  "generated_at": "ISO timestamp",
  "weeks": [
    {
      "week": 1,
      "phase": "base",
      "start_date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD",
      "target_volume_km": 40,
      "sessions": [
        {
          "day": "Mon",
          "type": "easy",
          "distance_km": 6,
          "distance_mi": 3.7,
          "pace": "6:00/km",
          "notes": "Keep effort conversational.",
          "is_key_session": false
        }
      ]
    }
  ]
}
"""

import json
import logging
import os
from datetime import datetime, timedelta

import anthropic

from app.config import Config

logger = logging.getLogger(__name__)

MODEL_OPUS = "claude-opus-4-20250514"
MODEL_SONNET = "claude-sonnet-4-20250514"


# ─── Pace calculation ─────────────────────────────────────────

def _time_str_to_seconds(time_str: str) -> int:
    """Convert h:mm:ss or mm:ss to total seconds."""
    parts = time_str.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        else:
            return int(parts[0])
    except (ValueError, IndexError):
        return 0


def _seconds_to_pace(seconds_per_km: float) -> str:
    """Convert seconds/km to mm:ss/km string."""
    s = int(seconds_per_km)
    return f"{s // 60}:{s % 60:02d}/km"


def _seconds_to_pace_mi(seconds_per_km: float) -> str:
    """Convert seconds/km to mm:ss/mi string."""
    s_per_mi = seconds_per_km * 1.60934
    s = int(s_per_mi)
    return f"{s // 60}:{s % 60:02d}/mi"


DISTANCE_KM = {
    "5k": 5.0,
    "10k": 10.0,
    "half_marathon": 21.0975,
    "marathon": 42.195,
}


def _seconds_to_time_str(total_seconds: int) -> str:
    """Convert total seconds to h:mm:ss string."""
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h}:{m:02d}:{s:02d}"


def _estimate_from_volume(weekly_km: float, longest_km: float,
                           experience: str, distance: str) -> int:
    """Estimate realistic race time in seconds from training data (no PRs)."""
    # Base marathon time from weekly volume (Jack Daniels-inspired)
    volume_map = [
        (70, 195 * 60),  # 3:15
        (60, 210 * 60),  # 3:30
        (50, 225 * 60),  # 3:45
        (40, 245 * 60),  # 4:05
        (30, 265 * 60),  # 4:25
        (20, 285 * 60),  # 4:45
        (0,  310 * 60),  # 5:10
    ]
    base_s = 310 * 60
    for km_thresh, time_s in volume_map:
        if weekly_km >= km_thresh:
            base_s = time_s
            break

    # Long run readiness adjustment
    if longest_km >= 32:
        base_s -= 10 * 60
    elif longest_km >= 26:
        base_s -= 5 * 60
    elif longest_km < 18:
        base_s += 10 * 60
    elif longest_km < 15:
        base_s += 20 * 60

    # Experience adjustment
    base_s += {"advanced": -10 * 60, "intermediate": 0, "beginner": 15 * 60}.get(experience, 0)

    # Scale to target distance via Riegel
    target_km = DISTANCE_KM.get(distance, 42.195)
    marathon_km = DISTANCE_KM["marathon"]
    if target_km != marathon_km:
        base_s = int(base_s * (target_km / marathon_km) ** 1.06)

    return base_s


def _training_improvement(weeks_to_race: int, experience: str, weekly_km: float) -> float:
    """Estimate how much faster (as a fraction) a runner can get over the training block.

    Returns a value like 0.05 meaning "5% faster by race day".
    Based on weeks available, experience level, and current volume headroom.
    """
    # More weeks = more adaptation time (cap at 20 weeks)
    week_factor = min(weeks_to_race, 20) / 20.0

    # Experience: beginners have more room to improve, advanced less
    base_improvement = {"beginner": 0.09, "intermediate": 0.06, "advanced": 0.03}.get(
        experience, 0.06
    )

    # Volume headroom: if currently running <50km, more aerobic gains available
    vol_headroom = max(0.0, min(1.0, (55 - weekly_km) / 55))

    improvement = base_improvement * week_factor * (1 + vol_headroom * 0.4)
    return round(min(improvement, 0.10), 4)  # cap at 10%


def predict_goal_times(profile: dict) -> dict:
    """Predict race goal times from fitness data, accounting for training ahead.

    Priority: PRs (Riegel formula) > volume-based estimate.
    Applies a training improvement factor so goals reflect race-day fitness,
    not just current fitness.

    Returns dict with conservative / realistic / aggressive / rationale.
    """
    from datetime import datetime

    distance = profile.get("race_distance", "marathon")
    weekly_km = float(profile.get("current_weekly_km", 30))
    longest_km = float(profile.get("longest_recent_run_km", 15))
    experience = profile.get("experience_level", "intermediate")
    prs = profile.get("prs", {})
    target_km = DISTANCE_KM.get(distance, 42.195)

    # Weeks until race (used to scale improvement potential)
    weeks_to_race = 12  # sensible default
    race_date_str = profile.get("race_date", "")
    if race_date_str:
        try:
            race_dt = datetime.strptime(race_date_str, "%Y-%m-%d")
            weeks_to_race = max(0, (race_dt - datetime.now()).days // 7)
        except ValueError:
            pass

    # Try PR-based prediction first (most accurate current-fitness baseline)
    best_s = None
    pr_source = None
    for pr_dist in ["half_marathon", "10k", "5k"]:
        if pr_dist in prs and pr_dist != distance:
            pr_s = _time_str_to_seconds(prs[pr_dist])
            if pr_s > 0:
                source_km = DISTANCE_KM[pr_dist]
                predicted = int(pr_s * (target_km / source_km) ** 1.06)
                if best_s is None or predicted < best_s:
                    best_s = predicted
                    pr_source = pr_dist.replace("_", " ")

    if best_s:
        current_fitness_s = best_s
        rationale = f"your {pr_source} PR + {weeks_to_race} weeks of training"
    else:
        current_fitness_s = _estimate_from_volume(weekly_km, longest_km, experience, distance)
        rationale = f"{int(weekly_km)}km/week + {int(longest_km)}km long run + {weeks_to_race} weeks to train"

    # Apply training improvement: realistic = race-day fitness after a full block
    improvement = _training_improvement(weeks_to_race, experience, weekly_km)
    realistic_s = int(current_fitness_s * (1 - improvement))

    # Conservative = modest improvement (half the realistic gain), low-risk target
    conservative_s = int(current_fitness_s * (1 - improvement * 0.4))

    # Aggressive = 1.5× the realistic gain, requires everything to click
    aggressive_s = int(current_fitness_s * (1 - improvement * 1.6))

    return {
        "conservative": _seconds_to_time_str(conservative_s),
        "realistic": _seconds_to_time_str(realistic_s),
        "aggressive": _seconds_to_time_str(aggressive_s),
        "rationale": rationale,
    }


def calculate_paces(profile: dict) -> dict:
    """Calculate training pace zones from goal time + race distance.

    Returns dict of zone_name -> pace string (in the user's preferred units).
    """
    goal_time = profile.get("goal_time", "finish")
    dist_key = profile.get("race_distance", "marathon")
    units = profile.get("units", "km")

    if goal_time.lower() == "finish" or not goal_time:
        return {}

    total_seconds = _time_str_to_seconds(goal_time)
    dist_km = DISTANCE_KM.get(dist_key, 42.195)

    if total_seconds <= 0 or dist_km <= 0:
        return {}

    goal_pace_s_per_km = total_seconds / dist_km

    # Multipliers per methodology
    mult = {
        "marathon": {"easy": 1.20, "threshold": 1.0, "intervals": 0.92},
        "half_marathon": {"easy": 1.20, "threshold": 1.05, "intervals": 0.90},
        "10k": {"easy": 1.25, "threshold": 1.10, "intervals": 0.95},
        "5k": {"easy": 1.30, "threshold": 1.15, "intervals": 1.0},
    }.get(dist_key, {"easy": 1.20, "threshold": 1.0, "intervals": 0.92})

    paces = {}
    for zone, m in mult.items():
        s = goal_pace_s_per_km * m
        if units == "mi":
            paces[zone] = _seconds_to_pace_mi(s)
        else:
            paces[zone] = _seconds_to_pace(s)

    paces["goal_race_pace"] = (
        _seconds_to_pace_mi(goal_pace_s_per_km) if units == "mi"
        else _seconds_to_pace(goal_pace_s_per_km)
    )

    return paces


# ─── Week date assignment ─────────────────────────────────────

def _assign_week_dates(weeks: list, start_date: datetime) -> list:
    """Add start_date/end_date to each week dict."""
    current = start_date
    for week in weeks:
        week["start_date"] = current.strftime("%Y-%m-%d")
        week["end_date"] = (current + timedelta(days=6)).strftime("%Y-%m-%d")
        current += timedelta(weeks=1)
    return weeks


def _load_methodology() -> str:
    path = os.path.join(Config.KNOWLEDGE_DIR, "public_coaching_methodology.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


# ─── Plan generation — two-brain orchestrator ────────────────

def _strip_fences(raw: str) -> str:
    """Remove markdown code fences if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def _build_inputs(profile: dict, strava_activities: list) -> tuple[dict, dict]:
    """Build the ATHLETE and RACE objects for Brain 1."""
    # Strava fitness summary (last 8 weeks)
    strava_summary = {}
    if strava_activities:
        run_acts = [
            a for a in strava_activities
            if "run" in (a.get("sport_type") or a.get("type") or "").lower()
        ]
        if run_acts:
            total_km = sum((a.get("distance") or 0) / 1000 for a in run_acts)
            weeks_span = max(1, len(strava_activities) / 7)  # rough
            long_runs = sorted(
                [(a.get("distance") or 0) / 1000 for a in run_acts], reverse=True
            )
            strava_summary = {
                "activity_count_8wk": len(run_acts),
                "avg_weekly_km": round(total_km / 8, 1),
                "recent_long_run_km": round(long_runs[0], 1) if long_runs else 0,
            }

    # Riegel estimate for the fitness floor
    riegel_estimate = ""
    prs = profile.get("prs", {})
    dist = profile.get("race_distance", "marathon")
    target_km = DISTANCE_KM.get(dist, 42.195)
    for pr_dist in ["half_marathon", "10k", "5k"]:
        if pr_dist in prs and pr_dist != dist:
            pr_s = _time_str_to_seconds(prs[pr_dist])
            if pr_s > 0:
                source_km = DISTANCE_KM[pr_dist]
                predicted_s = int(pr_s * (target_km / source_km) ** 1.06)
                riegel_estimate = _seconds_to_time_str(predicted_s)
                break

    athlete = {
        "profile": {
            "experience_level": profile.get("experience_level", "intermediate"),
            "current_weekly_km": float(profile.get("current_weekly_km", 30)),
            "longest_recent_run_km": float(profile.get("longest_recent_run_km", 15)),
            "injury_notes": profile.get("injury_notes", "none reported"),
            "prs": profile.get("prs", {}),
        },
        "fitness_markers": {
            "riegel_estimate": riegel_estimate or "unknown",
            **strava_summary,
        },
        "constraints": {
            "days_per_week": profile.get("days_per_week", 4),
            "long_run_day": profile.get("long_run_day", "Sunday"),
            "cross_training": profile.get("cross_training_prefs", []),
            "units": profile.get("units", "km"),
        },
    }

    race = {
        "basics": {
            "name": profile.get("race_name", "Goal Race"),
            "date": profile.get("race_date", ""),
            "distance": profile.get("race_distance", "marathon"),
        }
    }

    return athlete, race


def _run_planner_brain(athlete: dict, race: dict, progression: str, today: str) -> dict:
    """Brain 1: Planner (Opus).

    Takes the full athlete + race context and produces:
    - phases with weekly_km per week
    - A/B/C goals (floor rule enforced)
    - design notes + coach summary
    """
    from app.coaching.prompts import PLANNER_SYSTEM_PROMPT

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_message = json.dumps({
        "ATHLETE": athlete,
        "RACE": race,
        "PROGRESSION": progression,
        "TODAY": today,
    }, indent=2)

    logger.info("Brain 1 (Planner/Opus): starting...")

    with client.messages.stream(
        model=MODEL_OPUS,
        max_tokens=4000,
        system=PLANNER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        raw = stream.get_final_text()

    result = json.loads(_strip_fences(raw))
    logger.info("Brain 1 (Planner): complete")
    return result


_SESSION_CHUNK_SIZE = 5  # weeks per Brain 2 call — keeps each call well under 16K tokens


def _call_session_builder_chunk(
    week_chunk: list,
    system: str,
    paces_text: str,
    race_date_str: str,
    race_day_of_week: str,
    long_run_day: str,
    days_per_week: int,
    total_plan_weeks: int,
) -> list:
    """Run one Brain 2 chunk call for a slice of weeks.

    Returns a list of week objects with sessions[].
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    n = len(week_chunk)
    first_week = week_chunk[0]["week"]
    last_week = week_chunk[-1]["week"]

    user_message = (
        f"Fill sessions for weeks {first_week}–{last_week} "
        f"(plan is {total_plan_weeks} weeks total).\n"
        f"LONG RUN DAY: {long_run_day} — place the long run on {long_run_day} every week.\n"
        f"Training days per week: {days_per_week} (remaining days = rest).\n"
        f"Race day of week: {race_day_of_week}, race date: {race_date_str} "
        f"(race session only in final week week {total_plan_weeks}).\n"
        f"Pace zones:\n{paces_text}\n\n"
        f"Week structure:\n{json.dumps(week_chunk, indent=2)}\n\n"
        f"Return ONLY a valid JSON array of exactly {n} week objects with sessions[].\n"
        f"No prose. No fences. Start with ["
    )

    with client.messages.stream(
        model=MODEL_SONNET,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        raw = stream.get_final_text()

    raw = raw.strip()
    if not raw.startswith("["):
        raw = "[" + raw
    raw = _strip_fences(raw)
    if not raw.startswith("["):
        raw = "[" + raw

    return json.loads(raw)


def _run_session_builder_brain(
    planner_output: dict,
    athlete: dict,
    units: str,
    paces: dict,
    total_weeks: int,
    methodology: str,
    race_date_str: str,
    race_day_of_week: str,
) -> list:
    """Brain 2: Session Builder (Sonnet).

    Takes Brain 1's macro phases and fills in individual sessions for every week.
    Generates in chunks of _SESSION_CHUNK_SIZE weeks to stay under token limits.
    Returns a list of week dicts with sessions[].
    """
    from app.coaching.prompts import SESSION_BUILDER_SYSTEM_PROMPT

    long_run_day = athlete["constraints"]["long_run_day"]
    days_per_week = athlete["constraints"]["days_per_week"]
    cross_training = ", ".join(athlete["constraints"]["cross_training"]) or "none"
    injury_notes = athlete["profile"]["injury_notes"]

    paces_text = "\n".join(f"  {k}: {v}" for k, v in paces.items()) if paces else "  (use approximate zones)"

    # Flatten phases into a simple week list for Brain 2
    phases = planner_output.get("training_plan", {}).get("phases", [])
    week_structure = []
    for phase in phases:
        name = phase.get("name", "base")
        week_numbers = phase.get("week_numbers", [])
        weekly_kms = phase.get("weekly_km", [])
        for i, wn in enumerate(week_numbers):
            km = weekly_kms[i] if i < len(weekly_kms) else 0
            week_structure.append({"week": wn, "phase": name, f"target_volume_{units}": km})

    week_structure.sort(key=lambda w: w["week"])

    system = SESSION_BUILDER_SYSTEM_PROMPT\
        .replace("{long_run_day}", long_run_day)\
        .replace("{days_per_week}", str(days_per_week))\
        .replace("{units}", units)\
        .replace("{race_day_of_week}", race_day_of_week)\
        .replace("{cross_training}", cross_training)\
        .replace("{injury_notes}", injury_notes)\
        .replace("{methodology}", methodology[:2000])  # trim to avoid prompt bloat

    # Split into chunks — each chunk stays well under 16K output tokens
    chunks = [
        week_structure[i: i + _SESSION_CHUNK_SIZE]
        for i in range(0, len(week_structure), _SESSION_CHUNK_SIZE)
    ]

    logger.info(
        f"Brain 2 (Session Builder/Sonnet): {total_weeks} weeks "
        f"in {len(chunks)} chunks of up to {_SESSION_CHUNK_SIZE}..."
    )

    all_weeks = []
    for idx, chunk in enumerate(chunks):
        first = chunk[0]["week"]
        last = chunk[-1]["week"]
        logger.info(f"  Brain 2 chunk {idx + 1}/{len(chunks)}: weeks {first}–{last}")
        chunk_weeks = _call_session_builder_chunk(
            week_chunk=chunk,
            system=system,
            paces_text=paces_text,
            race_date_str=race_date_str,
            race_day_of_week=race_day_of_week,
            long_run_day=long_run_day,
            days_per_week=days_per_week,
            total_plan_weeks=total_weeks,
        )
        all_weeks.extend(chunk_weeks)

    logger.info(f"Brain 2 (Session Builder): complete — {len(all_weeks)} weeks total")
    return all_weeks


def generate_plan(profile: dict) -> dict:
    """Generate a full training plan using two coordinated brains.

    Brain 1 (Opus/Planner): backwards-designs the macro structure —
      phases, weekly km, A/B/C goals, taper.
    Brain 2 (Sonnet/Session Builder): fills individual sessions into
      each week respecting the runner's chosen training slots.

    Returns the plan dict (same shape as before — no breaking changes to callers).
    """
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")

    race_date_str = profile.get("race_date", "")
    try:
        race_dt = datetime.strptime(race_date_str, "%Y-%m-%d")
        # Next Monday from today (always moves forward, even if today is Monday)
        plan_start = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        # +1 so the race week is included as the final week
        total_weeks = max(4, ((race_dt - plan_start).days // 7) + 1)
        race_day_of_week = race_dt.strftime("%A")
    except ValueError:
        plan_start = today
        total_weeks = 12
        race_day_of_week = "Sunday"

    units = profile.get("units", "km")

    # Fetch Strava activities (last 8 weeks) — silent fail
    strava_activities = []
    try:
        from app.integrations import strava_client
        from datetime import timedelta as _td
        eight_weeks_ago = today - _td(weeks=8)
        strava_activities = strava_client.get_activities(
            profile.get("_chat_id", ""),
            after_epoch=eight_weeks_ago.timestamp(),
            per_page=100,
            max_pages=2,
        )
    except Exception:
        pass

    # Build structured inputs
    athlete, race = _build_inputs(profile, strava_activities)

    # Compute paces from realistic goal (Brain 1 will refine, but we need zones for Brain 2)
    paces = calculate_paces(profile)

    methodology = _load_methodology()

    # ── Brain 1: Planner (Opus) ──────────────────────────────
    try:
        planner_output = _run_planner_brain(athlete, race, "standard", today_str)
    except Exception as e:
        logger.error(f"Brain 1 (Planner) failed: {e}", exc_info=True)
        raise

    # Extract A/B/C goals from Brain 1
    goals = planner_output.get("race_updates", {}).get("goals", {})
    plan_summary = planner_output.get("summary", "")

    # Use B-goal as the plan's working goal_time (realistic target)
    b_goal = goals.get("B", profile.get("goal_time", "finish"))

    # ── Brain 2: Session Builder (Sonnet) ────────────────────
    try:
        weeks = _run_session_builder_brain(
            planner_output=planner_output,
            athlete=athlete,
            units=units,
            paces=paces,
            total_weeks=total_weeks,
            methodology=methodology,
            race_date_str=race_date_str,
            race_day_of_week=race_day_of_week,
        )
    except Exception as e:
        logger.error(f"Brain 2 (Session Builder) failed: {e}", exc_info=True)
        raise

    # ── Merge + assign dates ─────────────────────────────────
    weeks = _assign_week_dates(weeks, plan_start)

    plan = {
        "race_date": race_date_str,
        "race_distance": profile.get("race_distance", "marathon"),
        "goal_time": b_goal,
        "total_weeks": total_weeks,
        "generated_at": today.isoformat(),
        "goals": {
            "A": goals.get("A", ""),
            "B": goals.get("B", ""),
            "C": goals.get("C", ""),
        },
        "planner_summary": plan_summary,
        "weeks": weeks,
    }

    logger.info(f"Plan complete: {total_weeks} weeks, goals A={goals.get('A')} B={goals.get('B')} C={goals.get('C')}")
    return plan
