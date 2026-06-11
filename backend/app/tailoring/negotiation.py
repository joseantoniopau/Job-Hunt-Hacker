"""Negotiation prep — produce a structured negotiation script for a given
offer, grounded in market data + the user's verified evidence.

Honesty contract: every "talking point" carries a `provenance` field
listing the claim_ids it draws from. No fabricated wins. If the LLM
provider is unavailable, we fall back to a deterministic template.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from ..db import audit, get_conn, row_to_dict, tx
from ..services import salary_intelligence

log = logging.getLogger("jhh.tailoring.negotiation")

try:
    from ..llm import get_llm  # type: ignore
    from ..llm.json_repair import extract_json  # type: ignore
    from ..llm.observability import observed_complete  # type: ignore
except Exception:  # pragma: no cover
    get_llm = None  # type: ignore
    extract_json = None  # type: ignore
    observed_complete = None  # type: ignore


# ---- helpers ----

def _load_application(application_id: int) -> dict:
    conn = get_conn()
    row = conn.execute(
        """SELECT a.*, j.title AS job_title, j.company AS job_company,
                  j.location AS job_location, j.salary_min, j.salary_max,
                  j.currency AS job_currency, j.description AS job_description
           FROM application a
           LEFT JOIN job_posting j ON j.id = a.job_id
           WHERE a.id = ?""",
        (int(application_id),),
    ).fetchone()
    if not row:
        raise ValueError(f"application id={application_id} not found")
    return dict(row)


def _retrieve_claims(app: dict, max_claims: int = 8) -> list[dict]:
    """Pull a handful of the strongest, evidence-bearing claims so the
    negotiation script can cite real wins."""
    try:
        from ..services import career_vault  # type: ignore
        fn = getattr(career_vault, "retrieve_for_job", None)
        if callable(fn):
            blob = " ".join([app.get("job_title") or "", app.get("job_description") or ""])
            hits = fn(blob, top=max_claims) or []
            if hits:
                return hits
    except Exception:
        pass
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM career_claim WHERE allowed_for_resume = 1
           ORDER BY confidence DESC LIMIT ?""",
        (int(max_claims),),
    ).fetchall()
    return [row_to_dict(r) for r in rows if r]


def compare_to_market(
    offer: int,
    role: str,
    location: Optional[str] = None,
    currency: str = "USD",
) -> dict:
    """Compare a single offer number against the market percentiles for the
    role. Returns the offer's approximate percentile and a recommendation.
    """
    if offer is None or offer <= 0:
        raise ValueError("offer must be a positive integer")
    market = salary_intelligence.compute_market(role=role, location=location, currency=currency)
    p25 = market.get("p25")
    median = market.get("median")
    p75 = market.get("p75")
    p90 = market.get("p90")

    # Bucket the offer to a percentile band.
    if median is None:
        percentile_band = None
        gap_to_median = None
        gap_pct = None
        recommendation = (
            "Not enough comparable market data to anchor a counter. Use Glassdoor / "
            "Levels.fyi for a rough check, and counter on non-cash levers (equity, "
            "signing bonus, start date, time off)."
        )
    else:
        # Approximate the offer's percentile band from the four known points.
        if p90 is not None and offer >= p90:
            percentile_band = "90+"
        elif p75 is not None and offer >= p75:
            percentile_band = "75-90"
        elif median is not None and offer >= median:
            percentile_band = "50-75"
        elif p25 is not None and offer >= p25:
            percentile_band = "25-50"
        else:
            percentile_band = "0-25"
        gap_to_median = int(median) - int(offer)
        gap_pct = round((gap_to_median / int(median)) * 100, 1) if median else None
        if percentile_band == "0-25":
            recommendation = (
                f"Offer is below the 25th percentile. Counter aggressively — target "
                f"at minimum the median ({median:,} {currency}); aim higher if your "
                f"evidence is strong."
            )
        elif percentile_band == "25-50":
            recommendation = (
                f"Below median by {abs(gap_pct or 0)}%. Counter to median or 75th "
                f"({p75:,} {currency}) backed by a market anchor."
            )
        elif percentile_band == "50-75":
            recommendation = (
                f"At or above median. Counter toward the 75th percentile "
                f"({p75:,} {currency}) and ask for one or two non-cash items."
            )
        elif percentile_band == "75-90":
            recommendation = (
                "Already in the top quartile. Counter modestly on base and push "
                "harder on equity, signing bonus, or PTO."
            )
        else:
            recommendation = (
                "Offer is already top-decile. Focus the counter on equity refresh "
                "schedule and ramp accelerators rather than base."
            )
    return {
        "offer": int(offer),
        "role": role,
        "location": location,
        "currency": currency,
        "market_count": market.get("count", 0),
        "market_p25": p25,
        "market_median": median,
        "market_p75": p75,
        "market_p90": p90,
        "percentile_band": percentile_band,
        "gap_to_median": gap_to_median,
        "gap_pct": gap_pct,
        "recommendation": recommendation,
    }


def _deterministic_script(
    app: dict,
    claims: list[dict],
    market_compare: dict,
    offer_base: int,
    offer_total: int,
    currency: str,
) -> dict:
    company = app.get("job_company") or "the company"
    role = app.get("job_title") or "the role"
    median = market_compare.get("market_median")
    p75 = market_compare.get("market_p75")

    opening = (
        f"Thank you for the offer for the {role} position at {company}. "
        f"I'm excited about the team and the work you've described."
    )
    if median:
        market_anchor = (
            f"Based on current market data for {role} roles (median around "
            f"{median:,} {currency}, 75th percentile around "
            f"{(p75 or median):,} {currency}), I'd like to discuss the base "
            f"compensation."
        )
    else:
        market_anchor = (
            "Before I sign, I'd like to share a couple of data points on what "
            f"comparable {role} roles are paying right now."
        )

    if median and offer_base < median:
        counter = int(round(median))
    elif p75 and offer_base < p75:
        counter = int(round(p75))
    else:
        # 8% bump is the conventional "safe" counter when the offer is at/above market
        counter = int(round(offer_base * 1.08))

    counter_ask = (
        f"Given my experience and the value I'd bring, I'd like to ask for a "
        f"base of {counter:,} {currency}. I'd also be open to discussing the "
        f"signing bonus, equity refresh, and start date if base is fixed."
    )
    fallback_position = (
        f"If the base is firm, I'd ask for {max(int(round(offer_base * 1.04)), counter):,} "
        f"{currency} base or, alternatively, a signing bonus of "
        f"{int(round(offer_base * 0.10)):,} {currency}, an additional 5 days of PTO, "
        f"and an early performance review at 6 months."
    )
    walkaway = (
        f"My minimum acceptable offer is {int(round(offer_base * 0.95)):,} "
        f"{currency} all-in. Below that, given other conversations I'm in, I'd "
        f"have to respectfully pass."
    )

    talking_points: list[dict] = []
    for c in claims[:5]:
        text = (c.get("claim_text") or "").strip()
        if not text or len(text.split()) < 4:
            continue
        cid = c.get("id")
        talking_points.append({
            "point": text,
            "claim_ids": [cid] if cid else [],
        })
    if not talking_points:
        talking_points.append({
            "point": (
                "I'm a strong fit for the scope outlined — happy to walk through "
                "specific examples that map to your team's priorities."
            ),
            "claim_ids": [],
        })

    return {
        "opening": opening,
        "market_anchor": market_anchor,
        "counter_ask": counter_ask,
        "fallback_position": fallback_position,
        "walkaway": walkaway,
        "talking_points": talking_points,
    }


_LLM_SYS = (
    "You are a compensation negotiation coach. You will be given an offer, "
    "the candidate's verified career evidence, and current market data. "
    "Produce a structured negotiation script. CRITICAL: never fabricate "
    "achievements. Every talking point you include must cite the candidate's "
    "claim_id list as its provenance — if no claim supports a point, leave "
    "the claim_ids list empty and phrase it generically. Output ONLY valid "
    "JSON with keys: opening, market_anchor, counter_ask, fallback_position, "
    "walkaway, talking_points (array of {point, claim_ids})."
)


def _llm_script(app, claims, market_compare, offer_base, offer_total,
                currency) -> tuple[dict, Optional[int]]:
    """Run the negotiation-script LLM call under observability.

    Returns (script_dict, llm_run_id). The script is {} when the call
    failed or produced unusable output (caller falls back to the
    deterministic template); llm_run_id is None when no run row was
    recorded.
    """
    if get_llm is None or observed_complete is None or extract_json is None:
        return {}, None
    try:
        provider = get_llm()
        evidence = [
            {"id": c.get("id"), "text": (c.get("claim_text") or "")[:200]}
            for c in claims if c.get("id")
        ]
        user = json.dumps({
            "company": app.get("job_company"),
            "role": app.get("job_title"),
            "location": app.get("job_location"),
            "offer_base": offer_base,
            "offer_total": offer_total,
            "currency": currency,
            "market": {
                "median": market_compare.get("market_median"),
                "p75": market_compare.get("market_p75"),
                "p90": market_compare.get("market_p90"),
                "count": market_compare.get("market_count"),
                "percentile_band": market_compare.get("percentile_band"),
            },
            "evidence": evidence,
        })
        raw, run_id = observed_complete(
            provider,
            "negotiation_script",
            _LLM_SYS,
            user,
            max_tokens=1200,
            temperature=0.3,
            target_type="application",
            target_id=int(app.get("id") or 0) or None,
        )
        llm_run_id = int(run_id) if run_id and run_id > 0 else None
        data = extract_json(raw or "")
        if not isinstance(data, dict):
            return {}, llm_run_id
        return data, llm_run_id
    except Exception as e:
        log.warning("LLM negotiation script failed: %s", e)
        return {}, None


def _filter_provenance(script: dict, allowed_ids: set[int]) -> dict:
    """Strip any claim_ids not in `allowed_ids` (guardrail against the LLM
    hallucinating ids)."""
    tps = script.get("talking_points") or []
    if not isinstance(tps, list):
        return script
    clean: list[dict] = []
    for tp in tps:
        if not isinstance(tp, dict):
            continue
        ids = [int(i) for i in (tp.get("claim_ids") or []) if isinstance(i, (int, str))
               and str(i).isdigit() and int(i) in allowed_ids]
        clean.append({"point": (tp.get("point") or "").strip(), "claim_ids": ids})
    script["talking_points"] = clean
    return script


def generate(
    application_id: int,
    offer_base: int,
    offer_total: int,
    currency: str = "USD",
) -> dict:
    if offer_base is None or offer_base <= 0:
        raise ValueError("offer_base must be a positive integer")
    if offer_total is None or offer_total <= 0:
        offer_total = offer_base
    app = _load_application(application_id)
    role = app.get("job_title") or ""
    location = app.get("job_location")
    market_compare = compare_to_market(offer_base, role=role, location=location, currency=currency)
    claims = _retrieve_claims(app)
    allowed_ids = {int(c["id"]) for c in claims if c.get("id")}

    # Try LLM first; fall back to deterministic if it fails or is unavailable.
    script, llm_run_id = _llm_script(app, claims, market_compare, offer_base, offer_total, currency)
    used_provider = "llm"
    if not script or not script.get("opening"):
        script = _deterministic_script(app, claims, market_compare, offer_base, offer_total, currency)
        used_provider = "template"

    script = _filter_provenance(script, allowed_ids)

    # Build provenance summary for the overall script
    distinct_ids = sorted({
        i
        for tp in (script.get("talking_points") or [])
        for i in (tp.get("claim_ids") or [])
    })

    out = {
        "application_id": int(application_id),
        "company": app.get("job_company"),
        "role": role,
        "currency": currency,
        "offer_base": int(offer_base),
        "offer_total": int(offer_total),
        "market": market_compare,
        "script": script,
        "llm_run_id": llm_run_id,
        "provenance": {
            "claim_ids": distinct_ids,
            "claims_available": len(allowed_ids),
            "claims_cited": len(distinct_ids),
            "provider": used_provider,
        },
    }
    try:
        audit(
            "negotiation_script_generated",
            "application",
            int(application_id),
            offer_base=offer_base,
            provider=used_provider,
            llm_run_id=llm_run_id,
        )
    except Exception:
        pass
    return out


__all__ = ["generate", "compare_to_market"]
