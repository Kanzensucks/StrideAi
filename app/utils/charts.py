"""Weekly training load chart — multi-user, unit-aware."""

import io
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

from app.core import user_store

BG = "#FAFAF7"
INK = "#1A1A1A"
MUTED = "#6B6B6B"
GRID = "#E3E0D8"
RUN = "#FC5200"
RIDE = "#2D7DD2"
OTHER = "#8E8E8E"
TARGET = "#1A1A1A"


def _week_start_for(dt):
    return (dt - timedelta(days=dt.weekday())).date()


def _classify(activity):
    sport = (activity.get("sport_type") or activity.get("type") or "").lower()
    if any(k in sport for k in ["weight", "strength", "crossfit", "gym"]):
        return None
    if "run" in sport:
        return "run"
    if any(k in sport for k in ["ride", "bike", "cycl"]):
        return "ride"
    return "other"


def _bucket_by_week(activities, num_weeks=8, units="km"):
    buckets = {}
    for a in activities:
        start = datetime.fromisoformat(a["start_date_local"].replace("Z", "+00:00"))
        ws = _week_start_for(start.replace(tzinfo=None))
        b = buckets.setdefault(ws, {"run_dist": 0.0, "run_h": 0.0, "ride_h": 0.0, "other_h": 0.0})
        kind = _classify(a)
        if kind is None:
            continue
        hours = (a.get("moving_time", 0) or 0) / 3600
        km = (a.get("distance", 0) or 0) / 1000
        dist = km * 0.621371 if units == "mi" else km
        if kind == "run":
            b["run_dist"] += dist
            b["run_h"] += hours
        elif kind == "ride":
            b["ride_h"] += hours
        else:
            b["other_h"] += hours
    today = datetime.now()
    for i in range(num_weeks):
        ws = _week_start_for(today - timedelta(weeks=i))
        buckets.setdefault(ws, {"run_dist": 0.0, "run_h": 0.0, "ride_h": 0.0, "other_h": 0.0})
    return sorted(buckets.items())[-num_weeks:]


def _get_plan_targets(chat_id, weeks, units):
    plan = user_store.get_plan(chat_id)
    if not plan or "weeks" not in plan:
        return [0] * len(weeks)
    targets = []
    for ws in weeks:
        ws_str = ws.strftime("%Y-%m-%d")
        target = 0
        for w in plan["weeks"]:
            if w.get("start_date") == ws_str:
                key = f"target_volume_{'mi' if units == 'mi' else 'km'}"
                target = w.get(key) or w.get("target_volume_km", 0)
                if units == "mi" and key == "target_volume_km":
                    target = round(target * 0.621371, 1)
                break
        targets.append(target)
    return targets


def _style_axis(ax):
    ax.set_facecolor(BG)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    for spine in ["left", "bottom"]:
        ax.spines[spine].set_color(GRID)
        ax.spines[spine].set_linewidth(1)
    ax.tick_params(colors=MUTED, labelsize=10, length=0)
    ax.yaxis.label.set_color(MUTED)
    ax.grid(axis="y", linestyle="-", linewidth=0.8, color=GRID, alpha=0.8)
    ax.set_axisbelow(True)


def generate_weekly_mileage_chart(chat_id: str, activities: list, num_weeks: int = 8) -> bytes:
    profile = user_store.get_profile(chat_id)
    units = profile.get("units", "km")
    data = _bucket_by_week(activities, num_weeks, units)
    weeks = [w for w, _ in data]
    run_dist = [b["run_dist"] for _, b in data]
    run_h = [b["run_h"] for _, b in data]
    ride_h = [b["ride_h"] for _, b in data]
    other_h = [b["other_h"] for _, b in data]
    total_h = [r + rd + o for r, rd, o in zip(run_h, ride_h, other_h)]
    targets = _get_plan_targets(chat_id, weeks, units)
    labels = [f"{w.strftime('%b %d')}\n– {(w + timedelta(days=6)).strftime('%b %d')}" for w in weeks]
    x = list(range(len(weeks)))

    fig_width = max(11, min(20, 1.3 * len(weeks) + 3))
    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(fig_width, 9), dpi=140,
        gridspec_kw={"height_ratios": [1.05, 1], "hspace": 0.22},
    )
    fig.patch.set_facecolor(BG)

    _style_axis(ax_top)
    bars = ax_top.bar(x, run_dist, color=RUN, width=0.62, zorder=3, edgecolor="none")
    ymax_top = max(run_dist + targets + [10])
    for bar, val in zip(bars, run_dist):
        if val > 0.5:
            ax_top.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + ymax_top * 0.025,
                        f"{val:.0f}", ha="center", va="bottom", fontsize=10.5, fontweight="bold", color=INK)
    if any(t > 0 for t in targets):
        ax_top.plot(x, targets, "o--", color=TARGET, linewidth=1.6,
                    markersize=6, markerfacecolor=BG, markeredgewidth=1.6, zorder=4)
        ax_top.legend(
            handles=[Line2D([0], [0], color=TARGET, linestyle="--", marker="o",
                             markerfacecolor=BG, markeredgewidth=1.6, markersize=6, linewidth=1.6, label="Plan target")],
            loc="upper left", frameon=False, fontsize=10, handlelength=2.2, handletextpad=0.6,
        )
    ax_top.set_ylabel(f"Run {units}", fontsize=10.5, color=MUTED, labelpad=8)
    ax_top.set_xticks(x)
    ax_top.set_xticklabels([])
    ax_top.set_ylim(0, ymax_top * 1.22)

    _style_axis(ax_bot)
    ax_bot.bar(x, run_h, color=RUN, width=0.62, zorder=3, edgecolor="none")
    ax_bot.bar(x, ride_h, bottom=run_h, color=RIDE, width=0.62, zorder=3, edgecolor="none")
    ax_bot.bar(x, other_h, bottom=[r + rd for r, rd in zip(run_h, ride_h)],
               color=OTHER, width=0.62, zorder=3, edgecolor="none")
    tmax = max(total_h + [1])
    for xi, total in zip(x, total_h):
        if total > 0.05:
            ax_bot.text(xi, total + tmax * 0.03, f"{total:.1f}h",
                        ha="center", va="bottom", fontsize=10.5, fontweight="bold", color=INK)
    ax_bot.set_ylabel("Training hours", fontsize=10.5, color=MUTED, labelpad=8)
    ax_bot.set_xticks(x)
    ax_bot.set_xticklabels(labels, fontsize=10, color=INK)
    ax_bot.set_ylim(0, tmax * 1.22)
    ax_bot.legend(
        handles=[Patch(facecolor=RUN, label="Run"), Patch(facecolor=RIDE, label="Bike"),
                 Patch(facecolor=OTHER, label="Other")],
        loc="upper left", frameon=False, fontsize=10, ncol=3,
        handlelength=1.2, handletextpad=0.5, columnspacing=1.4,
    )

    profile_goal = profile.get("goal_time", "")
    race_name = profile.get("race_name", "")
    subtitle = "  ·  ".join(filter(None, [
        f"Last {num_weeks} weeks",
        f"{weeks[0].strftime('%b %d')} – {(weeks[-1] + timedelta(days=6)).strftime('%b %d')}" if weeks else "",
        f"{sum(run_dist):.0f}{units} run",
        f"{sum(total_h):.1f}h total",
        f"Goal: {race_name} {profile_goal}" if race_name and profile_goal else "",
    ]))
    fig.text(0.065, 0.965, "WEEKLY TRAINING LOAD", fontsize=17, fontweight="bold", color=INK)
    fig.text(0.065, 0.936, subtitle, fontsize=10.5, color=MUTED)

    plt.tight_layout(rect=[0.04, 0.02, 0.98, 0.90])
    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
