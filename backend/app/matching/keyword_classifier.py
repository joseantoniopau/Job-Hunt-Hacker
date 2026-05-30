"""Classify each job keyword by the evidence we can honestly claim for it.

Categories:
  supported       — at least one claim text directly mentions the keyword
  transferable    — semantic vector match >= 0.55 against a claim
  weak_evidence   — keyword appears only inside a long claim description
                    without being the focus skill/tool field
  unsupported     — no evidence at all

resume_safe = supported OR transferable
"""
from __future__ import annotations

import re
from typing import Iterable

from ..utils.text import normalize

try:
    from ..services import vector_store  # type: ignore
except Exception:  # pragma: no cover - defensive
    vector_store = None  # type: ignore

# match a single keyword as a whole-word substring of a claim
def _kw_regex(kw: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])", re.I)


def _focus_field_match(kw_lower: str, claim: dict) -> bool:
    for field in ("skill", "tool", "project", "employer"):
        v = (claim.get(field) or "")
        if not v:
            continue
        if kw_lower == str(v).strip().lower():
            return True
    return False


def _text_match(kw: str, claim: dict) -> bool:
    rx = _kw_regex(kw)
    for field in ("normalized_claim", "claim_text"):
        text = claim.get(field) or ""
        if text and rx.search(text):
            return True
    return False


def _vector_lookup(kw: str, top: int = 5) -> list[dict]:
    if vector_store is None:
        return []
    try:
        return vector_store.search(kw, owner_type="claim", top=top)
    except Exception:
        return []


def classify_keywords(
    job_keywords: list[str],
    evidence_claims: list[dict],
    transferable_threshold: float = 0.55,
) -> list[dict]:
    """Return one classification entry per keyword.

    Each entry: {keyword, category, support_status, evidence_claim_ids, resume_safe}
    `category` is just a label (kept for downstream UIs); here we use 'skill' as
    the default since ats_analyzer assigns a richer category. Tests for this
    module focus on `support_status`.
    """
    out: list[dict] = []
    claims = evidence_claims or []

    for raw_kw in job_keywords or []:
        kw = (raw_kw or "").strip()
        if not kw:
            continue
        kw_lower = kw.lower()

        supporting_ids: list[int] = []
        weak_ids: list[int] = []

        for c in claims:
            if not c:
                continue
            if _focus_field_match(kw_lower, c):
                cid = c.get("id")
                if cid and cid not in supporting_ids:
                    supporting_ids.append(int(cid))
                continue
            if _text_match(kw, c):
                cid = c.get("id")
                # if it appears in a long claim_text but not as focus field,
                # treat as weak unless the text is short (then trust it).
                text_len = len((c.get("normalized_claim") or c.get("claim_text") or ""))
                if text_len <= 180:
                    if cid and cid not in supporting_ids:
                        supporting_ids.append(int(cid))
                else:
                    if cid and cid not in weak_ids:
                        weak_ids.append(int(cid))

        status: str
        evidence_ids: list[int]
        if supporting_ids:
            status = "supported"
            evidence_ids = supporting_ids
        else:
            # try transferable via vector search over claims
            transferable_ids: list[int] = []
            for hit in _vector_lookup(kw):
                if hit.get("owner_type") != "claim":
                    continue
                score = float(hit.get("score") or 0)
                if score >= transferable_threshold:
                    oid = hit.get("owner_id")
                    if oid and oid not in transferable_ids:
                        transferable_ids.append(int(oid))
            if transferable_ids:
                status = "transferable"
                evidence_ids = transferable_ids[:5]
            elif weak_ids:
                status = "weak_evidence"
                evidence_ids = weak_ids
            else:
                status = "unsupported"
                evidence_ids = []

        out.append({
            "keyword": kw,
            "category": "skill",
            "support_status": status,
            "evidence_claim_ids": evidence_ids,
            "resume_safe": status in ("supported", "transferable"),
        })

    return out
