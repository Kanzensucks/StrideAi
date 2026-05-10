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
