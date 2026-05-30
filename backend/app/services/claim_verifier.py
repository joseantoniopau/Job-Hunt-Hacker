"""Verify whether a claim's text is actually supported by a stored evidence
source. Used by tailoring guardrails to refuse any bullet that doesn't
trace back to real evidence.
"""
from __future__ import annotations

import logging
import math
import re
from typing import Optional

from ..db import get_conn
from . import vector_store

log = logging.getLogger("jhh.evidence")

DEFAULT_THRESHOLD = 0.55


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(y * y for y in b)) or 1.0
    return num / (da * db)


def _chunks(text: str, size: int = 400, step: int = 200) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    out = []
    for i in range(0, max(1, len(text) - size + 1), step):
        out.append(text[i:i + size])
    if not out or text[-size:] not in out:
        out.append(text[-size:])
    return out


def verify_against_evidence(claim_text: str, evidence_id: int,
                            threshold: float = DEFAULT_THRESHOLD
                            ) -> tuple[bool, float, str]:
    """Return (supported_bool, confidence_score, reason)."""
    claim_text = (claim_text or "").strip()
    if not claim_text:
        return False, 0.0, "empty claim text"

    conn = get_conn()
    row = conn.execute(
        "SELECT raw_text, title, url FROM evidence_source WHERE id = ?",
        (evidence_id,),
    ).fetchone()
    if row is None:
        return False, 0.0, f"evidence_source {evidence_id} not found"

    source_text = (row["raw_text"] or "")
    if not source_text.strip():
        return False, 0.0, "evidence source has no raw_text"

    # Trivial substring check is a strong positive signal.
    if claim_text.lower() in source_text.lower():
        return True, 1.0, "exact substring match"

    try:
        claim_vec, _ = vector_store.embed(claim_text)
    except Exception as e:  # noqa: BLE001
        log.warning("verify embed (claim) failed: %s", e)
        return False, 0.0, f"embed failure: {e}"

    best = 0.0
    for chunk in _chunks(source_text):
        try:
            cv, _ = vector_store.embed(chunk)
        except Exception:
            continue
        sim = _cosine(claim_vec, cv)
        if sim > best:
            best = sim

    if best >= threshold:
        return True, round(best, 4), f"cosine {best:.3f} >= {threshold}"
    return False, round(best, 4), f"cosine {best:.3f} < {threshold}"
