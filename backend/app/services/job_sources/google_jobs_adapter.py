"""Google Jobs via SerpApi or SearchAPI."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ...config import settings
from .base import JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy

log = logging.getLogger("jhh.sources.google_jobs")


class GoogleJobsAdapter(JobSourceAdapter):
    name = "google_jobs"
    policy = SourcePolicy(
        name="google_jobs",
        display_name="Google Jobs (SerpApi / SearchAPI)",
        official_api=True,
        scraping=False,
        apply_automation_allowed=False,
        recommended_mode="assisted",
        risk_level="LEGAL",
        notes="Requires SERPAPI_API_KEY or SEARCHAPI_API_KEY.",
    )

    def _provider(self) -> str:
        if settings.serpapi_key:
            return "serpapi"
        if settings.searchapi_key:
            return "searchapi"
        return ""

    def healthy(self) -> bool:
        return bool(self._provider())

    def search(self, q: JobSearchQuery) -> list[JobRecord]:
        provider = self._provider()
        if not provider:
            return []
        params: dict[str, Any] = {
            "engine": "google_jobs",
            "q": q.query or "",
        }
        if q.location:
            params["location"] = q.location
        if provider == "serpapi":
            params["api_key"] = settings.serpapi_key
            url = "https://serpapi.com/search.json"
        else:
            params["api_key"] = settings.searchapi_key
            url = "https://www.searchapi.io/api/v1/search"
        try:
            with httpx.Client(timeout=20, headers={"User-Agent": "jhh/0.1"}) as client:
                r = client.get(url, params=params)
                if r.status_code != 200:
                    log.debug("google_jobs %s -> %s", provider, r.status_code)
                    return []
                data = r.json() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("google_jobs failed: %s", exc)
            return []
        jobs: list[dict[str, Any]] = data.get("jobs_results") or data.get("jobs") or []
        out: list[JobRecord] = []
        for job in jobs:
            try:
                ext = job.get("detected_extensions") or {}
                links = job.get("related_links") or job.get("apply_options") or []
                apply_url = ""
                if links and isinstance(links, list):
                    first = links[0]
                    if isinstance(first, dict):
                        apply_url = first.get("link") or first.get("url") or ""
                rec = JobRecord(
                    source=f"google_jobs:{provider}",
                    title=job.get("title") or "",
                    company=job.get("company_name") or "",
                    location=job.get("location") or "",
                    description=(job.get("description") or "")[:8000],
                    apply_url=apply_url,
                    posted_at=ext.get("posted_at") or "",
                    employment_type=ext.get("schedule_type") or "",
                    external_id=str(job.get("job_id") or job.get("id") or ""),
                    raw={"provider": provider},
                )
                if rec.title:
                    out.append(rec)
            except Exception as exc:  # noqa: BLE001
                log.debug("google_jobs row skipped: %s", exc)
                continue
        return out


REGISTRY.register(GoogleJobsAdapter())
