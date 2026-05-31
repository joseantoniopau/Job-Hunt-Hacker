"""Company research deep-dive — aggregate everything we know about a company
from the local DB + seed data. Mirrors what a recruiter would assemble
before sending a candidate into a screen.

Pure-local for now (no live web fetches) so it works offline and respects
the user's bandwidth. Could be extended later to fetch the careers page.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..config import settings
from ..db import audit, get_conn

log = logging.getLogger("jhh.services.company_research")


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _slugify(name: str) -> str:
    """Match the slugs used in remoteintech seed (lowercase, hyphens)."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower())
    return re.sub(r"^-+|-+$", "", s)


@lru_cache(maxsize=1)
def _remoteintech_index() -> dict:
    """Load the remote-in-tech seed (and our extensions) and index by slug/title."""
    p = Path(settings.root) / "data" / "seed" / "companies_remoteintech.json"
    if not p.exists():
        return {}
    try:
        rows = json.loads(p.read_text())
        idx: dict = {}
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            slug = (r.get("slug") or _slugify(r.get("title") or "")).lower()
            title = (r.get("title") or "").lower().strip()
            if slug:
                idx[slug] = r
            if title:
                idx[title] = r
        return idx
    except Exception as e:
        log.warning("remoteintech index load failed: %s", e)
        return {}


def _seed_match(company_name: str) -> Optional[dict]:
    if not company_name:
        return None
    idx = _remoteintech_index()
    key = (company_name or "").lower().strip()
    if key in idx:
        return idx[key]
    slug = _slugify(company_name)
    if slug in idx:
        return idx[slug]
    return None


def _tech_mentions_from_jobs(rows: list[dict], max_terms: int = 20) -> list[str]:
    """Pull common tech keywords mentioned across this company's job descriptions.
    Uses the existing ats_keywords categories so we don't reinvent the wheel.
    """
    try:
        from ..matching import keyword_classifier  # type: ignore
    except Exception:
        keyword_classifier = None  # type: ignore

    blob_parts: list[str] = []
    for r in rows:
        desc = r.get("description") or ""
        req = r.get("requirements") or ""
        if isinstance(req, list):
            req = "\n".join(str(x) for x in req)
        blob_parts.append(str(desc) + "\n" + str(req))
    blob = "\n\n".join(blob_parts).lower()
    if not blob:
        return []

    # Try the in-tree skills_extractor first (returns canonical keyword names)
    try:
        from ..matching.skills_extractor import extract_skills
        skills = extract_skills(blob)
        counts: dict[str, int] = {}
        for s in skills:
            counts[s] = counts.get(s, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        return [k for k, _ in ranked[:max_terms]]
    except Exception:
        pass

    # Fallback: cheap keyword scan
    keywords = [
        "python", "javascript", "typescript", "react", "node", "aws", "gcp",
        "azure", "docker", "kubernetes", "terraform", "sql", "postgres",
        "fastapi", "django", "rails", "java", "go", "rust",
    ]
    found = []
    for k in keywords:
        if k in blob:
            found.append(k.title() if len(k) > 3 else k.upper())
        if len(found) >= max_terms:
            break
    return found


def enrich(company_name: str) -> dict:
    """Aggregate everything we know about a company."""
    name = (company_name or "").strip()
    if not name:
        raise ValueError("company_name is required")

    conn = get_conn()
    norm_key = _normalize(name)

    # All jobs we've seen from this company (case-insensitive exact match).
    job_rows = conn.execute(
        """SELECT * FROM job_posting
           WHERE LOWER(TRIM(company)) = ?
           ORDER BY discovered_at DESC""",
        (norm_key,),
    ).fetchall()
    jobs = [dict(r) for r in job_rows]

    # Compute salary range across these jobs.
    sals: list[int] = []
    last_seen = None
    titles: dict[str, int] = {}
    for j in jobs:
        smin = j.get("salary_min")
        smax = j.get("salary_max")
        if smin and smax:
            sals.extend([int(smin), int(smax)])
        elif smin or smax:
            sals.append(int(smin or smax or 0))
        if j.get("discovered_at") and (last_seen is None or j["discovered_at"] > last_seen):
            last_seen = j["discovered_at"]
        t = j.get("title") or ""
        if t:
            titles[t] = titles.get(t, 0) + 1

    salary_range = None
    if sals:
        salary_range = {"min": min(sals), "max": max(sals), "count": len(sals)}

    # Our applications + outcomes to this company.
    app_rows = conn.execute(
        """SELECT a.id AS app_id, a.status, a.applied_at, a.notes, a.last_contact_at,
                  a.next_followup_at, j.title, j.id AS job_id
           FROM application a
           JOIN job_posting j ON j.id = a.job_id
           WHERE LOWER(TRIM(j.company)) = ?
           ORDER BY a.applied_at DESC""",
        (norm_key,),
    ).fetchall()
    our_apps = [dict(r) for r in app_rows]

    # Effectiveness outcomes recorded against those applications.
    outcomes: dict[str, int] = {}
    if our_apps:
        app_ids = [a["app_id"] for a in our_apps]
        placeholders = ",".join("?" for _ in app_ids)
        ev_rows = conn.execute(
            f"SELECT outcome, COUNT(*) AS c FROM effectiveness_event "
            f"WHERE application_id IN ({placeholders}) GROUP BY outcome",
            tuple(app_ids),
        ).fetchall()
        for r in ev_rows:
            outcomes[r["outcome"]] = int(r["c"])

    tech_mentions = _tech_mentions_from_jobs(jobs)

    # Seed lookup for careers URL / remote policy / industry.
    seed = _seed_match(name) or {}
    careers_url = seed.get("careers_url")
    remote_policy = seed.get("remote_policy")
    industry = seed.get("industry")
    website = seed.get("website")

    # Most common roles posted (good for steering tailoring).
    top_roles = sorted(titles.items(), key=lambda kv: kv[1], reverse=True)[:10]

    out = {
        "company": name,
        "jobs_seen": len(jobs),
        "top_roles": [{"title": t, "count": c} for t, c in top_roles],
        "salary_range": salary_range,
        "our_applications": our_apps,
        "outcomes": outcomes,
        "tech_mentions": tech_mentions,
        "careers_url": careers_url,
        "website": website,
        "remote_policy": remote_policy,
        "industry": industry,
        "last_seen": last_seen,
        "recent_jobs": jobs[:10],
    }

    try:
        audit("company_research_enriched", "job_posting", None,
              company=name, jobs_seen=len(jobs))
    except Exception:
        pass

    return out


def list_companies(limit: int = 200, offset: int = 0) -> list[dict]:
    """All companies we have jobs from, ranked by job count."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT TRIM(company) AS company, COUNT(*) AS job_count,
                  MAX(discovered_at) AS last_seen
           FROM job_posting
           WHERE company IS NOT NULL AND TRIM(company) <> ''
           GROUP BY TRIM(company)
           ORDER BY job_count DESC, company ASC
           LIMIT ? OFFSET ?""",
        (int(limit), int(offset)),
    ).fetchall()
    return [dict(r) for r in rows]


__all__ = ["enrich", "list_companies"]
