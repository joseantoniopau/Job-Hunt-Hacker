"""Auto-apply control endpoints. Heavy gating + kill switch."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..applications import auto_apply, compliance, pipeline
from ..config import settings
from ..db import audit

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
    # "Resume" = (a) lift kill switch if set, AND (b) enable auto-apply at
    # runtime so subsequent /run calls actually attempt. This matches the
    # UI's modal flow (typed-ENABLE confirm) and avoids requiring the user
    # to edit .env and restart.
    compliance.resume(i_understand=True)
    settings.auto_apply_enabled = True
    _persist_enabled_flag(True)
    audit("auto_apply_enabled", "settings", None, source="ui")
    return {"ok": True, "detail": "auto-apply enabled and kill switch lifted",
            "data": compliance.status_snapshot()}


@router.post("/disable")
def disable() -> dict:
    """Disable auto-apply at runtime. /run will refuse subsequent calls."""
    settings.auto_apply_enabled = False
    _persist_enabled_flag(False)
    audit("auto_apply_disabled", "settings", None, source="ui")
    return {"ok": True, "detail": "auto-apply disabled",
            "data": compliance.status_snapshot()}


def _persist_enabled_flag(enabled: bool) -> None:
    """Write the toggle through to .env so it survives a restart — without
    this, a UI enable/disable silently reverts on the next boot."""
    try:
        from .settings import _write_env
        _write_env({"JHH_AUTO_APPLY_ENABLED": "true" if enabled else "false"})
    except Exception as exc:  # noqa: BLE001
        log.warning("could not persist JHH_AUTO_APPLY_ENABLED to .env: %s", exc)


@router.get("/queue")
def queue() -> dict:
    rows = auto_apply.queue(limit=200)
    return {"ok": True, "data": rows, "count": len(rows)}
