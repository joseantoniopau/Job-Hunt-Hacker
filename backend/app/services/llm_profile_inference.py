"""LLM-powered profile inference + evidence claim extraction.

These services run alongside the deterministic regex parsers in
`backend/app/routers/profile.py` and `backend/app/services/evidence_extractor.py`.
Both are extractive-only: the LLM may only output text that is literally
in the input. Any hallucinated employer / title / metric is the failure
mode we are designed to catch.

Every call goes through `observed_complete()` so the UI's LLM activity
panel surfaces it, and so each proposal in `profile_proposal` can link to
its run id for the human reviewer.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..llm import get_llm
from ..llm.json_repair import extract_json
from ..llm.observability import observed_complete

log = logging.getLogger("jhh.llm.profile")


# The honesty rule. Repeated VERBATIM in every prompt that asks the LLM
# to read user evidence. Edit-with-caution: changing this weakens the
# trust contract the whole product is built on.
HONESTY_RULE = (
    "You may only output facts that are IN THE PROVIDED TEXT. "
    "Never invent employers, titles, dates, metrics, or skills. "
    "If a field is not in the text, return null. "
    "Distinguish company names (employer) from job titles (role). "
    "An employer is the organization that paid the person (e.g. 'eBay', "
    "'Stripe', 'Google'). A title is the role they held there (e.g. "
    "'Information Security Engineer III', 'Senior Product Manager'). "
    "Never put an employer name in a title field or vice versa."
)

# Cap input text length so a runaway resume doesn't blow the context
# window on a 70B local model. Profiles past this length tend to be
# multi-resume concats; the LLM still gets the most signal-dense top.
_MAX_INPUT_CHARS = 12000


def _trim(text: str, cap: int = _MAX_INPUT_CHARS) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…[truncated {len(text) - cap} chars]"


# ----- profile inference --------------------------------------------------

_PROFILE_SCHEMA = {
    "name": "string or null",
    "email": "string or null",
    "phone": "string or null",
    "location": "string in 'City, ST' format or null",
    "target_titles": "array of clean job-title strings (e.g. ['Senior Backend Engineer']); never include employer names",
    "target_keywords": "array of skills / technologies (e.g. ['Python', 'AWS', 'Kubernetes'])",
    "industries": "array of industry strings (e.g. ['fintech', 'healthcare'])",
    "years_experience": "integer or null",
    "seniority_level": "one of: intern, entry, mid, senior, staff, principal, manager, director, vp, exec — or null",
    "key_achievements": "array of short achievement strings ('reduced p99 latency 40% across 12 services')",
}


def _build_profile_system() -> str:
    return (
        "You extract structured job-hunt profile data from a resume and/or "
        "LinkedIn export. Output JSON ONLY (no prose, no fences, no commentary).\n\n"
        f"{HONESTY_RULE}\n\n"
        "Critical distinction:\n"
        "  EMPLOYER  = where the person worked (eBay, Stripe, Lattice)\n"
        "  TITLE     = the role they held (Information Security Engineer III, "
        "Staff Backend Engineer, Senior Product Manager)\n\n"
        "If the resume says 'Information Security Engineer III at eBay (2019-2022)', "
        "then 'Information Security Engineer III' is a TITLE and 'eBay' is an "
        "EMPLOYER. NEVER put 'eBay' or 'Senior eBay' in target_titles.\n\n"
        "If a field is not present in the text, output null (for scalars) or [] "
        "(for arrays). Do NOT make up plausible values."
    )


def _build_profile_user(resume_text: str, linkedin_text: str) -> str:
    schema_json = json.dumps(_PROFILE_SCHEMA, indent=2)
    parts: list[str] = []
    parts.append("Extract the following JSON shape:\n" + schema_json)
    parts.append("")
    if resume_text:
        parts.append("=== RESUME TEXT ===")
        parts.append(_trim(resume_text))
        parts.append("")
    if linkedin_text:
        parts.append("=== LINKEDIN TEXT ===")
        parts.append(_trim(linkedin_text))
        parts.append("")
    parts.append("Return only the JSON object — no prose, no markdown fences.")
    return "\n".join(parts)


# Fields the UI knows how to display in the human review gate. Drop
# anything the LLM produces outside this set so we don't surprise the
# user with fields the form has no input for.
_KNOWN_PROFILE_FIELDS = {
    "name", "email", "phone", "location",
    "target_titles", "target_keywords", "industries",
    "years_experience", "seniority_level", "key_achievements",
}


def _clean_profile_fields(raw: dict) -> dict:
    """Normalize the LLM's JSON. Drop empty strings, coerce nulls, keep
    only the fields the schema declared."""
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if k not in _KNOWN_PROFILE_FIELDS:
            continue
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if v.lower() in ("null", "n/a", "none", ""):
                continue
            out[k] = v
        elif isinstance(v, list):
            cleaned = [str(x).strip() for x in v if x and str(x).strip()]
            if cleaned:
                out[k] = cleaned
        elif isinstance(v, (int, float, bool)):
            out[k] = v
        # ignore unexpected dict-shaped values
    return out


def infer_with_llm(
    resume_text: str,
    linkedin_text: str = "",
    target_type: str = "",
    target_id: int | None = None,
) -> dict:
    """Run the profile inference prompt and return parsed fields + run id.

    Return shape:
        {"ok": True,  "fields": {...}, "llm_run_id": int, "raw": str}
        {"ok": False, "error": str,    "llm_run_id": int}

    Never raises. The caller decides whether to surface a partial failure.
    """
    if not (resume_text or linkedin_text):
        return {"ok": False, "error": "no input text", "llm_run_id": -1, "fields": {}}

    provider = get_llm()
    system = _build_profile_system()
    user = _build_profile_user(resume_text, linkedin_text)
    try:
        raw, run_id = observed_complete(
            provider,
            "profile_inference",
            system,
            user,
            max_tokens=1500,
            temperature=0.1,
            target_type=target_type or "profile",
            target_id=target_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM profile inference failed: %s", exc)
        return {"ok": False, "error": str(exc), "llm_run_id": -1, "fields": {}}

    if not raw or not raw.strip():
        return {"ok": False, "error": "empty LLM output",
                "llm_run_id": run_id, "fields": {}}

    parsed = extract_json(raw)
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "LLM did not return a JSON object",
                "llm_run_id": run_id, "fields": {}, "raw": raw}

    cleaned = _clean_profile_fields(parsed)
    return {"ok": True, "fields": cleaned, "llm_run_id": run_id, "raw": raw}


# ----- evidence claim extraction -----------------------------------------

_CLAIM_SCHEMA = {
    "verb": "string — the action the person took (e.g. 'reduced', 'shipped', 'led')",
    "metric": "string or null — the measured outcome (e.g. '40%', '$2M', '12 services')",
    "scope": "string or null — what was affected (e.g. 'p99 latency across the checkout API')",
    "tools": "array of tools used (e.g. ['Python', 'AWS', 'Kubernetes'])",
    "skills": "array of skills demonstrated (e.g. ['systems design', 'mentoring'])",
    "source_span": (
        "string — the EXACT substring from the input text that proves this claim. "
        "Must be literally present in the input. The verifier will reject any claim "
        "whose source_span is not found verbatim."
    ),
}


def _build_evidence_system() -> str:
    return (
        "You extract structured career claims from a single evidence source "
        "(resume bullet, LinkedIn experience entry, project write-up, etc.). "
        "Output JSON ONLY — a top-level array of claim objects.\n\n"
        f"{HONESTY_RULE}\n\n"
        "EVERY claim must include a `source_span` that is the EXACT substring "
        "of the input text proving the claim. Do not paraphrase the span. The "
        "downstream verifier rejects any claim whose source_span is not "
        "literally in the input — those claims are dropped, so making them up "
        "wastes the slot.\n\n"
        "Prefer fewer, higher-quality claims over many vague ones. If the "
        "input has no concrete claims (e.g. an empty header), return []."
    )


def _build_evidence_user(text: str) -> str:
    return (
        "Extract claims as a JSON array of objects with this shape:\n"
        + json.dumps(_CLAIM_SCHEMA, indent=2)
        + "\n\n=== INPUT TEXT ===\n"
        + _trim(text)
        + "\n\nReturn only the JSON array — no prose, no markdown fences."
    )


def _verify_source_spans(claims: list[dict], source_text: str) -> list[dict]:
    """Drop claims whose source_span is not literally in `source_text`.

    This is the deterministic counter-check that makes the extractor
    safe: even if the LLM invents a metric, the verifier rejects it
    because the proof string isn't in the input.
    """
    if not source_text:
        return []
    verified: list[dict] = []
    norm_source = source_text.lower()
    for c in claims:
        if not isinstance(c, dict):
            continue
        span = (c.get("source_span") or "").strip()
        if not span:
            continue
        if span.lower() not in norm_source:
            log.debug("dropping claim — source_span not in text: %r", span[:80])
            continue
        verified.append(c)
    return verified


def extract_evidence_claims_with_llm(text: str, source_id: int) -> list[dict]:
    """Extract structured claims from a single evidence source.

    Returns a list of verified claim dicts. Claims whose `source_span`
    isn't literally in the input are dropped before return.
    """
    if not text or not text.strip():
        return []

    provider = get_llm()
    system = _build_evidence_system()
    user = _build_evidence_user(text)
    try:
        raw, _run_id = observed_complete(
            provider,
            "evidence_extraction",
            system,
            user,
            max_tokens=1800,
            temperature=0.0,
            target_type="evidence_source",
            target_id=source_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM evidence extraction failed: %s", exc)
        return []

    if not raw or not raw.strip():
        return []

    parsed = extract_json(raw)
    if isinstance(parsed, dict):
        # sometimes wrapped in {claims: [...]}
        if isinstance(parsed.get("claims"), list):
            parsed = parsed["claims"]
        else:
            parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    return _verify_source_spans(parsed, text)


__all__ = ["infer_with_llm", "extract_evidence_claims_with_llm", "HONESTY_RULE"]
