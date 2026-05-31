"""A/B effectiveness tracking — which resume gets the most replies?

Stores outcome events per (application, resume) pair. Aggregates funnels
(sent → replied → interviewed → offered) so we can rank resumes by
real-world performance.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..db import audit, get_conn, tx

log = logging.getLogger("jhh.services.effectiveness_tracker")


VALID_OUTCOMES = {
    "sent",
    "replied",
    "screened",
    "interviewed",
    "offered",
    "rejected",
    "ghosted",
}


def _validate_outcome(outcome: str) -> str:
    s = (outcome or "").strip().lower()
    if s not in VALID_OUTCOMES:
        raise ValueError(
            f"invalid outcome: {outcome!r}; must be one of {sorted(VALID_OUTCOMES)}"
        )
    return s


def record(
    application_id: int | None,
    resume_id: int | None,
    outcome: str,
    notes: str = "",
) -> int:
    """Insert one effectiveness_event. Returns the new row id."""
    outcome = _validate_outcome(outcome)
    now = time.time()
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO effectiveness_event (ts, application_id, resume_id, outcome, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                now,
                int(application_id) if application_id is not None else None,
                int(resume_id) if resume_id is not None else None,
                outcome,
                notes or "",
            ),
        )
        eid = int(cur.lastrowid)
    try:
        audit("effectiveness_recorded", "effectiveness_event", eid,
              application_id=application_id, resume_id=resume_id, outcome=outcome)
    except Exception:
        pass
    return eid


def _counts_for(rows: list[Any]) -> dict[str, int]:
    counts = {k: 0 for k in VALID_OUTCOMES}
    for r in rows:
        o = r["outcome"] if hasattr(r, "keys") else r[0]
        if o in counts:
            counts[o] += 1
    return counts


def _stats_from_counts(counts: dict[str, int]) -> dict:
    sent = counts.get("sent", 0)
    replied = counts.get("replied", 0)
    screened = counts.get("screened", 0)
    interviewed = counts.get("interviewed", 0)
    offered = counts.get("offered", 0)
    rejected = counts.get("rejected", 0)
    ghosted = counts.get("ghosted", 0)
    denom = sent or 1
    return {
        "sent": sent,
        "replied": replied,
        "screened": screened,
        "interview": interviewed,
        "offer": offered,
        "rejected": rejected,
        "ghosted": ghosted,
        "reply_rate": round((replied + screened + interviewed + offered) / denom, 4) if sent else 0.0,
        "interview_rate": round((interviewed + offered) / denom, 4) if sent else 0.0,
        "offer_rate": round(offered / denom, 4) if sent else 0.0,
    }


def resume_stats(resume_id: int) -> dict:
    """Aggregate funnel stats for one resume."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT outcome FROM effectiveness_event WHERE resume_id = ?",
        (int(resume_id),),
    ).fetchall()
    counts = _counts_for(rows)
    out = _stats_from_counts(counts)
    out["resume_id"] = int(resume_id)
    return out


def all_resume_effectiveness(min_sent: int = 1) -> list[dict]:
    """Leaderboard: one row per resume_id, sorted by reply_rate desc.

    Resumes with `sent < min_sent` are excluded (otherwise a single reply
    on a 1-application resume dominates the chart).
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT resume_id, outcome FROM effectiveness_event WHERE resume_id IS NOT NULL"
    ).fetchall()
    by_resume: dict[int, dict[str, int]] = {}
    for r in rows:
        rid = int(r["resume_id"])
        bucket = by_resume.setdefault(rid, {k: 0 for k in VALID_OUTCOMES})
        o = r["outcome"]
        if o in bucket:
            bucket[o] += 1
    out: list[dict] = []
    for rid, counts in by_resume.items():
        if counts.get("sent", 0) < int(min_sent):
            continue
        stats = _stats_from_counts(counts)
        stats["resume_id"] = rid
        out.append(stats)
    out.sort(key=lambda x: (x["reply_rate"], x["sent"]), reverse=True)
    return out
