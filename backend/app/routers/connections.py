"""Networking / connections HTTP endpoints."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import networking

log = logging.getLogger("jhh.routers.connections")

router = APIRouter(prefix="/api", tags=["connections"])


class ConnectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    relationship: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    contact: Optional[str] = None
    notes: Optional[str] = None
    additional_companies: Optional[list[dict]] = None


class ConnectionUpdate(BaseModel):
    name: Optional[str] = None
    relationship: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    contact: Optional[str] = None
    notes: Optional[str] = None
    last_contacted_at: Optional[float] = None


@router.post("/connections")
def create(body: ConnectionCreate) -> dict:
    try:
        cid = networking.add_connection(
            name=body.name,
            relationship=body.relationship,
            company=body.company,
            role=body.role,
            contact=body.contact,
            notes=body.notes,
            additional_companies=body.additional_companies,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": networking.get_connection(cid)}


@router.get("/connections")
def listing(company: Optional[str] = None, limit: int = 500, offset: int = 0) -> dict:
    rows = networking.list_connections(company=company, limit=limit, offset=offset)
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/connections/refer/{company_name}")
def refer_at(company_name: str) -> dict:
    rows = networking.who_could_refer_at(company_name)
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/connections/suggest")
def suggest(max_jobs: int = 10) -> dict:
    rows = networking.suggest_outreach(max_jobs=max_jobs)
    return {"ok": True, "data": rows, "count": len(rows)}


# --------------------------------------------------------------------------
# Referral finder
# --------------------------------------------------------------------------

@router.get("/referrals")
def referrals(company: Optional[str] = None, job_id: Optional[int] = None,
              limit: int = 100) -> dict:
    """Rank connections for referral likelihood at a company.

    Request: GET /api/referrals?company=Stripe — or ?job_id=42 to pull the
    company (and job title, for message grounding) from a job posting; both
    may be combined, in which case `company` wins for matching and the job
    title still grounds the suggested message. `limit` caps results (default
    100).

    Response: {ok, company, job: {id,title,company}|null, count,
    data: [{connection, match_kind: current|past|fuzzy|mention,
    matched_company, last_contacted_at, suggested_message}]} ranked
    current > past > fuzzy > mention, then most recently contacted first.
    suggested_message is template-composed from stored facts only (profile
    name, connection name, company, job title) — no fabrication, no LLM.

    Errors: 400 when neither company nor a job with a company is given;
    404 when job_id doesn't exist.
    """
    try:
        result = networking.find_referrals(company=company, job_id=job_id, limit=limit)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {
        "ok": True,
        "company": result["company"],
        "job": result["job"],
        "data": result["results"],
        "count": result["count"],
    }


@router.get("/referrals/companies-with-connections")
def referral_companies() -> dict:
    """Companies from open jobs (job_posting status new/saved) where the user
    has >=1 connection, so the UI can badge jobs 'referral available'.

    Response: {ok, count, data: [{company, job_ids, job_count,
    connection_count, match_kinds: {kind: count}}]} sorted by
    connection_count desc, then company name.
    """
    rows = networking.companies_with_connections()
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/referrals/job-flags")
def referral_job_flags(job_ids: str = "") -> dict:
    """has_referral flag per job for list badging.

    Request: GET /api/referrals/job-flags?job_ids=1,2,3 (comma-separated ids).

    Response: {ok, data: {"<job_id>": bool, ...}} — True when the job's
    company matches >=1 connection (any match kind); unknown ids and jobs
    without a company are False. Errors: 400 on a non-integer id token.
    """
    ids: list[int] = []
    for token in (job_ids or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ids.append(int(token))
        except ValueError:
            raise HTTPException(400, f"invalid job id: {token!r}")
    flags = networking.referral_job_flags(ids)
    return {"ok": True, "data": {str(k): bool(v) for k, v in flags.items()}}


@router.get("/connections/{connection_id}")
def get_one(connection_id: int) -> dict:
    row = networking.get_connection(connection_id)
    if not row:
        raise HTTPException(404, f"connection {connection_id} not found")
    return {"ok": True, "data": row}


@router.patch("/connections/{connection_id}")
def patch_one(connection_id: int, body: ConnectionUpdate) -> dict:
    fields = body.model_dump(exclude_none=True)
    try:
        row = networking.update_connection(connection_id, fields)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "data": row}


@router.delete("/connections/{connection_id}")
def delete_one(connection_id: int) -> dict:
    ok = networking.delete_connection(connection_id)
    if not ok:
        raise HTTPException(404, f"connection {connection_id} not found")
    return {"ok": True, "detail": "deleted"}
