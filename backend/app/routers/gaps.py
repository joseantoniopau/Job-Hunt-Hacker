"""Skill-gap trend endpoints. Wraps services.gap_tracker."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import gap_tracker

log = logging.getLogger("jhh.routers.gaps")

router = APIRouter(prefix="/api/gaps", tags=["gaps"])


class GapRecordBody(BaseModel):
    job_id: Optional[int] = None
    missing: list[str] = Field(default_factory=list)


@router.get("/top")
def top(days: int = 30, limit: int = 10) -> dict:
    if days < 0 or limit <= 0:
        raise HTTPException(400, "days must be >= 0, limit must be > 0")
    rows = gap_tracker.top_gaps(days=days, limit=limit)
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/trend")
def trend(days: int = 30) -> dict:
    if days < 0:
        raise HTTPException(400, "days must be >= 0")
    return {"ok": True, "data": gap_tracker.trend(days=days)}


@router.post("/record")
def record(body: GapRecordBody) -> dict:
    n = gap_tracker.record_gaps(body.job_id, body.missing or [])
    return {"ok": True, "data": {"recorded": n}}
