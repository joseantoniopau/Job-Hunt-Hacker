"""Generate interview talking points + likely questions, grounded in evidence."""
from __future__ import annotations

import logging
from typing import Any

from ..db import audit, get_conn, row_to_dict
from ..llm import get_llm
from ..llm import guardrails
from ..llm.prompts import INTERVIEW_PREP_SYS, INTERVIEW_PREP_USER
from .honesty_report import build_report
from .provenance import ProvenanceMap
from .resume_tailor import _coerce_claims_for_prompt, _safe_retrieve_claims

log = logging.getLogger("jhh.tailoring.interview")


def _load_job(job_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM job_posting WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"job_posting id={job_id} not found")
    return row_to_dict(row) or {}


def _deterministic_prep(job: dict, claims: list[dict]) -> dict:
    # Talking points must be SENTENCE-shaped evidence, not bare skill
    # tokens. Same honesty rule we use in cover_letter + recruiter_messages
    # — a recruiter who hears "tell me about a recent accomplishment"
    # and gets back "postgresql" learns nothing.
    SENTENCE_TYPES = {"role", "accomplishment", "responsibility",
                      "leadership", "project", "metric", "experience"}
    points = []
    for c in claims:
        if len(points) >= 5:
            break
        text = (c.get("claim_text") or "").strip()
        if not text or len(text.split()) < 4:
            continue
        ctype = (c.get("claim_type") or "").lower()
        if ctype and ctype not in SENTENCE_TYPES:
            continue
        points.append({"text": text, "evidence_ids": [c["id"]]})
    role = job.get("title") or "the role"
    likely = [
        f"Walk me through your most relevant project for {role}.",
        "What's a measurable outcome you're proud of?",
        "How do you make trade-offs under deadline pressure?",
        "What gap do you have, and how would you close it in 90 days?",
        "Tell me about a time you disagreed with a teammate.",
    ]
    return {
        "talking_points": points,
        "likely_questions": likely,
        "evidence_map": {q: [] for q in likely},
    }


def generate(job_id: int) -> dict:
    job = _load_job(job_id)
    raw_claims = _safe_retrieve_claims(
        " ".join([job.get("title") or "", job.get("company") or "", job.get("description") or ""]),
        top=15,
    )
    claims = _coerce_claims_for_prompt(raw_claims)
    allowed_ids: set[int] = {c["id"] for c in claims}

    llm = get_llm()
    sys = INTERVIEW_PREP_SYS
    user = INTERVIEW_PREP_USER(
        {"title": job.get("title"), "company": job.get("company"), "description": job.get("description")},
        claims,
    )

    structured: dict[str, Any] = {}
    try:
        structured = llm.complete_json(sys, user, max_tokens=2000) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("LLM interview prep failed: %s", e)
        structured = {}

    if not isinstance(structured, dict) or not (structured.get("talking_points") or structured.get("likely_questions")):
        structured = _deterministic_prep(job, claims)

    cleaned = guardrails.validate_provenance(structured, allowed_ids)
    dropped = (cleaned.get("honesty_report") or {}).get("dropped_segments") or []

    pm = ProvenanceMap()
    for t_idx, tp in enumerate(cleaned.get("talking_points") or []):
        pm.link(f"talking_points[{t_idx}]", (tp or {}).get("evidence_ids") or [])

    honesty = build_report(
        provenance=pm,
        keyword_matrix=[],
        gaps_flagged=[],
        dropped_segments=dropped,
    )

    audit("interview_prep_generated", "job_posting", job_id,
          provider=getattr(llm, "name", "unknown"))

    return {
        "job_id": job_id,
        "talking_points": cleaned.get("talking_points") or [],
        "likely_questions": cleaned.get("likely_questions") or [],
        "evidence_map": cleaned.get("evidence_map") or {},
        "provenance": pm.to_dict(),
        "honesty_report": honesty,
    }


__all__ = ["generate"]
