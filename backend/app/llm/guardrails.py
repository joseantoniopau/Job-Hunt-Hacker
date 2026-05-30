"""Runtime enforcement of the no-fabrication rule.

The LLM is supposed to cite evidence_ids on every segment. The guardrails
layer is what ensures it happens even when the LLM forgets, lies, or
hallucinates. The pipeline:

  1. validate_provenance() — drops any segment whose evidence_ids is empty
     or contains IDs not in the allowed set.
  2. assert_no_fabrication() — flags suspicious n-grams that don't appear
     in the supplied evidence text.
  3. enforce_keyword_safety() — splits used-keywords into safe/unsafe based
     on the ATS keyword matrix's `resume_safe` flag.

Nothing here ever raises; the caller decides whether to drop or warn.
"""
from __future__ import annotations

import copy
import logging
import re
from typing import Any, Iterable

from ..utils.text import keyword_tokens, normalize

log = logging.getLogger("jhh.llm.guardrails")


# Common English stopwords + verbs we don't want to flag as "fabricated phrases".
_STOPWORDS: set[str] = {
    "the", "a", "an", "and", "or", "but", "of", "for", "to", "in", "on", "at",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "been",
    "being", "this", "that", "these", "those", "it", "its", "i", "you", "we",
    "they", "he", "she", "my", "our", "your", "their", "his", "her", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "can", "may", "might", "must", "shall", "not", "no", "yes", "so", "if",
    "than", "then", "such", "into", "out", "over", "under", "about", "after",
    "before", "while", "during", "between", "across", "per", "via", "also",
    "more", "less", "most", "least", "very", "much", "many", "few", "some",
    "all", "any", "each", "every", "other", "another", "same", "new", "old",
    "first", "second", "third", "last", "next", "previous", "good", "great",
    "best", "better", "well", "still", "just", "only", "even", "ever", "never",
    "always", "often", "sometimes", "usually", "team", "teams", "work", "works",
    "worked", "working", "role", "roles", "company", "companies", "experience",
    "year", "years", "month", "months",
}


# ---------- provenance validation ----------

def _coerce_id_list(val: Any) -> list[int]:
    if val is None:
        return []
    if isinstance(val, (int,)):
        return [val]
    if isinstance(val, str):
        try:
            return [int(val)]
        except Exception:
            return []
    if isinstance(val, (list, tuple, set)):
        out: list[int] = []
        for v in val:
            try:
                out.append(int(v))
            except Exception:
                continue
        return out
    return []


def validate_provenance(output: dict, evidence_ids_allowed: set[int] | Iterable[int]) -> dict:
    """Walk a tailored output, dropping any segment whose evidence_ids is
    empty or references unknown IDs. Mutates a copy; returns it.

    The output may be a tailored resume, cover letter, recruiter message, or
    interview prep — we walk known shapes plus a generic fallback.

    Dropped segments are stored in ``output["honesty_report"]["dropped_segments"]``.
    """
    if not isinstance(output, dict):
        return {"honesty_report": {"dropped_segments": [], "error": "non-dict output"}}

    allowed = set(int(x) for x in evidence_ids_allowed)
    out = copy.deepcopy(output)
    dropped: list[dict] = []

    def _check(segment: dict, where: str) -> bool:
        ids = _coerce_id_list(segment.get("evidence_ids"))
        # filter to only allowed
        clean = [i for i in ids if i in allowed]
        segment["evidence_ids"] = clean
        if not clean:
            dropped.append({
                "where": where,
                "text": (segment.get("text") or "")[:240],
                "reason": "no_valid_evidence_ids",
                "raw_evidence_ids": ids,
            })
            return False
        return True

    # resume-style: sections -> items
    if isinstance(out.get("sections"), list):
        for s_idx, sec in enumerate(out["sections"]):
            if not isinstance(sec, dict):
                continue
            items = sec.get("items") or []
            kept = []
            for i_idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                if _check(item, f"sections[{s_idx}].items[{i_idx}]"):
                    kept.append(item)
            sec["items"] = kept

    # cover-letter style: paragraphs[]
    if isinstance(out.get("paragraphs"), list):
        kept = []
        for p_idx, para in enumerate(out["paragraphs"]):
            if not isinstance(para, dict):
                continue
            # Allow empty-evidence paragraphs ONLY if they're non-claim (intro/outro).
            # We treat first and last paragraphs as soft-exempt: keep but mark.
            ids = _coerce_id_list(para.get("evidence_ids"))
            clean = [i for i in ids if i in allowed]
            para["evidence_ids"] = clean
            is_edge = (p_idx == 0 or p_idx == len(out["paragraphs"]) - 1)
            if not clean and not is_edge:
                dropped.append({
                    "where": f"paragraphs[{p_idx}]",
                    "text": (para.get("text") or "")[:240],
                    "reason": "no_valid_evidence_ids",
                    "raw_evidence_ids": ids,
                })
                continue
            kept.append(para)
        out["paragraphs"] = kept

    # interview-prep style: talking_points[]
    if isinstance(out.get("talking_points"), list):
        kept = []
        for t_idx, tp in enumerate(out["talking_points"]):
            if not isinstance(tp, dict):
                continue
            if _check(tp, f"talking_points[{t_idx}]"):
                kept.append(tp)
        out["talking_points"] = kept

    # recruiter-message style: single {text, evidence_ids}
    if "text" in out and "evidence_ids" in out and not isinstance(out.get("sections"), list):
        ids = _coerce_id_list(out.get("evidence_ids"))
        clean = [i for i in ids if i in allowed]
        out["evidence_ids"] = clean
        if not clean:
            dropped.append({
                "where": "root",
                "text": (out.get("text") or "")[:240],
                "reason": "no_valid_evidence_ids",
                "raw_evidence_ids": ids,
            })
            # don't blank the text — caller might want to fall back

    report = out.setdefault("honesty_report", {})
    if not isinstance(report, dict):
        report = {}
        out["honesty_report"] = report
    existing = report.get("dropped_segments") or []
    if not isinstance(existing, list):
        existing = []
    report["dropped_segments"] = existing + dropped
    return out


# ---------- fabrication detection (n-gram check) ----------

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]*")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _ngrams(tokens: list[str], n: int) -> list[tuple[str, ...]]:
    if len(tokens) < n:
        return []
    return [tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def assert_no_fabrication(text: str, evidence_texts: list[str]) -> list[str]:
    """Return n-grams (length 3-5) appearing in ``text`` that don't appear in
    any evidence text and aren't dominated by common English stopwords.

    These are *suspect phrases* — the caller decides whether to drop the
    containing segment or merely warn.
    """
    if not text:
        return []
    ev_norm = " ".join(normalize(e) for e in (evidence_texts or []))
    if not ev_norm.strip():
        # No evidence to compare against; we can't credibly call anything fabricated.
        return []

    text_tokens = _tokens(text)
    if not text_tokens:
        return []

    suspect: list[str] = []
    seen: set[tuple[str, ...]] = set()

    for n in (5, 4, 3):  # prefer longer matches first
        for gram in _ngrams(text_tokens, n):
            if gram in seen:
                continue
            content = [t for t in gram if t not in _STOPWORDS]
            if len(content) < max(2, n - 1):
                # mostly stopwords — skip
                continue
            phrase = " ".join(gram)
            if phrase in ev_norm:
                continue
            # also tolerate when each content token appears somewhere in ev_norm
            if all(t in ev_norm for t in content):
                continue
            suspect.append(phrase)
            seen.add(gram)

    return suspect


# ---------- keyword safety ----------

def enforce_keyword_safety(
    keywords_used: list[str],
    keyword_matrix: list[dict],
) -> tuple[list[str], list[str]]:
    """Split ``keywords_used`` into (safe, unsafe) using the ATS keyword matrix.

    The matrix is expected to be a list of dicts shaped like::

        {"keyword": "kubernetes", "resume_safe": True, "evidence_ids": [...], ...}

    Any keyword not present in the matrix is treated as unsafe (we have no
    evidence basis for it).
    """
    if not keywords_used:
        return [], []
    matrix_by_kw: dict[str, dict] = {}
    for entry in keyword_matrix or []:
        if not isinstance(entry, dict):
            continue
        kw = (entry.get("keyword") or entry.get("term") or "").strip().lower()
        if kw:
            matrix_by_kw[kw] = entry

    safe: list[str] = []
    unsafe: list[str] = []
    for raw in keywords_used:
        kw = (raw or "").strip()
        if not kw:
            continue
        entry = matrix_by_kw.get(kw.lower())
        if entry is None:
            unsafe.append(kw)
            continue
        # any of these flags signal "evidence-backed"
        flag = entry.get("resume_safe")
        if flag is None:
            flag = entry.get("safe")
        if flag is None:
            # if it has evidence_ids, treat as safe
            flag = bool(entry.get("evidence_ids"))
        if flag:
            safe.append(kw)
        else:
            unsafe.append(kw)
    return safe, unsafe


__all__ = [
    "validate_provenance",
    "assert_no_fabrication",
    "enforce_keyword_safety",
]
