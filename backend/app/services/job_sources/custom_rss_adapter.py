"""User-configurable RSS feed adapter.

Reads `data/custom_rss_feeds.json`, a JSON list of `{url, name}` entries.
Useful for hooking up niche job boards a user trusts.
"""
from __future__ import annotations

import json
import logging
import re

from ...config import settings
from .base import JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy

log = logging.getLogger("jhh.sources.custom_rss")

FEEDS_PATH = settings.data_dir / "custom_rss_feeds.json"

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


def _load_feeds() -> list[dict]:
    if not FEEDS_PATH.exists():
        return []
    try:
        data = json.loads(FEEDS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and d.get("url")]
    except Exception as exc:  # noqa: BLE001
        log.warning("custom_rss load failed: %s", exc)
    return []


class CustomRSSAdapter(JobSourceAdapter):
    name = "custom_rss"
    policy = SourcePolicy(
        name="custom_rss",
        display_name="Custom RSS feeds",
        official_api=True,
        scraping=False,
        apply_automation_allowed=False,
        recommended_mode="assisted",
        risk_level="LEGAL",
        notes="Reads data/custom_rss_feeds.json (list of {url,name}).",
    )

    def healthy(self) -> bool:
        return _FEEDPARSER_OK and bool(_load_feeds())

    def search(self, q: JobSearchQuery) -> list[JobRecord]:
        if not _FEEDPARSER_OK:
            return []
        feeds = _load_feeds()
        if not feeds:
            return []
        needle = (q.query or "").strip().lower()
        out: list[JobRecord] = []
        for f in feeds:
            url = f.get("url") or ""
            label = f.get("name") or url
            try:
                parsed = feedparser.parse(url)
            except Exception as exc:  # noqa: BLE001
                log.debug("custom_rss %s failed: %s", url, exc)
                continue
            for entry in (parsed.entries or []):
                try:
                    title = entry.get("title") or ""
                    desc = _strip_html(entry.get("summary") or entry.get("description") or "")
                    if needle and needle not in f"{title}\n{desc}".lower():
                        continue
                    link = entry.get("link") or ""
                    rec = JobRecord(
                        source=f"custom_rss:{label}",
                        title=title,
                        company=label,
                        description=desc[:8000],
                        apply_url=link,
                        posted_at=entry.get("published") or entry.get("updated") or "",
                        external_id=entry.get("id") or link,
                        raw={"feed_url": url, "feed_name": label},
                    )
                    if rec.title:
                        out.append(rec)
                except Exception as exc:  # noqa: BLE001
                    log.debug("custom_rss entry skipped: %s", exc)
                    continue
        return out


REGISTRY.register(CustomRSSAdapter())
