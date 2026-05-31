"""GET/PATCH/DELETE /api/jobs ..."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.job_sources.pipeline import (
    get_job,
    list_jobs,
    update_status,
)

log = logging.getLogger("jhh.routers.jobs")

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class StatusUpdate(BaseModel):
    status: str


class RescoreRequest(BaseModel):
    job_ids: list[int]


def _alias_job(row: dict) -> dict:
    """Add UI-friendly aliases so the frontend can rely on stable field
    names (score, url, currency, is_remote, created_at) regardless of the
    underlying DB column names.

    Score scale convention: the scorer persists overall_score on a 0-1
    scale; UI consumes 0-100. We multiply at the API boundary.
    """
    if not row:
        return row
    out = dict(row)
    if "overall_score" in out and "score" not in out:
        v = out["overall_score"]
        out["score"] = int(round(float(v) * 100)) if v is not None else None
    if "apply_url" in out and "url" not in out:
        out["url"] = out["apply_url"]
    if "currency" in out and "salary_currency" not in out:
        out["salary_currency"] = out["currency"]
    if "discovered_at" in out and "created_at" not in out:
        out["created_at"] = out["discovered_at"]
    if "remote_type" in out:
        rt = (out.get("remote_type") or "").lower()
        out["is_remote"] = rt == "remote"
    return out


@router.get("")
def list_endpoint(
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    min_score: Optional[int] = None,
) -> dict:
    rows = list_jobs(
        limit=limit, status=status, source=source, min_score=min_score, offset=offset
    )
    rows = [_alias_job(r) for r in rows]
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/{job_id}")
def get_endpoint(job_id: int) -> dict:
    row = get_job(job_id)
    if not row:
        raise HTTPException(404, f"job {job_id} not found")
    return {"ok": True, "data": _alias_job(row)}


@router.patch("/{job_id}/status")
def patch_status(job_id: int, body: StatusUpdate) -> dict:
    ok = update_status(job_id, body.status)
    if not ok:
        raise HTTPException(404, f"job {job_id} not found")
    return {"ok": True, "detail": f"status set to {body.status}"}


@router.delete("/{job_id}")
def delete_endpoint(job_id: int) -> dict:
    ok = update_status(job_id, "archived")
    if not ok:
        raise HTTPException(404, f"job {job_id} not found")
    return {"ok": True, "detail": "archived"}


@router.post("/rescore")
def rescore(body: RescoreRequest) -> dict:
    scored: list[int] = []
    errors: dict[int, str] = {}
    try:
        from ..matching import scorer  # type: ignore
    except Exception:
        scorer = None  # type: ignore
    if scorer is None or not hasattr(scorer, "score_job"):
        return {"ok": True, "data": {"scored": [], "skipped": body.job_ids,
                                     "detail": "scorer module not available"}}
    for jid in body.job_ids:
        try:
            scorer.score_job(int(jid))
            scored.append(int(jid))
        except Exception as exc:  # noqa: BLE001
            errors[int(jid)] = f"{type(exc).__name__}: {exc}"
    return {"ok": True, "data": {"scored": scored, "errors": errors}}
