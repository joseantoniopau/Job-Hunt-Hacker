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


class SavedSearchCreate(BaseModel):
    label: str
    query: JobSearchRequest
    frequency_hours: int = 24
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


@router.post("/inbox-sweep")
def inbox_sweep() -> dict:
    return {"ok": True, "data": sched.run_inbox_sweep()}


@router.post("/followups")
def followups() -> dict:
    return {"ok": True, "data": sched.run_followups()}
