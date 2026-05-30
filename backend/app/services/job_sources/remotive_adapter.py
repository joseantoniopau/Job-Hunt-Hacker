"""Remotive remote-jobs API adapter."""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy

log = logging.getLogger("jhh.sources.remotive")

_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _HTML_TAG.sub(" ", s).replace("&nbsp;", " ").replace("&amp;", "&")


class RemotiveAdapter(JobSourceAdapter):
    name = "remotive"
    policy = SourcePolicy(
        name="remotive",
        display_name="Remotive remote jobs",
        official_api=True,
        scraping=False,
        apply_automation_allowed=False,
        recommended_mode="assisted",
        risk_level="LEGAL",
    )

    def healthy(self) -> bool:
        return True

    def search(self, q: JobSearchQuery) -> list[JobRecord]:
        params: dict[str, Any] = {}
        if q.query:
            params["search"] = q.query
        if q.results_per_site:
            params["limit"] = int(q.results_per_site)
        extra = q.extra if isinstance(q.extra, dict) else {}
        if extra.get("category"):
            params["category"] = extra["category"]
        url = "https://remotive.com/api/remote-jobs"
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            with httpx.Client(timeout=20, headers={"User-Agent": "jhh/0.1"}) as client:
                r = client.get(url)
                if r.status_code != 200:
                    log.debug("remotive -> %s", r.status_code)
                    return []
                data = r.json() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("remotive failed: %s", exc)
            return []
        jobs: list[dict[str, Any]] = data.get("jobs", []) or []
        out: list[JobRecord] = []
        for job in jobs:
            try:
                desc = _strip_html(job.get("description") or "")
                rec = JobRecord(
                    source="remotive",
                    title=job.get("title") or "",
                    company=job.get("company_name") or "",
                    location=job.get("candidate_required_location") or "Remote",
                    remote_type="remote",
                    employment_type=job.get("job_type") or "",
                    bonus_equity_text=job.get("salary") or "",
                    description=desc[:8000],
                    apply_url=job.get("url") or "",
                    company_url=job.get("company_logo_url") or "",
                    posted_at=job.get("publication_date") or "",
                    external_id=str(job.get("id") or ""),
                    raw={"id": job.get("id"), "category": job.get("category")},
                )
                if rec.title:
                    out.append(rec)
            except Exception as exc:  # noqa: BLE001
                log.debug("remotive job skipped: %s", exc)
                continue
        return out


REGISTRY.register(RemotiveAdapter())
