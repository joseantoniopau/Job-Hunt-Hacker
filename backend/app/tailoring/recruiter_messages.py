"""Generate a short (<=120 word) recruiter outreach message."""
from __future__ import annotations

import logging
from typing import Any

from ..db import audit, get_conn, row_to_dict
from ..llm import get_llm
from ..llm import guardrails
from ..llm.prompts import RECRUITER_MESSAGE_SYS, RECRUITER_MESSAGE_USER
from .honesty_report import build_report
from .provenance import ProvenanceMap
from .resume_tailor import _coerce_claims_for_prompt, _safe_retrieve_claims, _user_header_from_profile

log = logging.getLogger("jhh.tailoring.recruiter")


def _load_job(job_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM job_posting WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"job_posting id={job_id} not found")
    return row_to_dict(row) or {}


def _trim_to_120_words(text: str) -> str:
    words = (text or "").split()
    if len(words) <= 120:
        return text
    return " ".join(words[:120]).rstrip(",.") + "…"


def _deterministic_msg(job: dict, claims: list[dict], channel: str) -> dict:
    header = _user_header_from_profile()
    name = header.get("name") or "there"
    role = job.get("title") or "the open role"
    company = job.get("company") or "your team"
    ids: list[int] = []
    highlight = ""
    for c in claims[:1]:
        highlight = c.get("claim_text") or ""
        ids.append(c["id"])
    body = (
        f"Hi — I came across the {role} listing at {company} and think my background is a strong fit. "
        + (f"Most recently: {highlight}. " if highlight else "")
        + "Open to a brief intro call this week? Thanks."
    )
    return {"text": _trim_to_120_words(body), "evidence_ids": ids}


def generate(job_id: int, channel: str = "email") -> dict:
    job = _load_job(job_id)
    raw_claims = _safe_retrieve_claims(
        " ".join([job.get("title") or "", job.get("company") or "", job.get("description") or ""]),
        top=8,
    )
    claims = _coerce_claims_for_prompt(raw_claims)
    allowed_ids: set[int] = {c["id"] for c in claims}

    llm = get_llm()
    sys = RECRUITER_MESSAGE_SYS
    user = RECRUITER_MESSAGE_USER(
        {"title": job.get("title"), "company": job.get("company"), "description": job.get("description")},
        claims,
        channel,
    )

    structured: dict[str, Any] = {}
    try:
        structured = llm.complete_json(sys, user, max_tokens=600) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("LLM recruiter message failed: %s", e)
        structured = {}

    if not isinstance(structured, dict) or not structured.get("text"):
        structured = _deterministic_msg(job, claims, channel)

    cleaned = guardrails.validate_provenance(structured, allowed_ids)
    dropped = (cleaned.get("honesty_report") or {}).get("dropped_segments") or []

    pm = ProvenanceMap()
    pm.link("message", cleaned.get("evidence_ids") or [])

    cleaned["text"] = _trim_to_120_words(cleaned.get("text") or "")

    honesty = build_report(
        provenance=pm,
        keyword_matrix=[],
        gaps_flagged=[],
        dropped_segments=dropped,
    )

    audit("recruiter_message_generated", "job_posting", job_id, channel=channel,
          provider=getattr(llm, "name", "unknown"))

    return {
        "job_id": job_id,
        "channel": channel,
        "text": cleaned.get("text") or "",
        "evidence_ids": cleaned.get("evidence_ids") or [],
        "provenance": pm.to_dict(),
        "honesty_report": honesty,
    }


__all__ = ["generate"]
