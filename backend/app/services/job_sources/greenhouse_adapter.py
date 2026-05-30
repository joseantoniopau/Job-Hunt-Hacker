"""Greenhouse public Job Board API adapter."""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from .base import JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy

log = logging.getLogger("jhh.sources.greenhouse")

_CURATED = [
    "airbnb", "stripe", "gitlab", "robinhood", "brex", "ramp", "mercury",
    "pinterest", "twitch", "reddit", "instacart", "dropbox", "doordash",
    "plaid", "scale",
]

_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _HTML_TAG.sub(" ", s).replace("&nbsp;", " ").replace("&amp;", "&")


class GreenhouseAdapter(JobSourceAdapter):
    name = "greenhouse"
    policy = SourcePolicy(
        name="greenhouse",
        display_name="Greenhouse public boards",
        official_api=True,
        scraping=False,
        apply_automation_allowed=False,
        recommended_mode="assisted",
        risk_level="LEGAL",
        rate_limit_note="Public board API; please be polite.",
    )

    def healthy(self) -> bool:
        return True

    def _boards(self, q: JobSearchQuery) -> list[str]:
        extra = q.extra if isinstance(q.extra, dict) else {}
        boards = extra.get("boards") or extra.get("greenhouse_boards")
        if boards and isinstance(boards, list):
            return [str(b).lower() for b in boards if b]
        return list(_CURATED)

    def search(self, q: JobSearchQuery) -> list[JobRecord]:
        boards = self._boards(q)
        needle = (q.query or "").strip().lower()
        out: list[JobRecord] = []
        with httpx.Client(timeout=20, headers={"User-Agent": "jhh/0.1"}) as client:
            for board in boards:
                url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
                try:
                    r = client.get(url)
                    if r.status_code != 200:
                        log.debug("greenhouse %s -> %s", board, r.status_code)
                        continue
                    data = r.json()
                except Exception as exc:  # noqa: BLE001
                    log.debug("greenhouse %s failed: %s", board, exc)
                    continue
                jobs: list[dict[str, Any]] = data.get("jobs", []) or []
                for job in jobs:
                    title = job.get("title") or ""
                    content_html = job.get("content") or ""
                    content_txt = _strip_html(content_html)
                    if needle:
                        hay = f"{title}\n{content_txt}".lower()
                        if needle not in hay:
                            continue
                    loc = ""
                    if isinstance(job.get("location"), dict):
                        loc = job["location"].get("name") or ""
                    posted = job.get("updated_at") or job.get("created_at") or ""
                    rec = JobRecord(
                        source=f"greenhouse:{board}",
                        title=title,
                        company=board,
                        location=loc,
                        description=content_txt[:8000],
                        apply_url=job.get("absolute_url") or "",
                        external_id=str(job.get("id") or ""),
                        posted_at=posted,
                        raw={"board": board, "id": job.get("id")},
                    )
                    if rec.title:
                        out.append(rec)
                    if len(out) >= int(q.results_per_site or 25) * len(boards):
                        break
        return out


REGISTRY.register(GreenhouseAdapter())
