"""Strava API client — per-user token management."""

import os
import time
import requests

from app.core import user_store

STRAVA_AUTH_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"
STRAVA_DEAUTH_URL = "https://www.strava.com/oauth/deauthorize"


def _refresh_if_expired(chat_id: str, tokens: dict) -> dict:
    if tokens.get("expires_at", 0) > time.time() + 60:
        return tokens
    resp = requests.post(STRAVA_AUTH_URL, data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    tokens.update({
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
    })
    user_store.save_strava_tokens(chat_id, tokens)
    return tokens


def _get_headers(chat_id: str) -> dict:
    tokens = user_store.get_strava_tokens(chat_id)
    if not tokens.get("access_token"):
        raise ValueError(f"No Strava tokens for user {chat_id}")
    tokens = _refresh_if_expired(chat_id, tokens)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def get_activity(chat_id: str, activity_id: int) -> dict:
    resp = requests.get(
        f"{STRAVA_API_BASE}/activities/{activity_id}",
        headers=_get_headers(chat_id), timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_athlete(chat_id: str) -> dict:
    resp = requests.get(
        f"{STRAVA_API_BASE}/athlete",
        headers=_get_headers(chat_id), timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_athlete_stats(chat_id: str) -> dict:
    headers = _get_headers(chat_id)
    t = user_store.get_strava_tokens(chat_id)
    athlete_id = t.get("athlete_id")
    if not athlete_id:
        athlete = get_athlete(chat_id)
        athlete_id = athlete["id"]
        user_store.update_profile(chat_id, {"strava_athlete_id": athlete_id})
        t["athlete_id"] = athlete_id
        user_store.save_strava_tokens(chat_id, t)
    resp = requests.get(
        f"{STRAVA_API_BASE}/athletes/{athlete_id}/stats",
        headers=headers, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def get_activities(chat_id: str, after_epoch=None, per_page: int = 200, max_pages: int = 10) -> list:
    params = {"per_page": min(per_page, 200)}
    if after_epoch:
        params["after"] = int(after_epoch)
    all_activities = []
    for page in range(1, max_pages + 1):
        params["page"] = page
        resp = requests.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers=_get_headers(chat_id), params=params, timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_activities.extend(batch)
        if len(batch) < params["per_page"]:
            break
    return all_activities


def revoke_access(chat_id: str) -> None:
    tokens = user_store.get_strava_tokens(chat_id)
    if not tokens.get("access_token"):
        return
    try:
        requests.post(STRAVA_DEAUTH_URL, data={"access_token": tokens["access_token"]}, timeout=10)
    except Exception:
        pass


def format_activity(activity: dict, units: str = "km") -> dict:
    from datetime import datetime
    start = datetime.fromisoformat(activity["start_date_local"].replace("Z", "+00:00"))
    sport = activity.get("sport_type", activity.get("type", "Unknown"))
    distance_m = activity.get("distance", 0)
    distance_km = distance_m / 1000
    moving_time_s = activity.get("moving_time", 0)
    hours = moving_time_s // 3600
    minutes = (moving_time_s % 3600) // 60
    duration = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"

    sport_lower = sport.lower()
    if "run" in sport_lower or "walk" in sport_lower:
        if distance_km > 0:
            pace_s = moving_time_s / distance_km
            if units == "mi":
                pace_s = pace_s * 1.60934
                pace = f"{int(pace_s // 60)}:{int(pace_s % 60):02d}/mi"
            else:
                pace = f"{int(pace_s // 60)}:{int(pace_s % 60):02d}/km"
        else:
            pace = "N/A"
    elif any(k in sport_lower for k in ["ride", "bike", "cycl"]):
        pace = f"{distance_km / (moving_time_s / 3600):.1f}kph" if moving_time_s > 0 else "N/A"
    else:
        pace = "N/A"

    dist_display = round(distance_km * 0.621371, 2) if units == "mi" else round(distance_km, 2)
    avg_hr = activity.get("average_heartrate")
    max_hr = activity.get("max_heartrate")
    return {
        "day": start.strftime("%A"),
        "date": start.strftime("%b %d"),
        "name": activity.get("name", "Untitled"),
        "sport": sport,
        "distance_km": round(distance_km, 2),
        "distance_display": f"{dist_display}{units}",
        "duration": duration,
        "pace": pace,
        "avg_hr": int(avg_hr) if avg_hr else "N/A",
        "max_hr": int(max_hr) if max_hr else "N/A",
        "elevation": f"{activity.get('total_elevation_gain', 0):.0f}m",
    }


def format_activity_line(activity: dict, units: str = "km") -> str:
    f = format_activity(activity, units)
    return (
        f"{f['day']} {f['date']} | {f['name']} | {f['sport']} | "
        f"{f['distance_display']} | {f['duration']} | {f['pace']} | "
        f"HR {f['avg_hr']}/{f['max_hr']} | Elev {f['elevation']}"
    )


def format_laps(activity: dict, units: str = "km", max_laps: int = 40) -> str:
    laps = activity.get("laps") or []
    if not laps:
        return ""
    if len(laps) == 1:
        total_d = activity.get("distance", 0) or 0
        lap_d = laps[0].get("distance", 0) or 0
        if total_d > 0 and abs(lap_d - total_d) / total_d < 0.02:
            return ""
    sport = (activity.get("sport_type") or activity.get("type") or "").lower()
    is_run = "run" in sport or "walk" in sport
    is_ride = any(k in sport for k in ["ride", "bike", "cycl"])
    lines = []
    for i, lap in enumerate(laps[:max_laps], start=1):
        dist_m = lap.get("distance", 0) or 0
        dist_km = dist_m / 1000
        t_s = lap.get("moving_time") or lap.get("elapsed_time") or 0
        dur = (f"{t_s // 3600}h{(t_s % 3600) // 60:02d}m" if t_s >= 3600
               else f"{t_s // 60}:{t_s % 60:02d}" if t_s >= 60 else f"{t_s}s")
        if is_run and dist_km > 0:
            pace_s = t_s / dist_km * (1.60934 if units == "mi" else 1)
            u = units
            metric = f"{int(pace_s // 60)}:{int(pace_s % 60):02d}/{u}"
        elif is_ride and t_s > 0:
            metric = f"{dist_km / (t_s / 3600):.1f}kph"
        else:
            metric = "—"
        avg_hr = lap.get("average_heartrate")
        max_hr = lap.get("max_heartrate")
        hr = f" | HR {int(avg_hr) if avg_hr else '—'}/{int(max_hr) if max_hr else '—'}" if (avg_hr or max_hr) else ""
        dist_d = round(dist_km * 0.621371, 2) if units == "mi" else round(dist_km, 2)
        lap_name = (lap.get("name") or "").strip()
        name_p = f" [{lap_name}]" if lap_name and not lap_name.lower().startswith("lap ") else ""
        lines.append(f"  L{i}{name_p} | {dist_d}{units} | {dur} | {metric}{hr}")
    if len(laps) > max_laps:
        lines.append(f"  … ({len(laps) - max_laps} more laps trimmed)")
    return "\n".join(lines)
