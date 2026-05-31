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


@router.post("/effectiveness/record")
def record(body: EffectivenessRecordBody) -> dict:
    try:
        eid = effectiveness_tracker.record(
            body.application_id, body.resume_id, body.outcome, body.notes or ""
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": {"id": eid}}


@router.get("/resumes/{resume_id}/effectiveness")
def get_resume_effectiveness(resume_id: int) -> dict:
    return {"ok": True, "data": effectiveness_tracker.resume_stats(resume_id)}


@router.get("/effectiveness/leaderboard")
def leaderboard(min_sent: int = 3) -> dict:
    if min_sent < 0:
        raise HTTPException(400, "min_sent must be >= 0")
    rows = effectiveness_tracker.all_resume_effectiveness(min_sent=min_sent)
    return {"ok": True, "data": rows, "count": len(rows)}
