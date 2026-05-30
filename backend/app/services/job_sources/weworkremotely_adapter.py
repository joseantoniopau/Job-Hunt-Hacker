"""We Work Remotely RSS adapter."""
from __future__ import annotations

import logging
import re

from .base import JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy

log = logging.getLogger("jhh.sources.wwr")

_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-jobs.rss",
]

_HTML_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return _HTML_TAG.sub(" ", s).replace("&nbsp;", " ").replace("&amp;", "&")


_FEEDPARSER_OK = True
try:
    import feedparser  # type: ignore
except Exception as exc:  # noqa: BLE001
    log.warning("feedparser unavailable: %s", exc)
    _FEEDPARSER_OK = False
    feedparser = None  # type: ignore


def _split_company_title(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    if ":" in text:
        company, _, title = text.partition(":")
        return company.strip(), title.strip()
    return "", text.strip()


class WeWorkRemotelyAdapter(JobSourceAdapter):
    name = "wwr"
    policy = SourcePolicy(
        name="wwr",
        display_name="We Work Remotely (RSS)",
        official_api=True,
        scraping=False,
        apply_automation_allowed=False,
        recommended_mode="assisted",
        risk_level="LEGAL",
    )

    def healthy(self) -> bool:
        return _FEEDPARSER_OK

    def search(self, q: JobSearchQuery) -> list[JobRecord]:
        if not _FEEDPARSER_OK:
            return []
        needle = (q.query or "").strip().lower()
        out: list[JobRecord] = []
        for url in _FEEDS:
            try:
                feed = feedparser.parse(url)
            except Exception as exc:  # noqa: BLE001
                log.debug("wwr feed %s failed: %s", url, exc)
                continue
            for entry in (feed.entries or []):
                try:
                    raw_title = entry.get("title") or ""
                    company, title = _split_company_title(raw_title)
                    desc = _strip_html(entry.get("summary") or entry.get("description") or "")
                    if needle and needle not in f"{raw_title}\n{desc}".lower():
                        continue
                    link = entry.get("link") or ""
                    rec = JobRecord(
                        source="wwr",
                        title=title or raw_title,
                        company=company,
                        location="Remote",
                        remote_type="remote",
                        description=desc[:8000],
                        apply_url=link,
                        posted_at=entry.get("published") or entry.get("updated") or "",
                        external_id=entry.get("id") or link,
                        raw={"feed": url, "guid": entry.get("id")},
                    )
                    if rec.title:
                        out.append(rec)
                except Exception as exc:  # noqa: BLE001
                    log.debug("wwr entry skipped: %s", exc)
                    continue
        return out


REGISTRY.register(WeWorkRemotelyAdapter())
