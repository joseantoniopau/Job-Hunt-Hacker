"""Shared search-plan builder.

Every code path that turns the user's profile into job-board queries
(autopilot, the Dashboard REFRESH button, scheduled saved searches created
from either) must apply the same rules:

  1. Queries are ROLES, never employers. Profile inference has leaked
     employer names into target_titles before ("eBay"); searching a board
     for an employer returns that company's whole catalog — office
     assistants, full-stack devs — drowning the user's actual field.
  2. The user's remote_preference wins over location inference.
  3. A remote search must NOT pass the home city as `location`: boards
     treat location as a hard metro filter, which starves results
     (Indeed: 0 hits for "threat hunter"+Miami vs 10 for remote).

Centralizing this here keeps the rules from drifting apart again.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..db import get_conn, row_to_dict

log = logging.getLogger("jhh.search_plan")


@dataclass
class SearchPlan:
    queries: list[str] = field(default_factory=list)
    location: str | None = None
    is_remote: bool = True
    dropped_employer_queries: list[str] = field(default_factory=list)


def _as_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                return [str(x).strip() for x in json.loads(s) if str(x).strip()]
            except Exception:
                pass
        return [t.strip() for t in s.split(",") if t.strip()]
    return []


def known_employers() -> set[str]:
    """Lower-cased employer names present anywhere in the vault."""
    try:
        rows = get_conn().execute(
            "SELECT DISTINCT lower(trim(employer)) AS e FROM career_claim "
            "WHERE employer IS NOT NULL AND trim(employer) != ''"
        ).fetchall()
        return {r["e"] for r in rows}
    except Exception:
        return set()


def build_search_plan(max_queries: int = 3) -> SearchPlan:
    """Derive board queries + location/remote settings from the profile."""
    plan = SearchPlan()
    prof: dict = {}
    try:
        row = get_conn().execute(
            "SELECT target_titles, target_keywords, preferred_locations, "
            "       location, remote_preference FROM user_profile WHERE id=1"
        ).fetchone()
        prof = row_to_dict(row) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("search plan: profile read failed: %s", exc)

    targets = _as_list(prof.get("target_titles"))
    keywords = _as_list(prof.get("target_keywords"))
    preferred = _as_list(prof.get("preferred_locations"))
    location_pref = preferred[0] if preferred else (prof.get("location") or "").strip()
    remote_pref = (prof.get("remote_preference") or "").strip().lower()

    employers = known_employers()
    plan.dropped_employer_queries = [
        t for t in targets if t.strip().lower() in employers]
    if plan.dropped_employer_queries:
        log.warning("search plan: dropped employer name(s) from queries: %s",
                    plan.dropped_employer_queries)
    titles = [t for t in targets if t.strip().lower() not in employers]

    plan.queries = titles[:max_queries]
    if not plan.queries and keywords:
        plan.queries = [" ".join(keywords[:3])]
    if not plan.queries:
        plan.queries = ["engineer"]

    plan.is_remote = remote_pref in ("remote", "remote_only", "remote-only") \
        or not bool(location_pref)
    plan.location = None if plan.is_remote else (location_pref or None)
    return plan
