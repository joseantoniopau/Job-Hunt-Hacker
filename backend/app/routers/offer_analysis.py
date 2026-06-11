"""LLM-powered offer analysis.

Endpoints:
  POST /api/offers/analyze   — break down a single offer
  GET  /api/offers           — list past analyses (newest first)
  GET  /api/offers/{app_id}  — latest analysis for an application
  POST /api/offers/compare   — compare N analyses side-by-side

Every LLM call runs through observability so the UI can show the prompt
and output behind a VIEW LLM REASONING link.

Honesty rule (in every prompt): Use ONLY facts in OFFER TEXT, JOB
DESCRIPTION, MARKET DATA, and CANDIDATE EVIDENCE. Never invent strike
prices, comp benchmarks, or candidate accomplishments. If a number isn't
stated, mark it unknown or estimated with explicit confidence.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import get_conn, tx, audit
from ..llm import get_llm
from ..llm.json_repair import extract_json
from ..llm.observability import observed_complete
from ..services import career_vault, salary_intelligence

log = logging.getLogger("jhh.routers.offer_analysis")

router = APIRouter(prefix="/api/offers", tags=["offer_analysis"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AnalyzeBody(BaseModel):
    application_id: Optional[int] = None
    offer_text: str


class CompareBody(BaseModel):
    analysis_ids: list[int]


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _load_job_context(application_id: Optional[int]) -> dict:
    """Pull (title, company, location, salary band, description first 2000
    chars) for the job tied to an application. Returns {} if no app or
    nothing found."""
    if not application_id:
        return {}
    conn = get_conn()
    row = conn.execute(
        """SELECT j.id AS job_id, j.title, j.company, j.location,
                  j.salary_min, j.salary_max, j.currency,
                  j.bonus_equity_text, j.description, j.remote_type,
                  j.employment_type, j.benefits, a.status AS app_status
           FROM application a
           JOIN job_posting j ON j.id = a.job_id
           WHERE a.id = ?""",
        (int(application_id),),
    ).fetchone()
    if not row:
        return {}
    d = dict(row)
    desc = (d.get("description") or "")
    d["description"] = desc[:2000]
    return d


def _load_user_profile() -> dict:
    row = get_conn().execute(
        """SELECT location, minimum_salary, preferred_salary, currency,
                  target_titles
           FROM user_profile WHERE id = 1"""
    ).fetchone()
    if not row:
        return {}
    return dict(row)


def _load_market_data(title: str, location: Optional[str], currency: str) -> dict:
    """Call salary intelligence directly (in-process — no HTTP loopback)."""
    if not (title or "").strip():
        return {}
    try:
        return salary_intelligence.compute_market(
            role=title,
            location=location or None,
            currency=(currency or "USD"),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("market data lookup failed: %s", exc)
        return {}


def _load_top_claims(top: int = 10) -> list[dict]:
    """Top verified+allowed claims to inform leverage angles. We do a
    direct list (cheaper than retrieve_for_job — we don't have a single
    query string here)."""
    try:
        rows = career_vault.list_claims(verified_only=True, allowed_only=True)
    except Exception:
        rows = []
    out: list[dict] = []
    for r in rows[:top]:
        out.append({
            "id": r.get("id"),
            "text": (r.get("claim_text") or "").strip(),
            "claim_type": r.get("claim_type") or "",
            "skill": r.get("skill") or "",
            "employer": r.get("employer") or "",
        })
    return out


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_HONESTY_RULE = (
    "Use ONLY facts in the OFFER TEXT, JOB DESCRIPTION, MARKET DATA, and "
    "CANDIDATE EVIDENCE blocks below. Never invent equity strike prices, "
    "comp benchmarks, or candidate accomplishments. If a number isn't "
    "stated, mark it 'unknown' or 'estimated' with explicit confidence."
)


def _build_analyze_system() -> str:
    return (
        "You are an offer-analysis expert helping a candidate evaluate a job offer. "
        "Output JSON ONLY — no preamble, no markdown fences. "
        + _HONESTY_RULE +
        " Be honest about red flags (non-compete, IP assignment, forfeit "
        "clauses, unusual vesting, golden handcuffs, exploding offers). "
        "If a section does not apply (e.g. no equity), return it with "
        "empty/unknown fields rather than fabricating."
    )


def _build_analyze_user(
    offer_text: str,
    job: dict,
    profile: dict,
    market: dict,
    claims: list[dict],
) -> str:
    job_block = "(no job context — analyze offer in isolation)"
    if job:
        job_block = (
            f"Title: {job.get('title') or 'unknown'}\n"
            f"Company: {job.get('company') or 'unknown'}\n"
            f"Location: {job.get('location') or 'unknown'} "
            f"({job.get('remote_type') or 'unknown'})\n"
            f"Listed salary band: "
            f"{job.get('salary_min') or '?'}–{job.get('salary_max') or '?'} "
            f"{job.get('currency') or ''}\n"
            f"Listed bonus/equity: {job.get('bonus_equity_text') or '(none on posting)'}\n"
            f"Description (truncated): {job.get('description') or ''}"
        )

    market_block = "(no recent comparable postings indexed)"
    if market and market.get("count"):
        market_block = (
            f"Window: last {market.get('window_days')} days. "
            f"N={market.get('count')} comparable postings "
            f"matching role='{market.get('role')}' "
            f"location='{market.get('location') or 'any'}' "
            f"currency={market.get('currency')}.\n"
            f"p25={market.get('p25')} median={market.get('median')} "
            f"p75={market.get('p75')} p90={market.get('p90')}."
        )

    profile_block = (
        f"Candidate location: {profile.get('location') or 'unknown'}\n"
        f"Candidate currency: {profile.get('currency') or 'USD'}\n"
        f"Minimum acceptable: {profile.get('minimum_salary') or 'not set'}\n"
        f"Preferred target:   {profile.get('preferred_salary') or 'not set'}"
    )

    claims_block = "(no verified vault claims available)"
    if claims:
        lines = []
        for c in claims:
            lines.append(
                f"  - claim#{c.get('id')} [{c.get('claim_type') or 'evidence'}]: "
                f"{c.get('text')}"
            )
        claims_block = "\n".join(lines)

    schema = (
        "{\n"
        '  "components": {\n'
        '    "base_salary":   {"value_text": "...", "confidence": "stated|estimated|unknown"},\n'
        '    "bonus":         {"value_text": "...", "confidence": "..."},\n'
        '    "equity":        {"value_text": "...", "confidence": "..."},\n'
        '    "sign_on":       {"value_text": "...", "confidence": "..."},\n'
        '    "benefits":      ["...", "..."],\n'
        '    "total_compensation_estimate": {"value_text": "...", "confidence": "..."}\n'
        "  },\n"
        '  "market_comparison": {\n'
        '    "percentile_estimate": "e.g. ~p60 of the band, or unknown",\n'
        '    "market_low":  <number or null>,\n'
        '    "market_mid":  <number or null>,\n'
        '    "market_high": <number or null>,\n'
        '    "leverage_factors": ["...", "..."]\n'
        "  },\n"
        '  "counter_script": [\n'
        '    {"angle_name": "...", "pitch": "complete pitch text the candidate could say verbatim",\n'
        '     "evidence_basis": [<claim_id>, <claim_id>], "suggested_ask": "specific dollar/percent/structure ask"},\n'
        '    {... 3 angles total ...}\n'
        "  ],\n"
        '  "red_flags": [\n'
        '    {"flag": "...", "severity": "low|medium|high", "explanation": "..."}\n'
        "  ],\n"
        '  "equity_analysis": {\n'
        '    "strike_price": "stated or unknown",\n'
        '    "vesting_cliff": "stated or unknown",\n'
        '    "vesting_schedule": "stated or unknown",\n'
        '    "fdv_stated": "stated or unknown",\n'
        '    "dilution_risk": "low|medium|high|unknown",\n'
        '    "notes": "..."\n'
        "  },\n"
        '  "total_score": <0-100>,\n'
        '  "recommendation": "accept|negotiate|counter_hard|walk"\n'
        "}"
    )

    return (
        "OFFER TEXT:\n"
        "----------\n"
        f"{offer_text.strip()}\n"
        "----------\n\n"
        "JOB DESCRIPTION:\n"
        "----------\n"
        f"{job_block}\n"
        "----------\n\n"
        "MARKET DATA:\n"
        "----------\n"
        f"{market_block}\n"
        "----------\n\n"
        "CANDIDATE PROFILE:\n"
        "----------\n"
        f"{profile_block}\n"
        "----------\n\n"
        "CANDIDATE EVIDENCE (verified vault claims):\n"
        "----------\n"
        f"{claims_block}\n"
        "----------\n\n"
        + _HONESTY_RULE + "\n\n"
        "Produce exactly this JSON shape (fill or mark 'unknown'/empty — never invent):\n"
        + schema
    )


def _build_compare_system() -> str:
    return (
        "You compare multiple job offers honestly using ONLY the analyses "
        "provided. Output JSON ONLY — no preamble, no fences. "
        + _HONESTY_RULE +
        " Highlight which offer is strongest on comp, growth, risk, and "
        "fit. Apply regret-minimization framing: which choice would the "
        "candidate regret least 12 months out?"
    )


def _build_compare_user(analyses: list[dict]) -> str:
    lines: list[str] = ["ANALYSES TO COMPARE:\n"]
    for i, a in enumerate(analyses, 1):
        comp = a.get("components") or {}
        market = a.get("market_comparison") or {}
        red = a.get("red_flags") or []
        lines.append(f"--- Offer #{i} (analysis_id={a.get('id')}) ---")
        lines.append(f"Company: {a.get('company') or 'unknown'}")
        lines.append(f"Title:   {a.get('title') or 'unknown'}")
        lines.append(f"Base:    {((comp.get('base_salary') or {}).get('value_text')) or 'unknown'}")
        lines.append(f"Bonus:   {((comp.get('bonus') or {}).get('value_text')) or 'unknown'}")
        lines.append(f"Equity:  {((comp.get('equity') or {}).get('value_text')) or 'unknown'}")
        lines.append(f"Sign-on: {((comp.get('sign_on') or {}).get('value_text')) or 'unknown'}")
        lines.append(f"Total:   {((comp.get('total_compensation_estimate') or {}).get('value_text')) or 'unknown'}")
        lines.append(f"Market percentile: {market.get('percentile_estimate') or 'unknown'}")
        lines.append(f"Recommendation: {a.get('recommendation') or 'unknown'}")
        lines.append(f"Total score: {a.get('total_score')}")
        if red:
            lines.append("Red flags:")
            for f in red[:5]:
                lines.append(f"  - [{f.get('severity')}] {f.get('flag')}: {f.get('explanation')}")
        lines.append("")

    schema = (
        "{\n"
        '  "scorecard": [\n'
        '    {"analysis_id": <int>, "company": "...", "title": "...",\n'
        '     "comp_score": <0-100>, "growth_score": <0-100>,\n'
        '     "risk_score": <0-100>, "fit_score": <0-100>,\n'
        '     "overall": <0-100>, "headline": "one-line take"}\n'
        "  ],\n"
        '  "regret_minimization": {\n'
        '    "least_regret_12mo": <analysis_id>,\n'
        '    "reasoning": "why this offer minimizes 12-month regret"\n'
        "  },\n"
        '  "recommendation": "id N or walk",\n'
        '  "reasoning": "honest summary"\n'
        "}"
    )
    lines.append(_HONESTY_RULE)
    lines.append("")
    lines.append("Produce exactly this JSON shape:")
    lines.append(schema)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _persist_analysis(
    application_id: Optional[int],
    offer_text: str,
    parsed: dict,
    llm_run_id: int,
) -> int:
    """Insert and return id."""
    rec = (parsed.get("recommendation") or "negotiate").strip().lower()
    try:
        score = float(parsed.get("total_score"))
    except Exception:
        score = 0.0

    now = time.time()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO offer_analysis
               (application_id, created_at, offer_text,
                components_json, market_comparison_json, counter_script_json,
                red_flags_json, equity_analysis_json,
                total_score, recommendation, llm_run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                application_id,
                now,
                offer_text,
                json.dumps(parsed.get("components") or {}),
                json.dumps(parsed.get("market_comparison") or {}),
                json.dumps(parsed.get("counter_script") or []),
                json.dumps(parsed.get("red_flags") or []),
                json.dumps(parsed.get("equity_analysis") or {}),
                score,
                rec,
                int(llm_run_id) if llm_run_id and llm_run_id > 0 else None,
            ),
        )
        return int(cur.lastrowid)


def _row_to_analysis(row: Any) -> dict:
    d = dict(row)
    def _j(key: str, default):
        raw = d.get(key)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default
    return {
        "id": d.get("id"),
        "application_id": d.get("application_id"),
        "created_at": d.get("created_at"),
        "offer_text": d.get("offer_text") or "",
        "components": _j("components_json", {}),
        "market_comparison": _j("market_comparison_json", {}),
        "counter_script": _j("counter_script_json", []),
        "red_flags": _j("red_flags_json", []),
        "equity_analysis": _j("equity_analysis_json", {}),
        "total_score": d.get("total_score"),
        "recommendation": d.get("recommendation"),
        "llm_run_id": d.get("llm_run_id"),
        "company": d.get("company"),
        "title": d.get("title"),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/analyze")
def analyze(body: AnalyzeBody) -> dict:
    offer_text = (body.offer_text or "").strip()
    if not offer_text and not body.application_id:
        raise HTTPException(400, "either application_id or offer_text is required")
    if not offer_text:
        raise HTTPException(400, "offer_text is required (paste the offer letter / verbal offer summary)")

    job = _load_job_context(body.application_id)
    profile = _load_user_profile()
    title = (job.get("title") or "").strip() if job else ""
    if not title:
        # fall back to first target title so market lookup still has signal
        raw = (profile.get("target_titles") or "").strip()
        if raw:
            try:
                lst = json.loads(raw)
                if isinstance(lst, list) and lst:
                    title = str(lst[0])
            except Exception:
                title = raw.split(",")[0].strip()
    location = (job.get("location") if job else None) or profile.get("location")
    currency = (job.get("currency") if job else None) or profile.get("currency") or "USD"
    market = _load_market_data(title, location, currency)
    claims = _load_top_claims(top=10)

    system = _build_analyze_system()
    user = _build_analyze_user(offer_text, job, profile, market, claims)

    try:
        provider = get_llm()
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM provider init failed: %s", exc)
        raise HTTPException(500, "LLM provider init failed (see server log)")

    try:
        output, run_id = observed_complete(
            provider,
            stage="offer_analysis",
            system=system,
            user=user,
            max_tokens=3500,
            temperature=0.15,
            target_type="application",
            target_id=body.application_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("offer analysis LLM call failed")
        raise HTTPException(500, "LLM call failed (see server log)")

    parsed = extract_json(output or "") or {}
    if not isinstance(parsed, dict):
        parsed = {}

    # Guarantee shape minimally — never invent values, but ensure the keys
    # exist so the UI doesn't trip on undefined access.
    parsed.setdefault("components", {})
    parsed.setdefault("market_comparison", {})
    parsed.setdefault("counter_script", [])
    parsed.setdefault("red_flags", [])
    parsed.setdefault("equity_analysis", {})
    parsed.setdefault("total_score", 0)
    parsed.setdefault("recommendation", "negotiate")

    analysis_id = _persist_analysis(body.application_id, offer_text, parsed, run_id)

    parsed["id"] = analysis_id
    parsed["application_id"] = body.application_id
    parsed["llm_run_id"] = run_id if run_id and run_id > 0 else None
    parsed["company"] = (job.get("company") if job else None)
    parsed["title"] = (job.get("title") if job else None)
    parsed["offer_text"] = offer_text

    return {"ok": True, "data": parsed}


@router.get("")
def list_analyses(limit: int = 50) -> dict:
    limit = max(1, min(int(limit or 50), 200))
    rows = get_conn().execute(
        """SELECT o.id, o.application_id, o.created_at, o.offer_text,
                  o.components_json, o.market_comparison_json,
                  o.counter_script_json, o.red_flags_json,
                  o.equity_analysis_json, o.total_score, o.recommendation,
                  o.llm_run_id,
                  j.title  AS title,
                  j.company AS company
           FROM offer_analysis o
           LEFT JOIN application a ON a.id = o.application_id
           LEFT JOIN job_posting j ON j.id = a.job_id
           ORDER BY o.created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return {"ok": True, "data": [_row_to_analysis(r) for r in rows]}


@router.get("/{application_id}")
def latest_for_app(application_id: int) -> dict:
    row = get_conn().execute(
        """SELECT o.id, o.application_id, o.created_at, o.offer_text,
                  o.components_json, o.market_comparison_json,
                  o.counter_script_json, o.red_flags_json,
                  o.equity_analysis_json, o.total_score, o.recommendation,
                  o.llm_run_id,
                  j.title  AS title,
                  j.company AS company
           FROM offer_analysis o
           LEFT JOIN application a ON a.id = o.application_id
           LEFT JOIN job_posting j ON j.id = a.job_id
           WHERE o.application_id = ?
           ORDER BY o.created_at DESC
           LIMIT 1""",
        (int(application_id),),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"no offer analysis for application {application_id}")
    return {"ok": True, "data": _row_to_analysis(row)}


@router.delete("/{analysis_id}")
def delete_analysis(analysis_id: int) -> dict:
    row = get_conn().execute(
        "SELECT id FROM offer_analysis WHERE id = ?", (int(analysis_id),),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"offer analysis {analysis_id} not found")
    with tx() as c:
        c.execute("DELETE FROM offer_analysis WHERE id = ?", (int(analysis_id),))
    audit("offer_analysis_deleted", "offer_analysis", int(analysis_id))
    return {"ok": True, "data": {"deleted": int(analysis_id)}}


@router.post("/compare")
def compare(body: CompareBody) -> dict:
    ids = [int(x) for x in (body.analysis_ids or []) if x]
    if len(ids) < 2:
        raise HTTPException(400, "compare requires at least 2 analysis_ids")
    placeholders = ",".join(["?"] * len(ids))
    rows = get_conn().execute(
        f"""SELECT o.id, o.application_id, o.created_at, o.offer_text,
                   o.components_json, o.market_comparison_json,
                   o.counter_script_json, o.red_flags_json,
                   o.equity_analysis_json, o.total_score, o.recommendation,
                   o.llm_run_id,
                   j.title  AS title,
                   j.company AS company
            FROM offer_analysis o
            LEFT JOIN application a ON a.id = o.application_id
            LEFT JOIN job_posting j ON j.id = a.job_id
            WHERE o.id IN ({placeholders})""",
        ids,
    ).fetchall()
    if len(rows) < 2:
        raise HTTPException(404, "could not load at least 2 of the requested analyses")
    analyses = [_row_to_analysis(r) for r in rows]

    system = _build_compare_system()
    user = _build_compare_user(analyses)

    try:
        provider = get_llm()
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM provider init failed: %s", exc)
        raise HTTPException(500, "LLM provider init failed (see server log)")

    try:
        output, run_id = observed_complete(
            provider,
            stage="offer_analysis",
            system=system,
            user=user,
            max_tokens=1800,
            temperature=0.15,
            target_type="application",
            target_id=None,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("offer compare LLM call failed")
        raise HTTPException(500, "LLM call failed (see server log)")

    parsed = extract_json(output or "") or {}
    if not isinstance(parsed, dict):
        parsed = {}
    parsed.setdefault("scorecard", [])
    parsed.setdefault("regret_minimization", {})
    parsed.setdefault("recommendation", "")
    parsed.setdefault("reasoning", "")

    return {
        "ok": True,
        "data": {
            "analyses": analyses,
            "comparison": parsed,
            "llm_run_id": run_id if run_id and run_id > 0 else None,
        },
    }
