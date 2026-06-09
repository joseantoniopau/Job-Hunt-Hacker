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


class BulkStatusRequest(BaseModel):
    job_ids: list[int]
    status: str


@router.post("/bulk-status")
def bulk_status(body: BulkStatusRequest) -> dict:
    """Move a batch of jobs to a status (typically 'dismissed' or 'saved').

    Used by the Dashboard highlight flow: user selects N rows and clicks
    DISMISS SELECTED or SAVE SELECTED.
    """
    ok, errors = [], {}
    for jid in body.job_ids:
        if update_status(int(jid), body.status):
            ok.append(int(jid))
        else:
            errors[int(jid)] = "not found"
    return {"ok": True, "data": {"updated": ok, "errors": errors,
                                 "status": body.status}}


@router.post("/refresh")
async def refresh_jobs(top_n: int = 25, hours_old: int = 168) -> dict:
    """Re-run the user's saved-search query but EXCLUDE jobs the user has
    already dismissed. Used by the Dashboard REFRESH button.

    The exclusion works at persist time: any record whose apply_url or
    (source, external_id) tuple matches a dismissed job_posting row is
    skipped during insert. Dismissed rows themselves are not re-fetched.
    """
    from ..db import get_conn, row_to_dict
    from ..services.job_sources import REGISTRY
    from ..services.job_sources.pipeline import search_all, persist
    from ..services.job_sources.base import JobSearchQuery

    conn = get_conn()
    prof_row = conn.execute(
        "SELECT target_titles, target_keywords, preferred_locations, location FROM user_profile WHERE id=1"
    ).fetchone()
    prof = row_to_dict(prof_row) or {}
    import json as _json
    try:
        targets = _json.loads(prof.get("target_titles") or "[]")
    except Exception:
        targets = []
    try:
        keywords = _json.loads(prof.get("target_keywords") or "[]")
    except Exception:
        keywords = []
    locations_pref = ""
    try:
        prefs = _json.loads(prof.get("preferred_locations") or "[]")
        if prefs:
            locations_pref = prefs[0]
    except Exception:
        pass

    queries = [t for t in (targets or []) if t][:3]
    if not queries and keywords:
        queries = [" ".join(keywords[:3])]
    if not queries:
        queries = ["engineer"]

    # Pre-build dismissed exclusion set (composite key sources used at persist)
    dismissed_rows = conn.execute(
        "SELECT source, external_id, apply_url FROM job_posting WHERE status = 'dismissed'"
    ).fetchall()
    dismissed_keys = set()
    dismissed_urls = set()
    for r in dismissed_rows:
        if r["external_id"]:
            dismissed_keys.add((r["source"], r["external_id"]))
        if r["apply_url"]:
            dismissed_urls.add(r["apply_url"])

    sites = list(REGISTRY.keys())
    merged: list[dict] = []
    seen: set = set()
    per_source_agg: dict = {}
    errors_agg: dict = {}
    excluded = 0
    per_q = max(5, int(top_n) // max(1, len(queries)))
    for q in queries:
        sr = search_all(
            JobSearchQuery(
                query=q,
                location=locations_pref or None,
                is_remote=not bool(locations_pref),
                results_per_site=per_q,
                hours_old=int(hours_old),
            ),
            sites=sites,
        )
        for rec in sr.get("records") or []:
            src = rec.get("source")
            ext = rec.get("external_id")
            url = rec.get("apply_url") or rec.get("url")
            if (src, ext) in dismissed_keys or (url and url in dismissed_urls):
                excluded += 1
                continue
            key = (src, ext or url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(rec)
        for k, v in (sr.get("per_source") or {}).items():
            per_source_agg[k] = per_source_agg.get(k, 0) + int(v or 0)
        for k, v in (sr.get("errors") or {}).items():
            errors_agg.setdefault(k, str(v))

    pr = persist(merged)
    return {"ok": True, "data": {
        "queries": queries,
        "discovered": len(merged),
        "inserted": int(pr.get("inserted", 0)),
        "excluded_dismissed": excluded,
        "per_source": per_source_agg,
        "errors": errors_agg,
    }}


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
