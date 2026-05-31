"""Salary intelligence — compute market percentiles from the local job_posting
corpus. No external paid data sources; we use what we've already scraped.

A great human headhunter has a rolodex of recent comp data; this service
gives the app the same instinct by aggregating from jobs we've already
indexed in the last N days.
"""
from __future__ import annotations

import logging
import re
import time
from statistics import quantiles
from typing import Optional

from ..db import audit, get_conn

log = logging.getLogger("jhh.services.salary_intelligence")


# Window for "current market" — 90 days of postings. Tunable but anything
# older risks comparing today's offer to last year's compensation.
DEFAULT_WINDOW_DAYS = 90

# When a posting only has a single salary number we treat it as midpoint;
# when both min+max are present we use the midpoint of the range. Headhunters
# typically quote the midpoint when sharing a "band".
def _midpoint(smin: Optional[int], smax: Optional[int]) -> Optional[int]:
    if smin is not None and smax is not None and smax > 0:
        return int((smin + smax) / 2)
    if smax:
        return int(smax)
    if smin:
        return int(smin)
    return None


def _normalize_role(role: str) -> str:
    return re.sub(r"\s+", " ", (role or "").strip().lower())


def _matches_role(job_title: str, role_query: str) -> bool:
    """Fuzzy: split the query into tokens, require at least one token (>=3 chars)
    to appear in the title. Avoids the noise of pure substring search while
    keeping the implementation dependency-free.
    """
    if not job_title or not role_query:
        return False
    title_l = job_title.lower()
    q_tokens = [t for t in re.findall(r"[a-z0-9+#]+", role_query.lower()) if len(t) >= 3]
    if not q_tokens:
        return role_query.lower() in title_l
    # Require at least 50% of the query tokens to appear, capped at 2 minimum
    # so "Senior Backend Engineer" needs roughly 2 hits in the title.
    needed = max(1, min(2, len(q_tokens) // 2 + 1))
    hits = sum(1 for t in q_tokens if t in title_l)
    return hits >= needed


def _matches_location(job_loc: Optional[str], location_query: Optional[str]) -> bool:
    if not location_query:
        return True
    if not job_loc:
        return False
    jl = job_loc.lower()
    lq = location_query.lower().strip()
    if lq in ("remote", "anywhere"):
        return "remote" in jl or "anywhere" in jl
    return lq in jl


def _percentiles(values: list[int]) -> dict:
    """Compute p25/median/p75/p90 over `values`. Returns a clean shape with
    nulls when there isn't enough data (need >=2 points for any percentile)."""
    n = len(values)
    if n == 0:
        return {"p25": None, "median": None, "p75": None, "p90": None}
    if n == 1:
        v = values[0]
        return {"p25": v, "median": v, "p75": v, "p90": v}
    sorted_v = sorted(values)
    # statistics.quantiles needs n>=2; we use method="inclusive" so endpoints
    # are included (gives stable values for small samples).
    try:
        qs_4 = quantiles(sorted_v, n=4, method="inclusive")
        qs_10 = quantiles(sorted_v, n=10, method="inclusive")
        return {
            "p25": int(round(qs_4[0])),
            "median": int(round(qs_4[1])),
            "p75": int(round(qs_4[2])),
            "p90": int(round(qs_10[8])),
        }
    except Exception:
        # Fallback to index-based percentile estimation.
        def at(p: float) -> int:
            idx = max(0, min(n - 1, int(round(p * (n - 1)))))
            return sorted_v[idx]
        return {
            "p25": at(0.25),
            "median": at(0.50),
            "p75": at(0.75),
            "p90": at(0.90),
        }


def compute_market(
    role: str,
    location: Optional[str] = None,
    currency: str = "USD",
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict:
    """Compute percentiles for jobs matching `role` (+ optional location) in
    the last `window_days`. Excludes jobs with no salary.
    """
    role = (role or "").strip()
    if not role:
        return {
            "role": role,
            "location": location,
            "currency": currency,
            "count": 0,
            "p25": None,
            "median": None,
            "p75": None,
            "p90": None,
            "sample_jobs": [],
            "window_days": window_days,
            "note": "empty role",
        }
    currency = (currency or "USD").upper()
    cutoff = time.time() - (window_days * 86400)

    conn = get_conn()
    # discovered_at can be NULL for legacy rows — accept those too so we
    # don't return 0 results during early use; in steady state cutoff filters.
    rows = conn.execute(
        """SELECT id, title, company, location, salary_min, salary_max, currency,
                  discovered_at, apply_url
           FROM job_posting
           WHERE (salary_min IS NOT NULL OR salary_max IS NOT NULL)
             AND (discovered_at IS NULL OR discovered_at >= ?)""",
        (cutoff,),
    ).fetchall()

    values: list[int] = []
    samples: list[dict] = []
    for r in rows:
        d = dict(r)
        cur = (d.get("currency") or "USD").upper()
        if currency and cur != currency:
            continue
        if not _matches_role(d.get("title") or "", role):
            continue
        if not _matches_location(d.get("location"), location):
            continue
        mid = _midpoint(d.get("salary_min"), d.get("salary_max"))
        if mid is None or mid <= 0:
            continue
        values.append(mid)
        if len(samples) < 20:
            samples.append({
                "id": d.get("id"),
                "title": d.get("title"),
                "company": d.get("company"),
                "location": d.get("location"),
                "salary_min": d.get("salary_min"),
                "salary_max": d.get("salary_max"),
                "currency": cur,
                "apply_url": d.get("apply_url"),
            })

    pcts = _percentiles(values)
    out = {
        "role": role,
        "location": location,
        "currency": currency,
        "count": len(values),
        **pcts,
        "sample_jobs": samples,
        "window_days": window_days,
    }
    if len(values) < 5:
        out["note"] = (
            "Sample size is small; treat the percentiles as directional only."
        )
    try:
        audit(
            "salary_market_computed",
            "salary_intelligence",
            None,
            role=role,
            location=location,
            count=len(values),
        )
    except Exception:
        pass
    return out


def _user_target_titles() -> list[str]:
    conn = get_conn()
    row = conn.execute(
        "SELECT target_titles, currency FROM user_profile WHERE id = 1"
    ).fetchone()
    if not row:
        return []
    raw = row["target_titles"] if hasattr(row, "keys") else row[0]
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    # JSON-encoded list or comma-separated
    import json as _json
    try:
        v = _json.loads(raw)
        if isinstance(v, list):
            return [str(t) for t in v if t]
    except Exception:
        pass
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def _user_currency() -> str:
    conn = get_conn()
    row = conn.execute("SELECT currency FROM user_profile WHERE id = 1").fetchone()
    if not row:
        return "USD"
    cur = row["currency"] if hasattr(row, "keys") else row[0]
    return (cur or "USD").upper()


def comp_summary_for_profile() -> dict:
    """Global comp readout for the user's target titles."""
    titles = _user_target_titles()
    currency = _user_currency()
    summaries: list[dict] = []
    for t in titles[:10]:
        summaries.append(compute_market(t, currency=currency))
    # Aggregate median across all summaries (weighted by count).
    weighted_total = 0.0
    weight = 0
    for s in summaries:
        m = s.get("median")
        c = s.get("count") or 0
        if m and c:
            weighted_total += m * c
            weight += c
    overall_median = int(round(weighted_total / weight)) if weight else None
    return {
        "currency": currency,
        "target_titles": titles,
        "per_role": summaries,
        "overall_median": overall_median,
        "total_postings_considered": weight,
    }


__all__ = ["compute_market", "comp_summary_for_profile"]
