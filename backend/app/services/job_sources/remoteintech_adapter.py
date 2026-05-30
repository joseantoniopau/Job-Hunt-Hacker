"""Remote In Tech company seed list.

Discovery-only adapter. Reads `data/seed/companies_remoteintech.json` and
returns one JobRecord per matched company pointing at its careers page.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ...config import settings
from ...utils.text import keyword_tokens, normalize
from .base import JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy

log = logging.getLogger("jhh.sources.remoteintech")

SEED_PATH = settings.data_dir / "seed" / "companies_remoteintech.json"


def _load_companies() -> list[dict]:
    if not SEED_PATH.exists():
        return []
    try:
        raw = SEED_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except Exception as exc:  # noqa: BLE001
        log.warning("seed load failed: %s", exc)
    return []


def _region_matches(company: dict, location_q: str) -> bool:
    if not location_q:
        return True
    needle = normalize(location_q)
    region = normalize(company.get("region") or "")
    if needle in region or region in needle:
        return True
    # crude aliases
    aliases = {
        "remote": ["worldwide", "anywhere", "remote"],
        "us": ["united states", "usa", "us only", "americas"],
        "europe": ["europe", "eu", "uk", "emea"],
        "uk": ["united kingdom", "uk", "europe"],
    }
    for k, vs in aliases.items():
        if k in needle and any(v in region for v in vs):
            return True
    return False


def _tech_matches(company: dict, query: str) -> bool:
    if not query:
        return True
    tokens = set(keyword_tokens(query))
    if not tokens:
        return True
    haystack_parts = [
        company.get("title") or "",
        " ".join(company.get("technologies") or []),
        company.get("remote_policy") or "",
    ]
    hay_tokens = set()
    for part in haystack_parts:
        hay_tokens.update(keyword_tokens(part))
    return bool(tokens & hay_tokens)


class RemoteInTechAdapter(JobSourceAdapter):
    name = "remoteintech"
    policy = SourcePolicy(
        name="remoteintech",
        display_name="Remote In Tech company seed list",
        official_api=False,
        scraping=False,
        apply_automation_allowed=False,
        recommended_mode="research",
        risk_level="LEGAL",
        notes="Curated company list; discovery only, no live job postings.",
    )

    def healthy(self) -> bool:
        return SEED_PATH.exists()

    def search(self, q: JobSearchQuery) -> list[JobRecord]:
        companies = _load_companies()
        if not companies:
            return []
        out: list[JobRecord] = []
        for c in companies:
            try:
                if not _region_matches(c, q.location or ""):
                    continue
                if q.query and not _tech_matches(c, q.query):
                    continue
                careers = c.get("careers_url") or c.get("website") or ""
                rec = JobRecord(
                    source="remoteintech",
                    title="(visit careers page)",
                    company=c.get("title") or c.get("slug") or "",
                    location=c.get("region") or "Remote",
                    remote_type="remote",
                    apply_url=careers,
                    company_url=c.get("website") or "",
                    description=c.get("remote_policy") or "",
                    external_id=c.get("slug") or (c.get("title") or "").lower().replace(" ", "-"),
                    raw=c,
                )
                if not rec.company:
                    continue
                out.append(rec)
            except Exception as exc:  # noqa: BLE001
                log.debug("company skipped: %s", exc)
                continue
        return out


REGISTRY.register(RemoteInTechAdapter())
