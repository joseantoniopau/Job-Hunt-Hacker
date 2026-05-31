"""Salary intelligence HTTP endpoints."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException

from ..services import salary_intelligence

log = logging.getLogger("jhh.routers.salary")

router = APIRouter(prefix="/api", tags=["salary"])


@router.get("/salary/market")
def market(
    role: str,
    location: Optional[str] = None,
    currency: str = "USD",
    window_days: int = 90,
) -> dict:
    if not role or not role.strip():
        raise HTTPException(400, "role is required")
    if window_days <= 0 or window_days > 3650:
        raise HTTPException(400, "window_days must be between 1 and 3650")
    data = salary_intelligence.compute_market(
        role=role, location=location, currency=currency, window_days=window_days
    )
    return {"ok": True, "data": data}


@router.get("/salary/summary")
def summary() -> dict:
    return {"ok": True, "data": salary_intelligence.comp_summary_for_profile()}
