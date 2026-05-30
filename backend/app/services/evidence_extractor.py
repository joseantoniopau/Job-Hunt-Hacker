"""Extract CareerClaim dicts from a piece of source text.

Hybrid pipeline:
  1. Rule-based pass produces a baseline set of candidate claims (any of:
     role, accomplishment, skill, tool, certification, degree, project,
     publication, metric, responsibility, leadership).
  2. Optional LLM pass refines the set with the same schema; LLM output is
     accepted only when its `claim_text` is supported by the source text.
  3. Confidence is composed: rule = 0.6, LLM-only = 0.8, agreed = 0.95.

Every claim has the fields required by `career_claim`:
  source_id, claim_type, claim_text, normalized_claim,
  date_start?, date_end?, employer?, project?, skill?, tool?,
  confidence, evidence_strength, user_verified, allowed_for_resume,
  contradiction_status.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..config import settings
from ..llm import get_llm
from ..utils.text import dedupe_preserve_order, keyword_tokens, normalize

log = logging.getLogger("jhh.evidence")

CLAIM_TYPES = (
    "role", "accomplishment", "skill", "tool", "certification",
    "degree", "project", "publication", "metric", "responsibility",
    "leadership",
)

MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_TOKEN = rf"(?:(?:\d{{1,2}}/\d{{4}})|(?:(?:{MONTHS})[a-z]*\.?\s+\d{{4}})|(?:\d{{4}}))"
DATE_RANGE_RE = re.compile(
    rf"({DATE_TOKEN})\s*[-–—to]+\s*({DATE_TOKEN}|present|current|now)",
    re.I,
)

ROLE_AT_RE = re.compile(
    r"\b([A-Z][A-Za-z./& \-]{2,60}?)\s+at\s+([A-Z][A-Za-z0-9./& ,\-]{1,60})",
)

METRIC_RE = re.compile(
    r"\b(?:\$\d[\d,\.]*\s*[KkMmBb]?|"
    r"\d[\d,\.]*\s*(?:%|percent|x|hours?|days?|weeks?|months?|years?|"
    r"users?|customers?|requests?|qps|rps|tps|reqs|engineers?|reports?|"
    r"k|m|b))\b",
    re.I,
)

ACCOMPLISHMENT_VERBS = {
    "shipped", "launched", "built", "designed", "delivered", "improved",
    "increased", "decreased", "reduced", "saved", "grew", "scaled",
    "led", "drove", "owned", "implemented", "migrated", "automated",
    "rewrote", "refactored", "introduced", "established", "negotiated",
    "managed", "mentored", "hired", "founded", "spearheaded", "championed",
    "architected", "optimized", "released", "deployed",
}

LEADERSHIP_HINTS = {
    "led", "managed", "mentored", "directed", "supervised", "oversaw",
    "headed", "founded", "co-founded", "chair", "chaired", "vp",
    "head of", "director of", "manager of",
}

CERT_HINTS = [
    "certified", "certification", "certificate",
    "aws certified", "gcp certified", "azure certified",
    "pmp", "cisa", "cissp", "cfa", "scrum master", "psm", "csm",
    "kubernetes administrator", "cka", "ckad",
]

DEGREE_HINTS = [
    "bachelor", "b.s.", "b.s ", "bs ", "ba ", "b.a.",
    "master", "m.s.", "ms ", "ma ", "m.a.", "mba",
    "ph.d", "phd", "doctorate", "doctoral",
    "associate of", "associate's", "associates ",
]

PUBLICATION_HINTS = [
    r"\bpublished\b", r"\bauthor of\b", r"\bco-author\b",
    r"\bjournal\b", r"\bconference\b",
    r"\bIEEE\b", r"\bACM\b", r"\barXiv\b", r"\bproceedings\b",
]

ROLE_TITLE_HINTS = [
    "engineer", "developer", "manager", "director", "lead", "architect",
    "designer", "analyst", "scientist", "researcher", "consultant",
    "specialist", "associate", "intern", "founder", "co-founder",
    "ceo", "cto", "coo", "cfo", "vp", "head of", "chief",
    "product manager", "program manager", "project manager",
    "pm", "swe", "sre", "tpm", "advisor",
]

_ROLE_HINT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(h) for h in ROLE_TITLE_HINTS) + r")\b",
    re.I,
)


def _walk_for_keywords(node: Any, out: set[str]) -> None:
    if isinstance(node, str):
        s = node.strip().lower()
        if s and 1 <= len(s) <= 60:
            out.add(s)
    elif isinstance(node, list):
        for item in node:
            _walk_for_keywords(item, out)
    elif isinstance(node, dict):
        for v in node.values():
            _walk_for_keywords(v, out)


def _load_seed_keywords() -> set[str]:
    paths = [
        settings.data_dir / "seed" / "ats_keywords.json",
        Path(__file__).resolve().parents[3] / "data" / "seed" / "ats_keywords.json",
    ]
    for p in paths:
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                out: set[str] = set()
                _walk_for_keywords(data, out)
                # Use top-level keys too (e.g. "Python" canonical names)
                if isinstance(data, dict):
                    _walk_for_keywords(list(data.keys()), out)
                if out:
                    return out
        except Exception as e:  # noqa: BLE001
            log.debug("seed keyword load failed: %s", e)
    # Fallback baseline list — small but real.
    return {
        "python", "java", "javascript", "typescript", "go", "rust", "c++",
        "c#", "ruby", "php", "scala", "kotlin", "swift", "sql", "bash",
        "react", "angular", "vue", "next.js", "node.js", "django", "flask",
        "fastapi", "spring", "rails", "express",
        "aws", "gcp", "azure", "kubernetes", "docker", "terraform", "ansible",
        "postgres", "postgresql", "mysql", "mongodb", "redis", "kafka",
        "snowflake", "bigquery", "airflow", "spark", "hadoop", "dbt",
        "tensorflow", "pytorch", "scikit-learn", "pandas", "numpy",
        "git", "github", "gitlab", "jira", "confluence", "linux", "macos",
        "ci/cd", "graphql", "rest", "grpc", "microservices",
        "agile", "scrum", "kanban",
    }


_SEED_KEYWORDS = _load_seed_keywords()


# ---------- helpers ----------

def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])|\n+", text)
    return [s.strip() for s in parts if s and s.strip()]


def _windows(text: str, size: int = 200, step: int = 100) -> list[str]:
    text = (text or "").lower()
    if not text:
        return []
    out = []
    for i in range(0, max(1, len(text) - size + 1), step):
        out.append(text[i:i + size])
    if out and not text.endswith(out[-1]):
        out.append(text[-size:])
    return out


def _supported(claim_text: str, source_text: str) -> bool:
    """Claim must share at least 50% of significant tokens with some 200-char
    window of the source text."""
    if not claim_text or not source_text:
        return False
    src_lower = source_text.lower()
    cleaned = claim_text.lower().strip()
    # Short claims: substring check (handles "aws", "gcp", "ci/cd" etc.)
    if len(cleaned) <= 12 and cleaned in src_lower:
        return True
    claim_tokens = [t.strip(".,;:!?()[]") for t in keyword_tokens(claim_text)]
    claim_tokens = [t for t in claim_tokens if len(t) >= 3]
    if not claim_tokens:
        return cleaned in src_lower
    claim_set = set(claim_tokens)
    threshold = max(1, int(len(claim_set) * 0.5))
    for win in _windows(source_text):
        win_tokens = {t.strip(".,;:!?()[]") for t in keyword_tokens(win)}
        overlap = len(claim_set & win_tokens)
        if overlap >= threshold:
            return True
    return False


def _strength_for(claim_text: str) -> str:
    has_metric = bool(METRIC_RE.search(claim_text))
    has_date = bool(DATE_RANGE_RE.search(claim_text))
    if has_metric and has_date:
        return "strong"
    if has_metric or has_date:
        return "medium"
    return "weak"


def _mk_claim(source_id: int, claim_type: str, claim_text: str, *,
              confidence: float = 0.6, **extras: Any) -> dict[str, Any]:
    claim_text = re.sub(r"\s+", " ", (claim_text or "").strip())
    return {
        "source_id": source_id,
        "claim_type": claim_type,
        "claim_text": claim_text,
        "normalized_claim": normalize(claim_text),
        "date_start": extras.get("date_start"),
        "date_end": extras.get("date_end"),
        "employer": extras.get("employer"),
        "project": extras.get("project"),
        "skill": extras.get("skill"),
        "tool": extras.get("tool"),
        "confidence": round(float(confidence), 3),
        "evidence_strength": extras.get("evidence_strength") or _strength_for(claim_text),
        "user_verified": False,
        "allowed_for_resume": True,
        "contradiction_status": "none",
    }


# ---------- rule extractors ----------

def _extract_roles(source_id: int, text: str) -> list[dict]:
    out: list[dict] = []
    for sentence in _sentences(text):
        for m in ROLE_AT_RE.finditer(sentence):
            title = m.group(1).strip().rstrip(",")
            company = m.group(2).strip().rstrip(",.")
            # heuristic guard: title must look like a job title
            if not _ROLE_HINT_RE.search(title):
                continue
            # trim trailing date tokens from company
            company = re.sub(rf"\s+{DATE_TOKEN}.*$", "", company).strip().rstrip(",.")
            dr = DATE_RANGE_RE.search(sentence)
            ds = de = None
            if dr:
                ds, de = dr.group(1), dr.group(2)
            out.append(_mk_claim(
                source_id, "role",
                f"{title} at {company}" + (f" ({ds} - {de})" if ds else ""),
                confidence=0.6, employer=company,
                date_start=ds, date_end=de,
            ))
    return out


def _extract_skills(source_id: int, text: str) -> list[dict]:
    found: set[str] = set()
    lower = (text or "").lower()
    for kw in _SEED_KEYWORDS:
        pat = r"\b" + re.escape(kw) + r"\b"
        if re.search(pat, lower):
            found.add(kw)
    out: list[dict] = []
    for kw in sorted(found):
        out.append(_mk_claim(
            source_id, "skill", kw,
            confidence=0.6, skill=kw, evidence_strength="medium",
        ))
    return out


def _extract_accomplishments(source_id: int, text: str) -> list[dict]:
    out: list[dict] = []
    for sentence in _sentences(text):
        s = sentence.strip()
        if not s or len(s) < 20:
            continue
        first_words = re.split(r"\W+", s.lower())[:4]
        verbs_in = {w for w in first_words if w in ACCOMPLISHMENT_VERBS}
        # Also catch bullets that start with verb
        if not verbs_in:
            # check first word only
            fw = first_words[0] if first_words else ""
            if fw not in ACCOMPLISHMENT_VERBS:
                continue
        if METRIC_RE.search(s):
            ctype = "metric"
            conf = 0.65
        else:
            ctype = "accomplishment"
            conf = 0.6
        out.append(_mk_claim(source_id, ctype, s, confidence=conf))
    return out


def _extract_responsibilities(source_id: int, text: str) -> list[dict]:
    out: list[dict] = []
    for sentence in _sentences(text):
        s = sentence.strip()
        if not s or len(s) < 25 or len(s) > 280:
            continue
        sl = s.lower()
        if sl.startswith(("responsible for", "owned ", "drove ", "managed ")):
            ctype = "leadership" if any(h in sl for h in LEADERSHIP_HINTS) else "responsibility"
            out.append(_mk_claim(source_id, ctype, s, confidence=0.6))
    return out


def _extract_certs(source_id: int, text: str) -> list[dict]:
    out: list[dict] = []
    for sentence in _sentences(text):
        sl = sentence.lower()
        for hint in CERT_HINTS:
            if hint in sl:
                out.append(_mk_claim(
                    source_id, "certification", sentence.strip(),
                    confidence=0.65,
                ))
                break
    return out


def _extract_degrees(source_id: int, text: str) -> list[dict]:
    out: list[dict] = []
    for sentence in _sentences(text):
        sl = sentence.lower()
        for hint in DEGREE_HINTS:
            if hint in sl:
                out.append(_mk_claim(
                    source_id, "degree", sentence.strip(),
                    confidence=0.65,
                ))
                break
    return out


_PUBLICATION_RE = re.compile("|".join(PUBLICATION_HINTS), re.I)


def _extract_publications(source_id: int, text: str) -> list[dict]:
    out: list[dict] = []
    for sentence in _sentences(text):
        if _PUBLICATION_RE.search(sentence):
            out.append(_mk_claim(
                source_id, "publication", sentence.strip(), confidence=0.6,
            ))
    return out


def _rule_extract(source_id: int, text: str, source_type: str) -> list[dict]:
    claims: list[dict] = []
    claims.extend(_extract_roles(source_id, text))
    claims.extend(_extract_skills(source_id, text))
    claims.extend(_extract_accomplishments(source_id, text))
    claims.extend(_extract_responsibilities(source_id, text))
    claims.extend(_extract_certs(source_id, text))
    claims.extend(_extract_degrees(source_id, text))
    claims.extend(_extract_publications(source_id, text))
    return claims


# ---------- LLM refinement ----------

def _llm_extract(source_id: int, text: str) -> list[dict]:
    try:
        llm = get_llm()
    except Exception as e:  # noqa: BLE001
        log.debug("LLM unavailable: %s", e)
        return []
    system = (
        "You are a career-claim extractor. Output JSON only. "
        "Do NOT invent facts. Only extract claims that are explicitly present "
        "in the source text. If you are unsure, omit the claim."
    )
    schema = {
        "claims": [{
            "claim_type": "one of: role, accomplishment, skill, tool, "
                          "certification, degree, project, publication, "
                          "metric, responsibility, leadership",
            "claim_text": "string verbatim or lightly cleaned from source",
            "employer": "string or empty",
            "project": "string or empty",
            "skill": "string or empty",
            "tool": "string or empty",
            "date_start": "string or empty",
            "date_end": "string or empty",
        }],
    }
    user = "Extract claims from this source text:\n\n---\n" + (text or "")[:12000]
    try:
        data = llm.complete_json(system, user, schema_hint=schema)
    except Exception as e:  # noqa: BLE001
        log.debug("LLM extract failed: %s", e)
        return []
    raw = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        ctype = (c.get("claim_type") or "").strip().lower()
        if ctype not in CLAIM_TYPES:
            continue
        ctext = (c.get("claim_text") or "").strip()
        if not ctext:
            continue
        out.append(_mk_claim(
            source_id, ctype, ctext, confidence=0.8,
            employer=c.get("employer") or None,
            project=c.get("project") or None,
            skill=c.get("skill") or None,
            tool=c.get("tool") or None,
            date_start=c.get("date_start") or None,
            date_end=c.get("date_end") or None,
        ))
    return out


def _merge_claims(rule_claims: list[dict], llm_claims: list[dict],
                  source_text: str) -> list[dict]:
    """Heuristic claims = ground truth for presence. LLM may add or boost."""
    # Filter LLM claims by source-text support.
    llm_filtered: list[dict] = []
    for c in llm_claims:
        if _supported(c["claim_text"], source_text):
            llm_filtered.append(c)
        else:
            log.debug("dropping unsupported LLM claim: %s", c.get("claim_text", "")[:80])

    by_key: dict[tuple[str, str], dict] = {}

    def key(c: dict) -> tuple[str, str]:
        return (c["claim_type"], c["normalized_claim"][:120])

    for c in rule_claims:
        by_key[key(c)] = c

    for c in llm_filtered:
        k = key(c)
        if k in by_key:
            existing = by_key[k]
            # Agreement → boost
            existing["confidence"] = round(min(0.95, max(existing["confidence"], 0.95)), 3)
            # Fill in missing structured fields
            for fld in ("employer", "project", "skill", "tool",
                        "date_start", "date_end"):
                if not existing.get(fld) and c.get(fld):
                    existing[fld] = c[fld]
        else:
            by_key[k] = c

    return list(by_key.values())


# ---------- public API ----------

def extract_claims(source_id: int, text: str, source_type: str) -> list[dict]:
    """Return a list of CareerClaim dicts ready to insert into `career_claim`."""
    text = (text or "").strip()
    if not text:
        return []
    rule_claims = _rule_extract(source_id, text, source_type)
    # Final source-support sanity check on rule claims too.
    rule_claims = [c for c in rule_claims if _supported(c["claim_text"], text)]
    llm_claims = _llm_extract(source_id, text)
    merged = _merge_claims(rule_claims, llm_claims, text)

    # Final dedupe by normalized text + type.
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for c in merged:
        k = (c["claim_type"], c["normalized_claim"][:160])
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out
