"""Generate a cover letter with full provenance + honesty checks."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..db import audit, get_conn, row_to_dict, tx
from ..llm import get_llm
from ..llm import guardrails
from ..llm.prompts import COVER_LETTER_SYS, COVER_LETTER_USER
from .honesty_report import build_report
from .provenance import ProvenanceMap
from .resume_tailor import _coerce_claims_for_prompt, _safe_retrieve_claims, _user_header_from_profile

log = logging.getLogger("jhh.tailoring.cover_letter")


def _load_job(job_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM job_posting WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"job_posting id={job_id} not found")
    return row_to_dict(row) or {}


def _deterministic_letter(job: dict, claims: list[dict], tone: str) -> dict:
    header = _user_header_from_profile()
    name = header.get("name") or "[Your Name]"
    company = job.get("company") or "the team"
    role = job.get("title") or "this role"
    # Mark greeting / closing as kind="boilerplate" so guardrails honor
    # them: they don't claim career facts and so don't need evidence_ids.
    intro = {"text": f"Dear Hiring Manager,\n\nI'm writing to express interest in the {role} position at {company}.",
             "evidence_ids": [], "kind": "boilerplate"}
    body_paragraphs: list[dict] = []
    for c in claims[:3]:
        text = (c.get("claim_text") or "").strip()
        if not text:
            continue
        body_paragraphs.append({"text": text, "evidence_ids": [c["id"]],
                                "kind": "body"})
    outro = {"text": "I'd welcome the chance to discuss how my background fits your needs.\n\nThank you,\n" + name,
             "evidence_ids": [], "kind": "boilerplate"}
    paragraphs = [intro] + body_paragraphs + [outro]
    return {"paragraphs": paragraphs, "gaps": []}


def _paragraphs_to_text(paragraphs: list[dict]) -> str:
    parts: list[str] = []
    for p in paragraphs:
        if not isinstance(p, dict):
            continue
        t = (p.get("text") or "").strip()
        if t:
            parts.append(t)
    return "\n\n".join(parts).strip() + "\n"


def generate(job_id: int, tone: str = "professional") -> dict:
    job = _load_job(job_id)
    raw_claims = _safe_retrieve_claims(
        " ".join([job.get("title") or "", job.get("company") or "", job.get("description") or ""]),
        top=15,
    )
    claims = _coerce_claims_for_prompt(raw_claims)
    allowed_ids: set[int] = {c["id"] for c in claims}

    llm = get_llm()
    sys = COVER_LETTER_SYS
    user = COVER_LETTER_USER(
        {"title": job.get("title"), "company": job.get("company"), "description": job.get("description")},
        claims,
        tone,
    )

    structured: dict[str, Any] = {}
    try:
        structured = llm.complete_json(sys, user, max_tokens=1800) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("LLM cover letter failed: %s", e)
        structured = {}

    if not isinstance(structured, dict) or not structured.get("paragraphs"):
        structured = _deterministic_letter(job, claims, tone)

    cleaned = guardrails.validate_provenance(structured, allowed_ids)
    dropped = (cleaned.get("honesty_report") or {}).get("dropped_segments") or []

    # Provenance ONLY tracks "body" paragraphs (those making career claims).
    # Boilerplate (greeting / closing) is informational, not evidence-bearing,
    # so it doesn't move the coverage math. The README promise — "every
    # SEGMENT MAKING A CAREER CLAIM ships only with evidence" — holds.
    pm = ProvenanceMap()
    for p_idx, p in enumerate(cleaned.get("paragraphs") or []):
        if not isinstance(p, dict):
            continue
        if p.get("kind") == "boilerplate":
            continue
        pm.link(f"paragraphs[{p_idx}]", p.get("evidence_ids") or [])

    honesty = build_report(
        provenance=pm,
        keyword_matrix=[],
        gaps_flagged=cleaned.get("gaps") or [],
        dropped_segments=dropped,
    )

    text = _paragraphs_to_text(cleaned.get("paragraphs") or [])

    now = time.time()
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO cover_letter (job_id, text, provenance_json, created_at) VALUES (?, ?, ?, ?)",
            (job_id, text, json.dumps({"provenance": pm.to_dict(), "honesty_report": honesty,
                                       "paragraphs": cleaned.get("paragraphs") or [],
                                       "gaps": cleaned.get("gaps") or []}, default=str), now),
        )
        new_id = int(cur.lastrowid)

    audit("cover_letter_generated", "cover_letter", new_id, job_id=job_id,
          provider=getattr(llm, "name", "unknown"))

    return {
        "id": new_id,
        "job_id": job_id,
        "text": text,
        "paragraphs": cleaned.get("paragraphs") or [],
        "gaps": cleaned.get("gaps") or [],
        "provenance": pm.to_dict(),
        "honesty_report": honesty,
    }


__all__ = ["generate"]
