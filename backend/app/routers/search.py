"""POST /api/search — multi-source job search + persistence.
GET /api/search/sources — discoverable adapter inventory.
"""
from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Request

from ..models.schemas import JobSearchRequest
from ..security.rate_limit import rate_limit
from ..services.job_sources import REGISTRY
from ..services.job_sources.base import JobSearchQuery
from ..services.job_sources.pipeline import persist, search_all

log = logging.getLogger("jhh.routers.search")

router = APIRouter(prefix="/api", tags=["search"])


def _to_query(body: JobSearchRequest) -> JobSearchQuery:
    return JobSearchQuery(
        query=body.query,
        location=body.location,
        is_remote=body.is_remote,
        results_per_site=body.results_per_site,
        hours_old=body.hours_old,
        country=body.country,
        employment_type=body.employment_type,
        distance=body.distance,
        extra={"sites": body.sites},
    )


@router.post("/search")
@rate_limit("10/minute")
def post_search(request: Request, body: JobSearchRequest) -> dict:
    q = _to_query(body)
    # which adapters to call
    requested_sites: list[str] = []
    unknown_sites: list[str] = []
    # jobspy fans out across multiple scraper sites via q.extra["sites"]; if any
    # are jobspy-supported sites, include the jobspy adapter.
    jobspy_sites = {"indeed", "glassdoor", "google", "linkedin",
                    "zip_recruiter", "bayt", "naukri", "bdjobs"}
    for s in body.sites:
        if s in jobspy_sites:
            if "jobspy" not in requested_sites:
                requested_sites.append("jobspy")
        elif s in REGISTRY:
            if s not in requested_sites:
                requested_sites.append(s)
        else:
            unknown_sites.append(s)

    # Fall back to every healthy adapter ONLY when the caller supplied no
    # `sites` at all. If they asked specifically for unknown sites we surface
    # an error rather than silently running every adapter.
    if not requested_sites:
        if body.sites:
            return {
                "ok": False,
                "error": f"no valid sites in {body.sites!r}; available: {sorted(REGISTRY.keys())}",
                "data": {
                    "discovered": 0, "inserted": 0, "duplicates": 0,
                    "per_source": {}, "errors": {s: "unknown_site" for s in unknown_sites},
                    "ids": [], "scored": 0, "unknown_sites": unknown_sites,
                },
            }
        requested_sites = list(REGISTRY.keys())

    search_res = search_all(q, requested_sites)
    # Surface unknown_sites in the errors map so the UI shows what was skipped.
    for s in unknown_sites:
        search_res.setdefault("errors", {})[s] = "unknown_site"
    persist_res = persist(search_res["records"])

    # Best-effort scoring kick-off
    scored = 0
    try:
        from ..matching import scorer  # type: ignore

        if hasattr(scorer, "score_job"):
            for jid in persist_res["ids"]:
                try:
                    # Bulk path: skip per-job LLM polish so search responds
                    # in seconds, not minutes.
                    scorer.score_job(jid, llm_polish=False)
                    scored += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("score_job(%s) failed: %s", jid, exc)
    except Exception:
        pass  # scorer not present yet

    return {
        "ok": True,
        "data": {
            "discovered": len(search_res["records"]),
            "inserted": persist_res["inserted"],
            "duplicates": persist_res["duplicates"],
            "per_source": search_res["per_source"],
            "errors": search_res["errors"],
            "ids": persist_res["ids"],
            "scored": scored,
        },
    }


@router.get("/search/sources")
def list_sources() -> dict:
    out = []
    for name, adapter in sorted(REGISTRY.items()):
        try:
            policy = asdict(adapter.policy)
        except Exception:
            policy = {"name": name, "display_name": name, "risk_level": "GRAY"}
        out.append({
            "name": name,
            "healthy": bool(_safe_healthy(adapter)),
            "policy": policy,
        })
    return {"ok": True, "data": out}


def _safe_healthy(adapter) -> bool:  # type: ignore[no-untyped-def]
    try:
        return bool(adapter.healthy())
    except Exception:
        return False
