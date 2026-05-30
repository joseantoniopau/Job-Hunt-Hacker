"""Lever public postings adapter."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from .base import JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy

log = logging.getLogger("jhh.sources.lever")

_CURATED = [
    "notion", "eaze", "netflix", "palantir", "retool",
    "anthropic", "openai", "plaid", "github",
]


def _iso(ms: Any) -> str:
    if not ms:
        return ""
    try:
        # Lever createdAt is epoch ms
        ts = int(ms) / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        return str(ms)


class LeverAdapter(JobSourceAdapter):
    name = "lever"
    policy = SourcePolicy(
        name="lever",
        display_name="Lever public boards",
        official_api=True,
        scraping=False,
        apply_automation_allowed=False,
        recommended_mode="assisted",
        risk_level="LEGAL",
        rate_limit_note="Public postings API; per-company throttling possible.",
    )

    def healthy(self) -> bool:
        return True

    def _companies(self, q: JobSearchQuery) -> list[str]:
        extra = q.extra if isinstance(q.extra, dict) else {}
        companies = extra.get("companies") or extra.get("lever_companies")
        if companies and isinstance(companies, list):
            return [str(c).lower() for c in companies if c]
        return list(_CURATED)

    def search(self, q: JobSearchQuery) -> list[JobRecord]:
        companies = self._companies(q)
        needle = (q.query or "").strip().lower()
        out: list[JobRecord] = []
        with httpx.Client(timeout=20, headers={"User-Agent": "jhh/0.1"}) as client:
            for company in companies:
                url = f"https://api.lever.co/v0/postings/{company}?mode=json"
                try:
                    r = client.get(url)
                    if r.status_code != 200:
                        log.debug("lever %s -> %s", company, r.status_code)
                        continue
                    postings: list[dict[str, Any]] = r.json() or []
                except Exception as exc:  # noqa: BLE001
                    log.debug("lever %s failed: %s", company, exc)
                    continue
                for job in postings:
                    title = job.get("text") or ""
                    desc = job.get("descriptionPlain") or job.get("description") or ""
                    if needle:
                        if needle not in f"{title}\n{desc}".lower():
                            continue
                    categories = job.get("categories") or {}
                    location = categories.get("location") or ""
                    employment_type = categories.get("commitment") or ""
                    rec = JobRecord(
                        source=f"lever:{company}",
                        title=title,
                        company=company,
                        location=location,
                        employment_type=employment_type,
                        description=desc[:8000],
                        apply_url=job.get("hostedUrl") or job.get("applyUrl") or "",
                        external_id=str(job.get("id") or ""),
                        posted_at=_iso(job.get("createdAt")),
                        raw={"company": company, "id": job.get("id")},
                    )
                    if rec.title:
                        out.append(rec)
        return out


REGISTRY.register(LeverAdapter())
