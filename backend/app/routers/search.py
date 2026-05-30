"""POST /api/search — multi-source job search + persistence.
GET /api/search/sources — discoverable adapter inventory.
"""
from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter

from ..models.schemas import JobSearchRequest
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
def post_search(body: JobSearchRequest) -> dict:
    q = _to_query(body)
    # which adapters to call
    requested_sites: list[str] = []
    # jobspy fans out across multiple scraper sites via q.extra["sites"]; if any
    # are jobspy-supported sites, include the jobspy adapter.
    jobspy_sites = {"indeed", "glassdoor", "google", "linkedin",
                    "zip_recruiter", "bayt", "naukri", "bdjobs"}
    if any(s in jobspy_sites for s in body.sites):
        requested_sites.append("jobspy")
    for s in body.sites:
        if s in REGISTRY and s not in requested_sites:
            requested_sites.append(s)
    if not requested_sites:
        # fall back to every healthy adapter
        requested_sites = list(REGISTRY.keys())

    search_res = search_all(q, requested_sites)
    persist_res = persist(search_res["records"])

    # Best-effort scoring kick-off
    scored = 0
    try:
        from ..matching import scorer  # type: ignore

        if hasattr(scorer, "score_job"):
            for jid in persist_res["ids"]:
                try:
                    scorer.score_job(jid)
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
