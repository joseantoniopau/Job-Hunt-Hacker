"""JobSpy multi-site scraper adapter.

Wraps the `python-jobspy` package, which scrapes Indeed, LinkedIn, Glassdoor,
Google, ZipRecruiter, Bayt, Naukri, and BDJobs. We never raise on missing
dependencies; we just go unhealthy and return no records.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from .base import JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy

log = logging.getLogger("jhh.sources.jobspy")

_JOBSPY_OK = True
try:
    from jobspy import scrape_jobs  # type: ignore
except Exception as exc:  # noqa: BLE001
    log.warning("python-jobspy unavailable: %s", exc)
    _JOBSPY_OK = False
    scrape_jobs = None  # type: ignore

_SUPPORTED_SITES = {
    "indeed", "glassdoor", "google", "linkedin",
    "zip_recruiter", "bayt", "naukri", "bdjobs",
}


def _safe_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        if isinstance(v, float) and math.isnan(v):
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    return str(v)


def _remote_from_flag(flag: Any) -> str:
    if flag is True:
        return "remote"
    if flag is False:
        return ""
    if isinstance(flag, str):
        s = flag.strip().lower()
        if s in ("true", "1", "yes", "remote"):
            return "remote"
    return ""


class JobSpyAdapter(JobSourceAdapter):
    name = "jobspy"
    policy = SourcePolicy(
        name="jobspy",
        display_name="JobSpy (multi-site scrape)",
        official_api=False,
        scraping=True,
        apply_automation_allowed=False,
        recommended_mode="research",
        risk_level="GRAY",
        rate_limit_note="LinkedIn rate-limits ~10 pages without proxies",
        notes="Scrapes Indeed/LinkedIn/Glassdoor/Google/ZipRecruiter via python-jobspy.",
    )

    def healthy(self) -> bool:
        return _JOBSPY_OK

    def search(self, q: JobSearchQuery) -> list[JobRecord]:
        if not _JOBSPY_OK:
            return []

        sites = q.extra.get("sites") if isinstance(q.extra, dict) else None
        if not sites:
            sites = ["indeed", "google", "glassdoor"]
        sites = [s for s in sites if s in _SUPPORTED_SITES]
        if not sites:
            return []

        try:
            df = scrape_jobs(
                site_name=sites,
                search_term=q.query or "",
                location=q.location or "",
                distance=q.distance,
                is_remote=bool(q.is_remote),
                job_type=q.employment_type,
                results_wanted=int(q.results_per_site or 25),
                country_indeed=q.country or "usa",
                hours_old=q.hours_old,
                description_format="markdown",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("scrape_jobs failed: %s", exc)
            return []

        if df is None:
            return []
        try:
            rows = df.to_dict(orient="records")
        except Exception:
            return []

        out: list[JobRecord] = []
        for row in rows:
            try:
                site = _safe_str(row.get("site") or "jobspy")
                rec = JobRecord(
                    source=f"jobspy:{site}" if site else "jobspy",
                    title=_safe_str(row.get("title")),
                    company=_safe_str(row.get("company")),
                    location=_safe_str(row.get("location")),
                    remote_type=_remote_from_flag(row.get("is_remote")),
                    employment_type=_safe_str(row.get("job_type")),
                    salary_min=_safe_int(row.get("min_amount")),
                    salary_max=_safe_int(row.get("max_amount")),
                    currency=_safe_str(row.get("currency")),
                    bonus_equity_text="",
                    description=_safe_str(row.get("description")),
                    apply_url=_safe_str(row.get("job_url") or row.get("job_url_direct")),
                    company_url=_safe_str(row.get("company_url")),
                    posted_at=_safe_str(row.get("date_posted")),
                    external_id=_safe_str(row.get("id")),
                    raw={k: _safe_str(v) for k, v in row.items()},
                )
                if not rec.title and not rec.company:
                    continue
                out.append(rec)
            except Exception as exc:  # noqa: BLE001
                log.debug("row parse failed: %s", exc)
                continue
        return out


REGISTRY.register(JobSpyAdapter())
