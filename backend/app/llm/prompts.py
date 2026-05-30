"""Centralized system + user prompts.

Every system prompt for tailoring outputs includes the no-fabrication rule.
The user-facing templates ALWAYS embed the evidence as JSON so that even the
TemplateProvider (which extracts JSON from the user message) can produce an
honest output.
"""
from __future__ import annotations

import json
from typing import Any


_NO_FABRICATION = (
    "ABSOLUTE RULE — NO FABRICATION: You produce ONLY content grounded in the "
    "supplied evidence. NEVER invent employers, dates, titles, metrics, tools, "
    "skills, or accomplishments. If a job requires a skill the user has no "
    "evidence for, mark it as a GAP — do not write it as if they have it. "
    "Every output segment MUST cite the evidence_id(s) it is grounded on. "
    "Segments without evidence_ids will be DROPPED automatically."
)


# ---------- resume parsing ----------

RESUME_PARSE_SYS = (
    "You parse resumes into structured JSON. Return ONLY what is present in "
    "the source text — do not infer titles, dates, or employers that are not "
    "explicitly stated. Use null for unknown fields."
)


def RESUME_PARSE_USER(text: str) -> str:
    return (
        "Parse the resume below into JSON with this shape:\n"
        "{\n"
        '  "header": {"name": str|null, "email": str|null, "phone": str|null, '
        '"location": str|null, "links": [str]},\n'
        '  "summary": str|null,\n'
        '  "experience": [{"title": str, "company": str, "start": str, '
        '"end": str|null, "location": str|null, "bullets": [str]}],\n'
        '  "education": [{"degree": str, "school": str, "year": str|null}],\n'
        '  "skills": [str],\n'
        '  "projects": [{"name": str, "description": str|null, "bullets": [str]}],\n'
        '  "certifications": [str]\n'
        "}\n\n"
        "RESUME TEXT:\n" + (text or "")
    )


# ---------- claim extraction ----------

CLAIM_EXTRACT_SYS = (
    "You extract atomic, verifiable career claims from source text. Each "
    "claim is one specific assertion (a skill used, a project shipped, a "
    "metric achieved, a tool wielded). Do not invent claims — only extract "
    "what is literally stated. Mark claim_type and a confidence score."
)


def CLAIM_EXTRACT_USER(source_text: str, source_type: str) -> str:
    return (
        f"Source type: {source_type}\n\n"
        "Return JSON: {\n"
        '  "claims": [{\n'
        '    "claim_type": "skill"|"project"|"achievement"|"tool"|"role"|"education"|"certification"|"metric",\n'
        '    "claim_text": str,\n'
        '    "normalized_claim": str,\n'
        '    "employer": str|null, "project": str|null, "skill": str|null, '
        '"tool": str|null, "date_start": str|null, "date_end": str|null,\n'
        '    "confidence": 0.0-1.0,\n'
        '    "evidence_strength": "strong"|"medium"|"weak"\n'
        "  }]\n"
        "}\n\n"
        "SOURCE TEXT:\n" + (source_text or "")
    )


# ---------- job description normalization ----------

JOB_NORMALIZE_SYS = (
    "You normalize raw job descriptions into structured JSON. Extract only "
    "what is explicitly stated; do not infer."
)


def JOB_NORMALIZE_USER(description: str) -> str:
    return (
        "Normalize this job description to JSON:\n"
        "{\n"
        '  "keywords": [str],         // important terms (technologies, methods, domain words)\n'
        '  "required": [str],         // hard requirements\n'
        '  "preferred": [str],        // nice-to-haves\n'
        '  "seniority": str,          // e.g. junior/mid/senior/staff/principal\n'
        '  "responsibilities": [str]\n'
        "}\n\n"
        "JOB DESCRIPTION:\n" + (description or "")
    )


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


# ---------- contradiction detection ----------

CONTRADICTION_DETECT_SYS = (
    "You detect contradictions across a candidate's career claims (date overlaps, "
    "title mismatches, conflicting tools/skills at the same period). Report only "
    "literal contradictions, not stylistic differences."
)


def CONTRADICTION_DETECT_USER(claims: list[dict]) -> str:
    return (
        "Find contradictions among the claims below. Return JSON:\n"
        "{\n"
        '  "contradictions": [{\n'
        '    "claim_ids": [int, int],\n'
        '    "kind": "date_overlap"|"title_mismatch"|"tool_conflict"|"other",\n'
        '    "explanation": str\n'
        "  }]\n"
        "}\n\n"
        "CLAIMS:\n" + json.dumps(claims, indent=2)
    )


__all__ = [
    "RESUME_PARSE_SYS", "RESUME_PARSE_USER",
    "CLAIM_EXTRACT_SYS", "CLAIM_EXTRACT_USER",
    "JOB_NORMALIZE_SYS", "JOB_NORMALIZE_USER",
    "RESUME_TAILOR_SYS", "RESUME_TAILOR_USER",
    "COVER_LETTER_SYS", "COVER_LETTER_USER",
    "RECRUITER_MESSAGE_SYS", "RECRUITER_MESSAGE_USER",
    "INTERVIEW_PREP_SYS", "INTERVIEW_PREP_USER",
    "CONTRADICTION_DETECT_SYS", "CONTRADICTION_DETECT_USER",
]
