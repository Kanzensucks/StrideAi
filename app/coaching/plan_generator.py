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


# ─── Plan generation ─────────────────────────────────────────

def generate_plan(profile: dict) -> dict:
    """Generate a full training plan using Claude Opus.

    Called once after onboarding. Returns the plan dict.
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    methodology = _load_methodology()
    paces = calculate_paces(profile)
    paces_text = "\n".join(f"  {k}: {v}" for k, v in paces.items()) if paces else "  (use approximate zones from methodology)"

    race_date_str = profile.get("race_date", "")
    try:
        race_dt = datetime.strptime(race_date_str, "%Y-%m-%d")
        today = datetime.now()
        total_weeks = max(4, (race_dt - today).days // 7)
        plan_start = today + timedelta(days=(7 - today.weekday()) % 7 or 7)  # next Monday
        race_day_of_week = race_dt.strftime("%A")  # e.g. "Sunday"
    except ValueError:
        total_weeks = 12
        plan_start = datetime.now()
        race_day_of_week = "Sunday"

    units = profile.get("units", "km")
    dist_label = profile.get("race_distance", "marathon").replace("_", " ").title()
    long_run_day = profile.get("long_run_day", "Sunday")
    days_per_week = profile.get("days_per_week", 4)
    cross_train = profile.get("cross_training_prefs", [])
    cross_str = ", ".join(cross_train) if cross_train else "none"
    experience = profile.get("experience_level", "intermediate")
    weekly_km = profile.get("current_weekly_km", 30)
    longest_run = profile.get("longest_recent_run_km", 15)
    injury_notes = profile.get("injury_notes", "none reported")
    first_name = profile.get("first_name", "Athlete")

    system_prompt = f"""You are StrideAI, an expert running coach. Generate a complete, personalised training plan in JSON format.

{methodology}

Rules:
- All distances in {units}. All paces in per {units}.
- Long run day: {long_run_day} only.
- Training days per week: {days_per_week}.
- Cross-training available: {cross_str}.
- Never exceed 30% of weekly volume in a single session.
- Week 4, 8, 12+ (every 4th week): recovery week at ~70% of prior week's volume.
- Taper: final 15% of the plan — reduce volume, maintain intensity.
- Include race-pace work only from week 4 onward (for plans 8+ weeks).
- Injury notes: {injury_notes} — build around these.
- CRITICAL: Generate exactly {total_weeks} weeks. The race ({race_date_str}) falls on a {race_day_of_week}. Place the Race session ONLY in week {total_weeks} on {race_day_of_week}. All other weeks are training weeks — no race session before week {total_weeks}.

Session types to use: easy, long, threshold, intervals, race_pace, cross_train, strength, rest, race.
Each session must have: day (3-letter abbreviation), type, distance_{units} (number or null for cross-train/rest), pace (string or null), notes (string), is_key_session (bool).

Return ONLY valid JSON — no explanations, no markdown, no code fences. Start with {{"""

    user_message = f"""Generate a {total_weeks}-week training plan for:
Name: {first_name}
Goal: {dist_label} on {race_date_str} in {profile.get('goal_time', 'finish')}
Experience: {experience}
Current weekly volume: {weekly_km}{units} | Longest recent run: {longest_run}{units}
PRs: {json.dumps(profile.get('prs', {}))}
Training pace zones:
{paces_text}

Plan starts: {plan_start.strftime('%Y-%m-%d')} (Monday)
Total weeks: {total_weeks}
Long run day: {long_run_day}
Days/week: {days_per_week}
Cross-training: {cross_str}
Injury notes: {injury_notes}

Return JSON matching this exact structure:
{{
  "race_date": "{race_date_str}",
  "race_distance": "{profile.get('race_distance', 'marathon')}",
  "goal_time": "{profile.get('goal_time', 'finish')}",
  "total_weeks": {total_weeks},
  "generated_at": "{datetime.now().isoformat()}",
  "weeks": [
    {{
      "week": 1,
      "phase": "base",
      "start_date": "",
      "end_date": "",
      "target_volume_{units}": 0,
      "sessions": [
        {{
          "day": "Mon",
          "type": "easy",
          "distance_{units}": 6,
          "pace": "6:00/{units}",
          "notes": "Keep effort conversational.",
          "is_key_session": false
        }}
      ]
    }}
  ]
}}"""

    logger.info(f"Generating plan for user (Opus, {total_weeks} weeks)")

    with client.messages.stream(
        model=MODEL_OPUS,
        max_tokens=32000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        raw = stream.get_final_text()

    raw = raw.strip()

    # Strip any accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    plan = json.loads(raw)

    # Assign actual start/end dates to each week
    plan["weeks"] = _assign_week_dates(plan["weeks"], plan_start)

    return plan
