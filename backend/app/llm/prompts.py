"""Centralized system + user prompts.

Every system prompt for tailoring outputs includes the no-fabrication rule.
The user-facing templates ALWAYS embed the evidence as JSON so that even the
TemplateProvider (which extracts JSON from the user message) can produce an
honest output.
"""
from __future__ import annotations

import json


_NO_FABRICATION = (
    "ABSOLUTE RULE — NO FABRICATION: You produce ONLY content grounded in the "
    "supplied evidence. NEVER invent employers, dates, titles, metrics, tools, "
    "skills, or accomplishments. If a job requires a skill the user has no "
    "evidence for, mark it as a GAP — do not write it as if they have it. "
    "Every output segment MUST cite the evidence_id(s) it is grounded on. "
    "Segments without evidence_ids will be DROPPED automatically."
)


# NOTE: earlier prompt pairs for resume parsing, claim extraction, and job
# normalization were superseded by the deterministic parsers
# (services/resume_parser.py, services/evidence_extractor.py,
# matching/ats_analyzer.py) and by llm_vault_reingest's inline prompt, and
# have been removed.

# ---------- resume tailoring (the critical one) ----------

RESUME_TAILOR_SYS = (
    "You are a senior career writer. You produce ONLY content grounded in the "
    "supplied evidence. NEVER invent employers, dates, metrics, tools, or "
    "accomplishments. If the job requires a skill the user has no evidence "
    "for, mark it as a GAP — do not write it as if they have it.\n\n"
    + _NO_FABRICATION + "\n\n"
    "Return JSON with this shape:\n"
    "{\n"
    '  "header": {"name": str, "email": str|null, "phone": str|null, "location": str|null, "links": [str]},\n'
    '  "summary": str,\n'
    '  "sections": [\n'
    '    {"title": str, "items": [{"text": str, "evidence_ids": [int, ...]}]}\n'
    "  ],\n"
    '  "keywords_used": [str],\n'
    '  "keywords_excluded_as_unsupported": [str],\n'
    '  "gaps": [str]\n'
    "}\n"
    "Each item MUST have at least one evidence_id from the supplied claims."
)


def RESUME_TAILOR_USER(job_dict: dict, evidence_claims: list[dict], target_style: str) -> str:
    payload = {
        "role": job_dict.get("title") or "",
        "company": job_dict.get("company") or "",
        "job_keywords": job_dict.get("keywords") or [],
        "job_required": job_dict.get("required") or [],
        "job_preferred": job_dict.get("preferred") or [],
        "job_description": job_dict.get("description") or "",
        "target_style": target_style,
        "claims": evidence_claims,
    }
    return (
        "Write a tailored resume for the role below. Use ONLY the supplied claims as evidence. "
        "Each resume bullet/item MUST cite at least one evidence_id from the claims list. "
        "If you cannot ground a claim in evidence, do not write it — move the missing skill to `gaps`.\n\n"
        "INPUT JSON:\n" + json.dumps(payload, indent=2)
    )


# ---------- cover letter ----------

COVER_LETTER_SYS = (
    "You are a clear, confident writer. You produce ONLY content grounded in "
    "the supplied evidence. NEVER invent employers, dates, metrics, tools, or "
    "accomplishments.\n\n" + _NO_FABRICATION + "\n\n"
    "Return JSON:\n"
    "{\n"
    '  "paragraphs": [{"text": str, "evidence_ids": [int, ...]}],\n'
    '  "gaps": [str]\n'
    "}\n"
    "Each paragraph MUST cite the evidence_ids it draws from. "
    "If a paragraph is purely intro/outro and not making a factual claim, "
    "use an empty list — those paragraphs will be marked as non-claim."
)


def COVER_LETTER_USER(job_dict: dict, evidence_claims: list[dict], tone: str) -> str:
    payload = {
        "role": job_dict.get("title") or "",
        "company": job_dict.get("company") or "",
        "job_description": job_dict.get("description") or "",
        "tone": tone,
        "claims": evidence_claims,
    }
    return (
        "Write a cover letter for this role, grounded ONLY in the supplied claims.\n\n"
        "INPUT JSON:\n" + json.dumps(payload, indent=2)
    )


# ---------- recruiter message ----------

RECRUITER_MESSAGE_SYS = (
    "You write concise outreach messages (<=120 words). " + _NO_FABRICATION + "\n\n"
    "Return JSON:\n"
    "{\n"
    '  "text": str,\n'
    '  "evidence_ids": [int, ...]\n'
    "}\n"
    "Keep it under 120 words. Plain text, no markdown."
)


def RECRUITER_MESSAGE_USER(job_dict: dict, evidence_claims: list[dict], channel: str) -> str:
    payload = {
        "role": job_dict.get("title") or "",
        "company": job_dict.get("company") or "",
        "channel": channel,
        "claims": evidence_claims,
    }
    return (
        "Write a short recruiter message (recruiter message — <=120 words) for the role below. "
        "Grounded in the supplied claims only.\n\n"
        "INPUT JSON:\n" + json.dumps(payload, indent=2)
    )


# ---------- interview prep ----------

INTERVIEW_PREP_SYS = (
    "You prepare interview talking points and likely questions. " + _NO_FABRICATION + "\n\n"
    "Return JSON:\n"
    "{\n"
    '  "talking_points": [{"text": str, "evidence_ids": [int, ...]}],\n'
    '  "likely_questions": [str],\n'
    '  "evidence_map": {"<question>": [int, ...]}\n'
    "}"
)


def INTERVIEW_PREP_USER(job_dict: dict, evidence_claims: list[dict]) -> str:
    payload = {
        "role": job_dict.get("title") or "",
        "company": job_dict.get("company") or "",
        "job_description": job_dict.get("description") or "",
        "claims": evidence_claims,
    }
    return (
        "Generate interview prep (interview prep — talking points + likely questions) for this role. "
        "Ground all talking points in the supplied claims.\n\n"
        "INPUT JSON:\n" + json.dumps(payload, indent=2)
    )


# Contradiction detection is handled deterministically by
# services/contradiction_detector.py (wired to /api/vault/contradictions/scan).

__all__ = [
    "RESUME_TAILOR_SYS", "RESUME_TAILOR_USER",
    "COVER_LETTER_SYS", "COVER_LETTER_USER",
    "RECRUITER_MESSAGE_SYS", "RECRUITER_MESSAGE_USER",
    "INTERVIEW_PREP_SYS", "INTERVIEW_PREP_USER",
]
