"""A/B effectiveness endpoints. Wraps services.effectiveness_tracker."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import effectiveness_tracker

log = logging.getLogger("jhh.routers.effectiveness")

router = APIRouter(prefix="/api", tags=["effectiveness"])


class EffectivenessRecordBody(BaseModel):
    application_id: Optional[int] = None
    resume_id: Optional[int] = None
    outcome: str
    notes: Optional[str] = None
    job_id: Optional[int] = None


class JobFeedbackBody(BaseModel):
    job_id: int
    verdict: str  # 'good_fit' | 'bad_fit'
    reason: Optional[str] = None


@router.post("/effectiveness/record")
def record(body: EffectivenessRecordBody) -> dict:
    """Record one outcome event.

    Request: {application_id?, resume_id?, outcome, notes?, job_id?}
    where outcome is one of sent/replied/screened/interviewed/offered/
    rejected/ghosted (or user_feedback_good/user_feedback_bad).
    Response: {"ok": true, "data": {"id": <event_id>}}
    """
    try:
        eid = effectiveness_tracker.record(
            body.application_id, body.resume_id, body.outcome,
            body.notes or "", job_id=body.job_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": {"id": eid}}


@router.get("/effectiveness/ab")
def ab_summary(min_n: int = 5) -> dict:
    """A/B win-rate table per resume style (tailored_resume.resume_type).

    Query: ?min_n=5 — styles with fewer than min_n sent applications are
    flagged insufficient_data with a caveat string.
    Response: {"ok": true, "data": {"styles": [{style, sent, replied,
    interviewed, offered, rejected, ghosted, reply_rate, interview_rate,
    offer_rate, n, insufficient_data, caveat}], "min_n", "total_styles"}}
    """
    if min_n < 1:
        raise HTTPException(400, "min_n must be >= 1")
    return {"ok": True, "data": effectiveness_tracker.ab_summary(min_n=min_n)}


@router.post("/effectiveness/job-feedback")
def job_feedback(body: JobFeedbackBody) -> dict:
    """Record an explicit user fit verdict for a job (feedback loop input).

    Request: {"job_id": int, "verdict": "good_fit"|"bad_fit", "reason"?: str}
    Stored as an effectiveness_event (outcome user_feedback_good/_bad,
    notes=reason). The scorer reads these to adjust future match scores.
    Response: {"ok": true, "data": {"id", "job_id", "verdict", "outcome"}}
    Errors: 400 invalid verdict, 404 unknown job.
    """
    try:
        eid = effectiveness_tracker.record_job_feedback(
            body.job_id, body.verdict, body.reason or ""
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "ok": True,
        "data": {
            "id": eid,
            "job_id": body.job_id,
            "verdict": body.verdict.strip().lower(),
            "outcome": effectiveness_tracker.VERDICT_TO_OUTCOME[body.verdict.strip().lower()],
        },
    }


@router.get("/effectiveness/feedback-summary")
def feedback_summary() -> dict:
    """Per role-family feedback aggregates + the scoring adjustment factors
    the scorer derives from them (see matching.scorer.load_feedback_adjustments).

    Response: {"ok": true, "data": {"families": {<family>: {good, bad, n,
    signal, factor, active}}, "min_events", "max_shift"}}
    """
    from ..matching import scorer as _scorer
    return {
        "ok": True,
        "data": {
            "families": _scorer.load_feedback_adjustments(),
            "min_events": _scorer.FEEDBACK_MIN_EVENTS,
            "max_shift": _scorer.FEEDBACK_MAX_SHIFT,
        },
    }


@router.get("/resumes/{resume_id}/effectiveness")
def get_resume_effectiveness(resume_id: int) -> dict:
    return {"ok": True, "data": effectiveness_tracker.resume_stats(resume_id)}


@router.get("/effectiveness/leaderboard")
def leaderboard(min_sent: int = 3) -> dict:
    if min_sent < 0:
        raise HTTPException(400, "min_sent must be >= 0")
    rows = effectiveness_tracker.all_resume_effectiveness(min_sent=min_sent)
    return {"ok": True, "data": rows, "count": len(rows)}
