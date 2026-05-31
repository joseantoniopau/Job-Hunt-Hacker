"""Application velocity / funnel HTTP endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..services import velocity

log = logging.getLogger("jhh.routers.velocity")

router = APIRouter(prefix="/api", tags=["velocity"])


@router.get("/velocity/weekly")
def weekly(weeks: int = 12) -> dict:
    try:
        data = velocity.weekly_velocity(weeks=weeks)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": data}


@router.get("/velocity/funnel")
def funnel() -> dict:
    return {"ok": True, "data": velocity.funnel()}


@router.get("/velocity/bottleneck")
def bottleneck() -> dict:
    return {"ok": True, "data": velocity.bottleneck_analysis()}
