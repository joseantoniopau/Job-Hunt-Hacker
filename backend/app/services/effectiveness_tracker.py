"""A/B effectiveness tracking — which resume gets the most replies?

Stores outcome events per (application, resume) pair. Aggregates funnels
(sent → replied → interviewed → offered) so we can rank resumes by
real-world performance — both per individual resume and per resume style
(`tailored_resume.resume_type`, the A/B variant axis).

Also records explicit user fit feedback per job ("good_fit" / "bad_fit"),
stored as `effectiveness_event` rows with outcome 'user_feedback_good' /
'user_feedback_bad'. The matching scorer (matching/scorer.py
`load_feedback_adjustments`) reads these to nudge future scores.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from ..db import _ensure_column, audit, get_conn, tx

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

# Explicit user fit verdicts per job — recorded via record_job_feedback().
FEEDBACK_OUTCOMES = {
    "user_feedback_good",
    "user_feedback_bad",
}

VERDICT_TO_OUTCOME = {
    "good_fit": "user_feedback_good",
    "bad_fit": "user_feedback_bad",
}

# Funnel stage ordering used by ab_summary(). Higher = further along.
# 'screened' is folded into the "replied" stage (employer responded).
_OUTCOME_STAGE = {
    "sent": 0,
    "replied": 1,
    "screened": 1,
    "interviewed": 2,
    "offered": 3,
    # A rejection or ghost implies the application WAS sent, but nothing more.
    "rejected": 0,
    "ghosted": 0,
}

# application.status (current + audit_json history entries) → funnel stage.
# saved / prepared / auto_packet_ready / archived carry no funnel evidence.
_STATUS_STAGE = {
    "applied": 0,
    "replied": 1,
    "screened": 1,
    "interview": 2,
    "offer": 3,
    "rejected": 0,  # employer rejection implies the application was sent
}


def _validate_outcome(outcome: str) -> str:
    s = (outcome or "").strip().lower()
    allowed = VALID_OUTCOMES | FEEDBACK_OUTCOMES
    if s not in allowed:
        raise ValueError(
            f"invalid outcome: {outcome!r}; must be one of {sorted(allowed)}"
        )
    return s


# ---- additive schema: effectiveness_event.job_id ----
# Older DBs predate job-level feedback; add the column lazily (idempotent,
# guarded by a lock so concurrent first-writes don't race the ALTER).
_schema_lock = threading.Lock()
_feedback_schema_ready = False


def _ensure_feedback_schema() -> None:
    global _feedback_schema_ready
    if _feedback_schema_ready:
        return
    with _schema_lock:
        if _feedback_schema_ready:
            return
        _ensure_column(get_conn(), "effectiveness_event", "job_id", "INTEGER")
        _feedback_schema_ready = True


def record(
    application_id: int | None,
    resume_id: int | None,
    outcome: str,
    notes: str = "",
    job_id: int | None = None,
) -> int:
    """Insert one effectiveness_event. Returns the new row id.

    `job_id` is optional and only stored when provided (the column is added
    additively on first use, so pre-existing DBs keep working untouched).
    """
    outcome = _validate_outcome(outcome)
    now = time.time()
    with tx() as conn:
        if job_id is not None:
            _ensure_feedback_schema()
            cur = conn.execute(
                "INSERT INTO effectiveness_event (ts, application_id, resume_id, outcome, notes, job_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now,
                    int(application_id) if application_id is not None else None,
                    int(resume_id) if resume_id is not None else None,
                    outcome,
                    notes or "",
                    int(job_id),
                ),
            )
        else:
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
              application_id=application_id, resume_id=resume_id, outcome=outcome,
              job_id=job_id)
    except Exception:
        pass
    return eid


def record_job_feedback(job_id: int, verdict: str, reason: str = "") -> int:
    """Record an explicit user fit verdict for a job.

    verdict: 'good_fit' | 'bad_fit' → stored as effectiveness_event with
    outcome 'user_feedback_good' / 'user_feedback_bad', notes=reason and
    job_id set. If the job has an application, the latest one (and its
    resume) is linked for traceability.

    Raises ValueError on a bad verdict, LookupError if the job is missing.
    Returns the new effectiveness_event id.
    """
    v = (verdict or "").strip().lower()
    if v not in VERDICT_TO_OUTCOME:
        raise ValueError(
            f"invalid verdict: {verdict!r}; must be one of {sorted(VERDICT_TO_OUTCOME)}"
        )
    conn = get_conn()
    job = conn.execute(
        "SELECT id FROM job_posting WHERE id = ?", (int(job_id),)
    ).fetchone()
    if job is None:
        raise LookupError(f"job_posting id={job_id} not found")
    app_row = conn.execute(
        "SELECT id, resume_id FROM application WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (int(job_id),),
    ).fetchone()
    application_id = int(app_row["id"]) if app_row else None
    resume_id = int(app_row["resume_id"]) if app_row and app_row["resume_id"] is not None else None
    eid = record(
        application_id,
        resume_id,
        VERDICT_TO_OUTCOME[v],
        notes=reason or "",
        job_id=int(job_id),
    )
    try:
        audit("job_feedback_recorded", "job_posting", int(job_id),
              verdict=v, reason=reason or "", event_id=eid)
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


# ---- A/B summary per resume style ------------------------------------------

def _statuses_from_history(status: str | None, audit_json: str | None) -> list[str]:
    """Extract every status the application ever held: current status +
    statuses recorded in the audit_json history (both 'created' entries,
    which carry a top-level "status", and 'update' entries, which carry
    fields={"status": ...}).
    """
    statuses: list[str] = []
    if status:
        statuses.append(str(status))
    try:
        hist = json.loads(audit_json) if audit_json else []
    except Exception:
        hist = []
    if isinstance(hist, list):
        for entry in hist:
            if not isinstance(entry, dict):
                continue
            s = entry.get("status")
            if s:
                statuses.append(str(s))
            fields = entry.get("fields")
            if isinstance(fields, dict) and fields.get("status"):
                statuses.append(str(fields["status"]))
    return statuses


def _style_of(resume_type: Any) -> str:
    s = (str(resume_type).strip() if resume_type is not None else "")
    return s or "unknown"


def ab_summary(min_n: int = 5) -> dict:
    """Per-style (tailored_resume.resume_type) A/B win-rate table.

    Unit of analysis: one application = one funnel record. For every
    application linked to a tailored resume, the furthest funnel stage is
    computed from BOTH its effectiveness events and its status history
    (current status + audit_json entries):

        stage 0 sent       <- event 'sent' / status 'applied'
                              (rejected/ghosted also imply sent)
        stage 1 replied    <- event 'replied'/'screened' / status 'replied'/'screened'
        stage 2 interviewed<- event 'interviewed' / status 'interview'
        stage 3 offered    <- event 'offered' / status 'offer'

    Counts are cumulative ("reached at least stage X"), so offered implies
    interviewed/replied/sent. Multiple events on one application are
    de-duplicated. Events without an application_id but with a resume_id
    ("orphan" manual recordings) each count as one independent funnel record.
    Feedback outcomes (user_feedback_*) are excluded from the funnel.

    Styles with sent < min_n are flagged insufficient_data with a caveat —
    win rates on tiny samples are noise, not signal.

    Returns:
        {"styles": [{style, sent, replied, interviewed, offered, rejected,
                     ghosted, reply_rate, interview_rate, offer_rate, n,
                     insufficient_data, caveat}],
         "min_n": int, "total_styles": int}
    Sorted: sufficient styles first, then by reply_rate desc, sent desc.
    """
    min_n = max(1, int(min_n))
    conn = get_conn()

    # 1) Applications joined to a styled resume → one unit each.
    units: dict[Any, dict] = {}
    app_rows = conn.execute(
        "SELECT a.id, a.status, a.audit_json, tr.resume_type "
        "FROM application a JOIN tailored_resume tr ON tr.id = a.resume_id"
    ).fetchall()
    for r in app_rows:
        unit = {"style": _style_of(r["resume_type"]), "max_stage": None,
                "rejected": False, "ghosted": False}
        for s in _statuses_from_history(r["status"], r["audit_json"]):
            s = s.strip().lower()
            if s == "rejected":
                unit["rejected"] = True
            stage = _STATUS_STAGE.get(s)
            if stage is not None and (unit["max_stage"] is None or stage > unit["max_stage"]):
                unit["max_stage"] = stage
        units[("app", int(r["id"]))] = unit

    # 2) Effectiveness events — fold into the application's unit when linked;
    #    otherwise each orphan event stands alone as its own funnel record.
    funnel_outcomes = sorted(VALID_OUTCOMES)
    placeholders = ",".join("?" for _ in funnel_outcomes)
    ev_rows = conn.execute(
        "SELECT e.id, e.application_id, e.resume_id, e.outcome, tr.resume_type "
        f"FROM effectiveness_event e "
        "LEFT JOIN tailored_resume tr ON tr.id = e.resume_id "
        f"WHERE e.outcome IN ({placeholders})",
        funnel_outcomes,
    ).fetchall()
    for r in ev_rows:
        outcome = r["outcome"]
        stage = _OUTCOME_STAGE.get(outcome)
        if stage is None:
            continue
        if r["application_id"] is not None:
            key = ("app", int(r["application_id"]))
            unit = units.get(key)
            if unit is None:
                # Application exists but isn't linked to a styled resume; use
                # the event's own resume style if it has one, else skip.
                if r["resume_id"] is None:
                    continue
                unit = {"style": _style_of(r["resume_type"]), "max_stage": None,
                        "rejected": False, "ghosted": False}
                units[key] = unit
        elif r["resume_id"] is not None:
            unit = {"style": _style_of(r["resume_type"]), "max_stage": None,
                    "rejected": False, "ghosted": False}
            units[("event", int(r["id"]))] = unit
        else:
            continue  # neither application nor resume — unattributable
        if outcome == "rejected":
            unit["rejected"] = True
        elif outcome == "ghosted":
            unit["ghosted"] = True
        if unit["max_stage"] is None or stage > unit["max_stage"]:
            unit["max_stage"] = stage

    # 3) Aggregate units per style with cumulative funnel counts.
    styles: dict[str, dict] = {}
    for unit in units.values():
        if unit["max_stage"] is None:
            continue  # never sent — not part of the A/B funnel
        b = styles.setdefault(unit["style"], {
            "style": unit["style"], "sent": 0, "replied": 0,
            "interviewed": 0, "offered": 0, "rejected": 0, "ghosted": 0,
        })
        b["sent"] += 1
        if unit["max_stage"] >= 1:
            b["replied"] += 1
        if unit["max_stage"] >= 2:
            b["interviewed"] += 1
        if unit["max_stage"] >= 3:
            b["offered"] += 1
        if unit["rejected"]:
            b["rejected"] += 1
        if unit["ghosted"]:
            b["ghosted"] += 1

    out: list[dict] = []
    for b in styles.values():
        sent = b["sent"]
        denom = sent or 1
        b["reply_rate"] = round(b["replied"] / denom, 4)
        b["interview_rate"] = round(b["interviewed"] / denom, 4)
        b["offer_rate"] = round(b["offered"] / denom, 4)
        b["n"] = sent
        b["insufficient_data"] = sent < min_n
        b["caveat"] = (
            f"Only {sent} application(s) — need at least {min_n} for a reliable win rate."
            if sent < min_n else ""
        )
        out.append(b)
    out.sort(key=lambda x: (not x["insufficient_data"], x["reply_rate"], x["sent"]),
             reverse=True)
    return {"styles": out, "min_n": min_n, "total_styles": len(out)}
