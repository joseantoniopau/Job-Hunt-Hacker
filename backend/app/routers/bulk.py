"""Bulk operations across jobs / applications.

Wraps the existing per-id mutation paths so callers can fan out without
making N HTTP calls. Each endpoint returns a {touched, failed} report
so the UI can render partial-success messaging."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..applications import pipeline as app_pipeline
from ..db import audit
from ..services.job_sources.pipeline import update_status as update_job_status

log = logging.getLogger("jhh.routers.bulk")

router = APIRouter(prefix="/api/bulk", tags=["bulk"])


class BulkJobStatusBody(BaseModel):
    job_ids: list[int] = Field(default_factory=list)
    status: str


class BulkAppStatusBody(BaseModel):
    application_ids: list[int] = Field(default_factory=list)
    status: str


class BulkJobDeleteBody(BaseModel):
    job_ids: list[int] = Field(default_factory=list)


def _wrap_bulk(touched: int, failed: list[dict], action: str, **detail) -> dict:
    try:
        audit(action, "bulk", None, touched=touched, failed_count=len(failed), **detail)
    except Exception:
        pass
    return {"ok": True, "data": {"touched": touched, "failed": failed}}


@router.post("/jobs/status")
def bulk_jobs_status(body: BulkJobStatusBody) -> dict:
    if not body.job_ids:
        raise HTTPException(400, "job_ids required")
    if not (body.status or "").strip():
        raise HTTPException(400, "status required")
    touched = 0
    failed: list[dict] = []
    for jid in body.job_ids:
        try:
            ok = update_job_status(int(jid), body.status)
            if ok:
                touched += 1
            else:
                failed.append({"id": int(jid), "reason": "not found"})
        except Exception as exc:  # noqa: BLE001
            failed.append({"id": int(jid), "reason": f"{type(exc).__name__}: {exc}"})
    return _wrap_bulk(touched, failed, "bulk_jobs_status", status=body.status)


@router.post("/applications/status")
def bulk_applications_status(body: BulkAppStatusBody) -> dict:
    if not body.application_ids:
        raise HTTPException(400, "application_ids required")
    if not (body.status or "").strip():
        raise HTTPException(400, "status required")
    touched = 0
    failed: list[dict] = []
    for aid in body.application_ids:
        try:
            app_pipeline.update_application(int(aid), {"status": body.status})
            touched += 1
        except LookupError as exc:
            failed.append({"id": int(aid), "reason": str(exc)})
        except ValueError as exc:
            failed.append({"id": int(aid), "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001
            failed.append({"id": int(aid), "reason": f"{type(exc).__name__}: {exc}"})
    return _wrap_bulk(touched, failed, "bulk_applications_status", status=body.status)


@router.post("/jobs/delete")
def bulk_jobs_delete(body: BulkJobDeleteBody) -> dict:
    """Soft delete: same as setting status=archived. The cascade in
    update_job_status will archive linked applications too."""
    if not body.job_ids:
        raise HTTPException(400, "job_ids required")
    touched = 0
    failed: list[dict] = []
    for jid in body.job_ids:
        try:
            ok = update_job_status(int(jid), "archived")
            if ok:
                touched += 1
            else:
                failed.append({"id": int(jid), "reason": "not found"})
        except Exception as exc:  # noqa: BLE001
            failed.append({"id": int(jid), "reason": f"{type(exc).__name__}: {exc}"})
    return _wrap_bulk(touched, failed, "bulk_jobs_delete")
