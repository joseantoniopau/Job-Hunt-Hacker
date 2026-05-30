"""Ashby public board adapter."""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .base import JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy

log = logging.getLogger("jhh.sources.ashby")

_CURATED = ["ramp", "linear", "replicate", "posthog", "vanta"]
_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _HTML_TAG.sub(" ", s).replace("&nbsp;", " ").replace("&amp;", "&")


class AshbyAdapter(JobSourceAdapter):
    name = "ashby"
    policy = SourcePolicy(
        name="ashby",
        display_name="Ashby public boards",
        official_api=True,
        scraping=False,
        apply_automation_allowed=False,
        recommended_mode="assisted",
        risk_level="LEGAL",
    )

    def healthy(self) -> bool:
        return True

    def _boards(self, q: JobSearchQuery) -> list[str]:
        extra = q.extra if isinstance(q.extra, dict) else {}
        boards = extra.get("boards") or extra.get("ashby_boards")
        if boards and isinstance(boards, list):
            return [str(b).lower() for b in boards if b]
        return list(_CURATED)

    def search(self, q: JobSearchQuery) -> list[JobRecord]:
        boards = self._boards(q)
        needle = (q.query or "").strip().lower()
        out: list[JobRecord] = []
        with httpx.Client(timeout=20, headers={"User-Agent": "jhh/0.1"}) as client:
            for board in boards:
                url = (
                    f"https://api.ashbyhq.com/posting-api/job-board/{board}"
                    "?includeCompensation=true"
                )
                try:
                    r = client.get(url)
                    if r.status_code != 200:
                        log.debug("ashby %s -> %s", board, r.status_code)
                        continue
                    data = r.json() or {}
                except Exception as exc:  # noqa: BLE001
                    log.debug("ashby %s failed: %s", board, exc)
                    continue
                jobs: list[dict[str, Any]] = data.get("jobs", []) or []
                for job in jobs:
                    title = job.get("title") or ""
                    desc_html = job.get("descriptionHtml") or job.get("description") or ""
                    desc = _strip_html(desc_html)
                    if needle and needle not in f"{title}\n{desc}".lower():
                        continue
                    comp = job.get("compensation") or {}
                    bonus_equity_text = ""
                    if isinstance(comp, dict):
                        bonus_equity_text = comp.get("compensationTierSummary") or ""
                    rec = JobRecord(
                        source=f"ashby:{board}",
                        title=title,
                        company=board,
                        location=job.get("locationName") or "",
                        employment_type=job.get("employmentType") or "",
                        description=desc[:8000],
                        bonus_equity_text=bonus_equity_text or "",
                        apply_url=job.get("jobUrl") or job.get("applyUrl") or "",
                        external_id=str(job.get("id") or ""),
                        posted_at=job.get("publishedAt") or "",
                        remote_type="remote" if job.get("isRemote") else "",
                        raw={"board": board, "id": job.get("id")},
                    )
                    if rec.title:
                        out.append(rec)
        return out


REGISTRY.register(AshbyAdapter())
