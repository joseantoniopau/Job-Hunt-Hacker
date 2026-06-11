"""Scheduler + saved-search router."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..integrations import scheduler as sched
from ..models.schemas import JobSearchRequest

log = logging.getLogger("jhh.routers.scheduler")

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


from pydantic import Field


class SavedSearchCreate(BaseModel):
    # `min_length=1` rejects an empty label.
    label: str = Field(..., min_length=1, max_length=200)
    query: JobSearchRequest
    # frequency_hours must be positive, capped at 1 year of hours so a
    # rogue value can't flood the scheduler.
    frequency_hours: int = Field(default=24, ge=1, le=8760)
    enabled: bool = True


@router.get("/status")
def status() -> dict:
    return {"ok": True, "data": sched.status()}


@router.get("/saved-searches")
def list_searches() -> dict:
    return {"ok": True, "data": sched.list_saved_searches()}


@router.post("/saved-searches")
def create_search(body: SavedSearchCreate) -> dict:
    try:
        sid = sched.create_saved_search(
            label=body.label,
            query=body.query.model_dump(),
            frequency_hours=int(body.frequency_hours),
            enabled=bool(body.enabled),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": {"id": sid}}


class SavedSearchUpdate(BaseModel):
    enabled: Optional[bool] = None
    frequency_hours: Optional[int] = Field(default=None, ge=1, le=8760)


@router.patch("/saved-searches/{sid}")
def update_search(sid: int, body: SavedSearchUpdate) -> dict:
    if body.enabled is None and body.frequency_hours is None:
        raise HTTPException(400, "nothing to update: provide enabled and/or frequency_hours")
    rec = sched.update_saved_search(
        int(sid), enabled=body.enabled, frequency_hours=body.frequency_hours,
    )
    if rec is None:
        raise HTTPException(404, f"saved_search {sid} not found")
    return {"ok": True, "data": rec}


@router.delete("/saved-searches/{sid}")
def delete_search(sid: int) -> dict:
    ok = sched.delete_saved_search(int(sid))
    if not ok:
        raise HTTPException(404, f"saved_search {sid} not found")
    return {"ok": True, "detail": "deleted"}


@router.post("/run-now/{sid}")
def run_now(sid: int) -> dict:
    res = sched.run_saved_search_now(int(sid))
    if not res.get("ok"):
        raise HTTPException(400, res.get("detail") or "run failed")
    return {"ok": True, "data": res}


@router.post("/saved-searches/{sid}/dry-run")
def dry_run(sid: int, results_cap: int = 5) -> dict:
    """Preview what a saved search would pull WITHOUT persisting anything.

    Response data: {would_insert, duplicates, discovered, top: [{title,
    company, url} x5], per_source, errors}. `results_cap` (default 5) keeps
    the preview cheap. Lets the user sanity-check a query before scheduling.
    """
    res = sched.dry_run_saved_search(int(sid), results_cap=max(1, min(int(results_cap), 25)))
    if not res.get("ok"):
        raise HTTPException(404, res.get("detail") or "saved search not found")
    return {"ok": True, "data": res}


@router.post("/inbox-sweep")
def inbox_sweep() -> dict:
    return {"ok": True, "data": sched.run_inbox_sweep()}


@router.post("/followups")
def followups() -> dict:
    return {"ok": True, "data": sched.run_followups()}
