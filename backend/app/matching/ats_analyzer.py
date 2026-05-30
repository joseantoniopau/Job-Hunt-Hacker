"""Build the full Keyword Matrix for a job posting.

Output:
  {
    keywords: [{keyword, category, importance, support_status,
                resume_safe, evidence_claim_ids}],
    coverage: {supported, transferable, weak, unsupported},
    ats_risk: low | medium | high,
    suggestions: [str, ...]
  }
"""
from __future__ import annotations

import re
from typing import Iterable

from .keyword_classifier import classify_keywords
from .skills_extractor import categorize_skill, extract_skills

# Category mapping — refined buckets used in the keyword matrix UI.
_CATEGORY_MAP = {
    "programming_languages": "required_skill",
    "frontend": "required_skill",
    "backend": "required_skill",
    "mobile": "required_skill",
    "ai_ml": "required_skill",
    "data": "platform",
    "cloud": "platform",
    "devops": "tool",
    "security": "compliance",
    "methodology": "methodology",
    "leadership": "leadership",
    "product": "domain",
    "soft_skills": "soft_skill",
}

# Importance heuristics
_REQUIRED_RX = re.compile(
    r"\b(required|requirements?|must[\s-]?have|minimum|essential|need[s]?\s+to\s+have)\b",
    re.I,
)
_PREFERRED_RX = re.compile(
    r"\b(preferred|nice[\s-]?to[\s-]?have|bonus|plus|ideally|huge\s+plus|a\s+plus)\b",
    re.I,
)


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Roughly split a job description into sections like
    'Requirements: ...' / 'Preferred: ...' / general body. Returns a list
    of (importance_label, chunk_text). Importance label is 'required',
    'preferred', or 'nice' (nice = unmarked body).
    """
    if not text:
        return []
    # naive bullet/paragraph chunking
    chunks = re.split(r"\n{2,}|\r\n{2,}|(?<=[.;:])\s*\n", text)
    sections: list[tuple[str, str]] = []
    current_label = "nice"
    for ch in chunks:
        s = ch.strip()
        if not s:
            continue
        first_line = s.split("\n", 1)[0].lower()
        if _REQUIRED_RX.search(first_line) and len(first_line) < 200:
            current_label = "required"
        elif _PREFERRED_RX.search(first_line) and len(first_line) < 200:
            current_label = "preferred"
        # if a sentence contains the markers inline, still tag the chunk
        local_label = current_label
        if _REQUIRED_RX.search(s) and not _PREFERRED_RX.search(s):
            local_label = "required"
        elif _PREFERRED_RX.search(s) and not _REQUIRED_RX.search(s):
            local_label = "preferred"
        sections.append((local_label, s))
    return sections


def _importance_for_keyword(keyword: str, sections: list[tuple[str, str]]) -> str:
    """Decide importance based on which section the keyword first appears in."""
    kw_lower = keyword.lower()
    # Order priority: required > preferred > nice
    best = "nice"
    for label, chunk in sections:
        if kw_lower in chunk.lower():
            if label == "required":
                return "required"
            if label == "preferred" and best == "nice":
                best = "preferred"
    return best


def _refined_category(keyword: str) -> str:
    cat = categorize_skill(keyword)
    return _CATEGORY_MAP.get(cat, "required_skill")


def _job_text(job_record: dict) -> str:
    if not job_record:
        return ""
    parts: list[str] = []
    for key in ("title", "description", "requirements", "benefits"):
        v = job_record.get(key)
        if isinstance(v, list):
            parts.append(" \n ".join(str(x) for x in v if x))
        elif v:
            parts.append(str(v))
    return "\n\n".join(parts)


def _ats_risk(coverage: dict, importance_counts: dict) -> str:
    required_total = importance_counts.get("required", 0)
    if required_total == 0:
        # Fall back to overall ratios when no required signals were detected
        total = sum(coverage.values()) or 1
        unsupported_ratio = coverage.get("unsupported", 0) / total
        if unsupported_ratio >= 0.5:
            return "high"
        if unsupported_ratio >= 0.25:
            return "medium"
        return "low"
    unsupported_required = importance_counts.get("required_unsupported", 0)
    ratio = unsupported_required / required_total
    if ratio >= 0.4:
        return "high"
    if ratio >= 0.15:
        return "medium"
    return "low"


def _suggestions(entries: list[dict]) -> list[str]:
    suggestions: list[str] = []
    for e in entries:
        if e["support_status"] == "unsupported" and e["importance"] == "required":
            suggestions.append(
                f"Mention {e['keyword']} if you have hands-on experience — currently no evidence in your vault."
            )
    # cap to avoid runaway lists
    return suggestions[:8]


def analyze_job(job_record: dict, evidence_claims: list[dict]) -> dict:
    """Build the keyword matrix for a single job."""
    job_record = job_record or {}
    claims = evidence_claims or []

    text = _job_text(job_record)
    sections = _split_sections(text)
    keywords = extract_skills(text)

    classified = classify_keywords(keywords, claims)

    coverage = {"supported": 0, "transferable": 0, "weak": 0, "unsupported": 0}
    importance_counts = {"required": 0, "required_unsupported": 0}

    entries: list[dict] = []
    for c in classified:
        imp = _importance_for_keyword(c["keyword"], sections)
        cat = _refined_category(c["keyword"])
        status = c["support_status"]
        if status == "supported":
            coverage["supported"] += 1
        elif status == "transferable":
            coverage["transferable"] += 1
        elif status == "weak_evidence":
            coverage["weak"] += 1
        else:
            coverage["unsupported"] += 1
        if imp == "required":
            importance_counts["required"] += 1
            if status == "unsupported":
                importance_counts["required_unsupported"] += 1
        entries.append({
            "keyword": c["keyword"],
            "category": cat,
            "importance": imp,
            "support_status": status,
            "resume_safe": c["resume_safe"],
            "evidence_claim_ids": c["evidence_claim_ids"],
        })

    return {
        "keywords": entries,
        "coverage": coverage,
        "ats_risk": _ats_risk(coverage, importance_counts),
        "suggestions": _suggestions(entries),
    }
