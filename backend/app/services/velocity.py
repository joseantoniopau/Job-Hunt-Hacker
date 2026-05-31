"""Application velocity + funnel analytics.

The headhunter equivalent: tracking how many candidates moved from
submission -> phone screen -> on-site -> offer over the last N weeks, and
calling out which stage is the bottleneck.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from ..db import get_conn

log = logging.getLogger("jhh.services.velocity")


# ---- helpers ----

def _iso_week(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    y, w, _ = dt.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _empty_week_bucket() -> dict:
    return {"applied": 0, "replied": 0, "interviews": 0, "offers": 0}


def _zero_funnel() -> dict:
    """Canonical empty shape returned when there is no application data."""
    return {
        "prepared": 0,
        "applied": 0,
        "replied": 0,
        "screened": 0,
        "interviewed": 0,
        "offered": 0,
        "rejected": 0,
        "ghosted": 0,
        "reply_rate": 0.0,
        "interview_rate": 0.0,
        "offer_rate": 0.0,
    }


# ---- public API ----

def weekly_velocity(weeks: int = 12) -> dict:
    """Per-ISO-week counts of applied / replied / interviews / offers over
    the trailing `weeks` weeks.
    """
    if weeks <= 0 or weeks > 520:
        raise ValueError("weeks must be between 1 and 520")
    cutoff = time.time() - (weeks * 7 * 86400)
    conn = get_conn()

    by_week: dict[str, dict] = defaultdict(_empty_week_bucket)

    # `applied` from application table — anything with an applied_at in the window.
    app_rows = conn.execute(
        """SELECT applied_at FROM application
           WHERE applied_at IS NOT NULL AND applied_at >= ?""",
        (cutoff,),
    ).fetchall()
    for r in app_rows:
        ts = r["applied_at"]
        by_week[_iso_week(ts)]["applied"] += 1

    # `replied / interviews / offers` from effectiveness_event in the window.
    ev_rows = conn.execute(
        """SELECT ts, outcome FROM effectiveness_event
           WHERE ts >= ?""",
        (cutoff,),
    ).fetchall()
    for r in ev_rows:
        wk = _iso_week(r["ts"])
        o = r["outcome"]
        if o in ("replied", "screened"):
            by_week[wk]["replied"] += 1
        elif o == "interviewed":
            by_week[wk]["interviews"] += 1
        elif o == "offered":
            by_week[wk]["offers"] += 1

    # Order weeks chronologically
    sorted_weeks = sorted(by_week.keys())
    series = [{"iso_week": w, **by_week[w]} for w in sorted_weeks]

    totals = {
        "applied": sum(b["applied"] for b in by_week.values()),
        "replied": sum(b["replied"] for b in by_week.values()),
        "interviews": sum(b["interviews"] for b in by_week.values()),
        "offers": sum(b["offers"] for b in by_week.values()),
    }
    return {
        "weeks": weeks,
        "by_week": series,
        "totals": totals,
    }


def funnel() -> dict:
    """Lifetime funnel: prepared -> applied -> replied -> screened -> interview
    -> offer -> rejected/ghosted, with conversion rates relative to applied."""
    conn = get_conn()

    # Application statuses give us prepared + applied (status-driven counts).
    app_status_rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM application GROUP BY status"
    ).fetchall()
    by_status: dict[str, int] = {r["status"]: int(r["c"]) for r in app_status_rows}

    prepared = by_status.get("prepared", 0)
    # `applied` count = any app whose status is past prepared. We treat
    # explicit applied_at as the canonical signal so manual data entry
    # doesn't double-count.
    applied = conn.execute(
        "SELECT COUNT(*) FROM application WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    # Outcome counts from effectiveness_event (lifetime).
    out_rows = conn.execute(
        "SELECT outcome, COUNT(*) AS c FROM effectiveness_event GROUP BY outcome"
    ).fetchall()
    outcomes: dict[str, int] = {r["outcome"]: int(r["c"]) for r in out_rows}

    if applied == 0 and not outcomes:
        return _zero_funnel()

    replied = outcomes.get("replied", 0)
    screened = outcomes.get("screened", 0)
    interviewed = outcomes.get("interviewed", 0)
    offered = outcomes.get("offered", 0)
    rejected = outcomes.get("rejected", 0)
    ghosted = outcomes.get("ghosted", 0)

    denom = applied or 1
    reply_rate = round((replied + screened + interviewed + offered) / denom, 4) if applied else 0.0
    interview_rate = round((interviewed + offered) / denom, 4) if applied else 0.0
    offer_rate = round(offered / denom, 4) if applied else 0.0

    return {
        "prepared": prepared,
        "applied": applied,
        "replied": replied,
        "screened": screened,
        "interviewed": interviewed,
        "offered": offered,
        "rejected": rejected,
        "ghosted": ghosted,
        "reply_rate": reply_rate,
        "interview_rate": interview_rate,
        "offer_rate": offer_rate,
    }


# Heuristic thresholds (loosely calibrated against industry recruiting averages).
# These are intentionally conservative — we'd rather under-flag than tell a
# user they're "doing fine" when they're stuck.
_THRESHOLD_REPLY_RATE = 0.08    # ~8% reply rate is industry "ok"
_THRESHOLD_INTERVIEW_RATE = 0.03  # ~3% interview rate is industry "ok"
_THRESHOLD_OFFER_RATE = 0.01     # ~1% offer rate from applied


def bottleneck_analysis() -> dict:
    """Identify the weakest funnel stage with a one-line diagnosis."""
    f = funnel()
    applied = f.get("applied", 0)
    if applied < 5:
        return {
            "stage": "data",
            "diagnosis": (
                "Not enough applications yet to analyze a bottleneck — "
                "send at least 5–10 applications and revisit."
            ),
            "funnel": f,
        }

    reply_rate = f.get("reply_rate", 0.0)
    interview_rate = f.get("interview_rate", 0.0)
    offer_rate = f.get("offer_rate", 0.0)

    if reply_rate < _THRESHOLD_REPLY_RATE:
        return {
            "stage": "reply",
            "diagnosis": (
                f"Reply rate is {reply_rate * 100:.1f}% (below the ~8% baseline). "
                "Resume/cover letter are likely the bottleneck — try tightening keyword "
                "coverage and the opening summary, and check that you're applying to "
                "roles where your evidence actually matches the listing."
            ),
            "funnel": f,
        }
    if interview_rate < _THRESHOLD_INTERVIEW_RATE:
        return {
            "stage": "screen",
            "diagnosis": (
                f"You're getting replies ({reply_rate * 100:.1f}%) but only "
                f"{interview_rate * 100:.1f}% reach interviews. Phone screens are "
                "the bottleneck — practice the 90-second pitch and the 'why this "
                "company' answer with the interview prep tool."
            ),
            "funnel": f,
        }
    if offer_rate < _THRESHOLD_OFFER_RATE:
        return {
            "stage": "interview",
            "diagnosis": (
                f"Interviews are happening ({interview_rate * 100:.1f}%) but offers "
                f"({offer_rate * 100:.1f}%) are rare. Focus on technical/role-specific "
                "interview practice and post-interview followups."
            ),
            "funnel": f,
        }
    return {
        "stage": "healthy",
        "diagnosis": (
            "Funnel is healthy across all stages. Keep the cadence up and focus "
            "on negotiating the offers you do receive."
        ),
        "funnel": f,
    }


__all__ = ["weekly_velocity", "funnel", "bottleneck_analysis"]
