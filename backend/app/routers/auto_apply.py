"""Auto-apply control endpoints. Heavy gating + kill switch."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..applications import auto_apply, compliance, pipeline

log = logging.getLogger("jhh.routers.auto_apply")

router = APIRouter(prefix="/api/auto-apply", tags=["auto-apply"])


class RunBody(BaseModel):
    job_ids: Optional[list[int]] = None


class ResumeBody(BaseModel):
    i_understand: bool = False


@router.get("/status")
def status() -> dict:
    return {"ok": True, "data": auto_apply.status()}


@router.post("/run")
def run(body: RunBody) -> dict:
    res = auto_apply.attempt(body.job_ids if body.job_ids else None)
    return {"ok": bool(res.get("ok")), "data": res}


@router.post("/halt")
def halt() -> dict:
    compliance.halt()
    return {"ok": True, "detail": "kill switch activated", "data": compliance.status_snapshot()}


@router.post("/resume")
def resume(body: ResumeBody) -> dict:
    if not body.i_understand:
        raise HTTPException(400, "explicit confirmation required: send {\"i_understand\": true}")
    ok = compliance.resume(i_understand=True)
    if not ok:
        raise HTTPException(500, "could not lift kill switch")
    return {"ok": True, "detail": "kill switch lifted", "data": compliance.status_snapshot()}


@router.get("/queue")
def queue() -> dict:
    rows = auto_apply.queue(limit=200)
    return {"ok": True, "data": rows, "count": len(rows)}
