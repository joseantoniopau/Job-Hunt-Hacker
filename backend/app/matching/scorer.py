"""Main scoring entrypoint.

`score_job(job_id)` loads a job + the user's Career Vault claims, runs the
sub-scorers, computes a weighted overall score, persists to `job_match`
and returns the dict.
"""
from __future__ import annotations

import json
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from ..db import audit, get_conn, row_to_dict, tx
from . import ats_analyzer, location_parser, salary_parser, seniority_parser

# --- defensive optional imports ---
try:
    from ..services import career_vault  # type: ignore
except Exception:  # pragma: no cover - optional
    career_vault = None  # type: ignore

try:
    from ..llm import get_llm  # type: ignore
except Exception:  # pragma: no cover - optional
    get_llm = None  # type: ignore


# ---- weights ----

_DEFAULT_WEIGHTS = {
    "skills": 0.25,
    "experience": 0.15,
    "salary": 0.10,
    "location": 0.10,
    "seniority": 0.10,
    "keywords": 0.20,
    "evidence": 0.10,
}


@lru_cache(maxsize=1)
def default_weights() -> dict:
    path = Path(settings.root) / "data" / "seed" / "scoring_weights_default.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, dict) and data:
                return {k: float(v) for k, v in data.items()}
        except Exception:
            pass
    return dict(_DEFAULT_WEIGHTS)


def _renormalize(weights: dict[str, float], skip: set[str]) -> dict[str, float]:
    """Drop skipped dimensions and rescale remaining to sum to 1.0."""
    kept = {k: float(v) for k, v in weights.items() if k not in skip and v > 0}
    total = sum(kept.values())
    if total <= 0:
        return kept
    return {k: v / total for k, v in kept.items()}


# ---- claim loading ----

def _load_claims_fallback() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM career_claim WHERE allowed_for_resume = 1"
    ).fetchall()
    return [row_to_dict(r) for r in rows if r]


def _load_claims_for_job(job_text: str) -> list[dict]:
    if career_vault is not None:
        # prefer the retrieve API if available
        try:
            fn = getattr(career_vault, "retrieve_for_job", None)
            if callable(fn):
                hits = fn(job_text, top=20) or []
                if hits:
                    return hits
        except Exception:
            pass
        try:
            fn = getattr(career_vault, "list_claims", None)
            if callable(fn):
                return fn(allowed_only=True) or []
        except Exception:
            pass
    return _load_claims_fallback()


# ---- user profile ----

def _load_user_profile() -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    return row_to_dict(row) or {}


# ---- sub-scorers (all 0..1) ----

def _skills_score(ats_result: dict) -> float:
    """Ratio of required keywords that have supported/transferable evidence."""
    required = [k for k in ats_result.get("keywords", []) if k.get("importance") == "required"]
    if not required:
        # use overall safe ratio
        safe = [k for k in ats_result.get("keywords", []) if k.get("resume_safe")]
        total = ats_result.get("keywords") or []
        return (len(safe) / len(total)) if total else 0.5
    safe = [k for k in required if k.get("resume_safe")]
    return len(safe) / len(required)


def _years_of_experience_from_claims(claims: list[dict]) -> float:
    """Crude: take the earliest date_start and most recent date_end across
    employment-type claims. Anything unparseable contributes 0.
    """
    earliest = None
    latest = None
    year_rx = re.compile(r"(\d{4})")
    for c in claims:
        for f in ("date_start", "date_end"):
            v = c.get(f)
            if not v:
                continue
            m = year_rx.search(str(v))
            if not m:
                continue
            y = int(m.group(1))
            if earliest is None or y < earliest:
                earliest = y
            if latest is None or y > latest:
                latest = y
    if earliest is None or latest is None:
        return 0.0
    yrs = max(0.0, latest - earliest)
    return yrs


_LEVEL_MIN_YEARS = {
    "intern": 0, "entry": 0, "mid": 2, "senior": 5,
    "staff": 8, "principal": 10,
    "manager": 5, "director": 8, "vp": 12, "exec": 15,
}


def _experience_score(claims: list[dict], job_level: str) -> float:
    yrs = _years_of_experience_from_claims(claims)
    target = _LEVEL_MIN_YEARS.get(job_level or "mid", 3)
    if target <= 0:
        return 1.0
    if yrs <= 0:
        return 0.4 if not claims else 0.55
    ratio = yrs / target
    if ratio >= 1.0:
        return 1.0
    if ratio >= 0.6:
        return 0.7
    if ratio >= 0.3:
        return 0.4
    return 0.2


def _salary_score(job_record: dict, user: dict) -> float:
    job_max = job_record.get("salary_max")
    job_min = job_record.get("salary_min")
    user_pref = user.get("preferred_salary")
    user_min = user.get("minimum_salary")
    if not job_max and not job_min:
        return 1.0  # unknown — don't penalize
    # use the upper bound where possible
    effective = job_max or job_min
    if not user_pref and not user_min:
        return 0.8  # we know the job pays; user gave no targets
    if user_pref and effective and effective >= user_pref:
        return 1.0
    if user_min and effective and effective >= user_min:
        return 0.7
    return 0.3


def _location_score(job_record: dict, user: dict) -> float:
    job_loc_text = job_record.get("location") or ""
    job_loc = location_parser.parse_location(job_loc_text)
    # honor remote_type field if set explicitly
    rt = (job_record.get("remote_type") or "").lower()
    if rt in ("remote", "fully_remote", "fully remote"):
        job_loc["remote"] = True
    elif rt == "hybrid":
        job_loc["hybrid"] = True
    prefs = {
        "remote_preference": user.get("remote_preference"),
        "preferred_locations": user.get("preferred_locations") or [],
        "location": user.get("location") or "",
    }
    return location_parser.match_location(job_loc, prefs)


def _seniority_score(job_record: dict, user: dict) -> float:
    level = seniority_parser.detect_seniority(
        job_record.get("title") or "",
        job_record.get("description") or "",
    )
    return seniority_parser.match_seniority(level, user.get("seniority_targets") or [])


def _keyword_score(ats_result: dict) -> float:
    cov = ats_result.get("coverage") or {}
    total = sum(cov.values())
    if not total:
        return 0.5
    s = cov.get("supported", 0) + 0.7 * cov.get("transferable", 0) + 0.3 * cov.get("weak", 0)
    return min(1.0, s / total)


def _evidence_score(claims: list[dict]) -> float:
    if not claims:
        return 0.0
    return min(1.0, len(claims) / 10.0)


# ---- explanation ----

def _template_explanation(job: dict, scores: dict, ats: dict) -> str:
    overall = int(round(scores.get("overall", 0) * 100))
    required = [k for k in ats.get("keywords", []) if k.get("importance") == "required"]
    safe_required = [k for k in required if k.get("resume_safe")]
    pieces = [f"Match score {overall}/100."]
    if required:
        pieces.append(
            f"{len(safe_required)} of {len(required)} required keywords supported by evidence."
        )
    if job.get("salary_max") or job.get("salary_min"):
        smin = job.get("salary_min") or 0
        smax = job.get("salary_max") or 0
        if smin and smax:
            pieces.append(f"Salary {smin:,}-{smax:,} {job.get('currency') or 'USD'}.")
        elif smax:
            pieces.append(f"Salary up to {smax:,} {job.get('currency') or 'USD'}.")
    gaps = [
        k["keyword"]
        for k in ats.get("keywords", [])
        if k.get("importance") == "required" and k.get("support_status") == "unsupported"
    ][:6]
    if gaps:
        pieces.append("Gaps: " + ", ".join(gaps) + " — no evidence yet.")
    return " ".join(pieces)


def _explain(job: dict, scores: dict, ats: dict) -> str:
    base = _template_explanation(job, scores, ats)
    if get_llm is None:
        return base
    try:
        provider = get_llm()
        # Skip LLM polish for the template provider — it just echoes
        if getattr(provider, "name", "") in ("", "template"):
            return base
        polished = provider.complete(
            system=(
                "You polish job-match explanations. Keep facts identical; tighten phrasing; "
                "2-3 sentences max; no exclamation marks; no emojis."
            ),
            user=f"Original explanation:\n{base}",
            max_tokens=180,
            temperature=0.2,
        )
        polished = (polished or "").strip()
        if polished and len(polished) < len(base) * 3:
            return polished
    except Exception:
        pass
    return base


# ---- persistence ----

def _upsert_match(job_id: int, payload: dict) -> int:
    now = time.time()
    with tx() as conn:
        conn.execute("DELETE FROM job_match WHERE job_id = ?", (job_id,))
        cur = conn.execute(
            """INSERT INTO job_match
               (job_id, overall_score, skills_score, experience_score, salary_score,
                location_score, seniority_score, keyword_score, evidence_score,
                explanation, matched_keywords, transferable_keywords, missing_keywords,
                unsupported_keywords, red_flags, recommended_resume_strategy, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                payload["overall_score"],
                payload["skills_score"],
                payload["experience_score"],
                payload["salary_score"],
                payload["location_score"],
                payload["seniority_score"],
                payload["keyword_score"],
                payload["evidence_score"],
                payload["explanation"],
                json.dumps(payload["matched_keywords"]),
                json.dumps(payload["transferable_keywords"]),
                json.dumps(payload["missing_keywords"]),
                json.dumps(payload["unsupported_keywords"]),
                json.dumps(payload["red_flags"]),
                payload.get("recommended_resume_strategy", ""),
                now,
            ),
        )
        return int(cur.lastrowid)


# ---- public API ----

def _job_record_text(job: dict) -> str:
    parts = [job.get("title") or "", job.get("description") or ""]
    req = job.get("requirements")
    if isinstance(req, list):
        parts.append("\n".join(str(x) for x in req))
    elif req:
        parts.append(str(req))
    return "\n\n".join(p for p in parts if p)


def score_job(job_id: int, weights: Optional[dict] = None) -> dict:
    conn = get_conn()
    job_row = conn.execute("SELECT * FROM job_posting WHERE id = ?", (job_id,)).fetchone()
    if not job_row:
        raise ValueError(f"job_posting id={job_id} not found")
    job = row_to_dict(job_row) or {}

    user = _load_user_profile()
    job_text = _job_record_text(job)
    claims = _load_claims_for_job(job_text)

    ats = ats_analyzer.analyze_job(job, claims)

    job_level = seniority_parser.detect_seniority(
        job.get("title") or "", job.get("description") or ""
    )

    scores = {
        "skills": _skills_score(ats),
        "experience": _experience_score(claims, job_level),
        "salary": _salary_score(job, user),
        "location": _location_score(job, user),
        "seniority": _seniority_score(job, user),
        "keywords": _keyword_score(ats),
        "evidence": _evidence_score(claims),
    }

    # Determine which weights to skip based on missing user profile data.
    skip: set[str] = set()
    if not user.get("preferred_salary") and not user.get("minimum_salary"):
        # Only skip when the job itself has no salary either — otherwise keep neutral score in.
        if not (job.get("salary_min") or job.get("salary_max")):
            skip.add("salary")
    if not user.get("preferred_locations") and not user.get("remote_preference") and not user.get("location"):
        skip.add("location")
    if not user.get("seniority_targets"):
        skip.add("seniority")
    if not claims:
        # experience can't be assessed without claims
        skip.add("experience")

    weights = dict(weights or user.get("scoring_weights_json") or default_weights())
    eff_weights = _renormalize(weights, skip)

    overall = 0.0
    for k, w in eff_weights.items():
        overall += float(scores.get(k, 0.0)) * float(w)
    scores["overall"] = round(overall, 4)

    # Categorize keywords for the persisted lists
    matched = [k["keyword"] for k in ats["keywords"] if k["support_status"] == "supported"]
    transferable = [k["keyword"] for k in ats["keywords"] if k["support_status"] == "transferable"]
    missing = [k["keyword"] for k in ats["keywords"] if k["support_status"] == "weak_evidence"]
    unsupported = [k["keyword"] for k in ats["keywords"] if k["support_status"] == "unsupported"]
    red_flags: list[str] = []
    if ats.get("ats_risk") == "high":
        red_flags.append("Many required keywords lack evidence — high ATS fabrication risk.")
    if scores["salary"] < 0.5:
        red_flags.append("Salary below your stated minimum.")
    if scores["seniority"] < 0.4:
        red_flags.append(f"Level mismatch ({job_level}).")

    explanation = _explain(job, scores, ats)

    strategy = (
        "evidence_first" if scores["overall"] >= 0.7
        else "transferable_skills" if scores["skills"] < 0.5
        else "standard"
    )

    payload = {
        "overall_score": scores["overall"],
        "skills_score": round(scores["skills"], 4),
        "experience_score": round(scores["experience"], 4),
        "salary_score": round(scores["salary"], 4),
        "location_score": round(scores["location"], 4),
        "seniority_score": round(scores["seniority"], 4),
        "keyword_score": round(scores["keywords"], 4),
        "evidence_score": round(scores["evidence"], 4),
        "explanation": explanation,
        "matched_keywords": matched,
        "transferable_keywords": transferable,
        "missing_keywords": missing,
        "unsupported_keywords": unsupported,
        "red_flags": red_flags,
        "recommended_resume_strategy": strategy,
    }

    match_id = _upsert_match(job_id, payload)

    try:
        audit("score_job", "job_posting", job_id, overall=payload["overall_score"],
              ats_risk=ats.get("ats_risk"))
    except Exception:
        pass

    return {
        "id": match_id,
        "job_id": job_id,
        **payload,
        "ats": ats,
        "weights_used": eff_weights,
        "level_detected": job_level,
    }


def score_jobs(job_ids: list[int]) -> dict:
    results = []
    errors = []
    for jid in job_ids or []:
        try:
            results.append(score_job(int(jid)))
        except Exception as e:
            errors.append({"job_id": jid, "error": str(e)})
    return {"results": results, "errors": errors, "count": len(results)}
