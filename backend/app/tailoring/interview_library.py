"""Curated interview-question library, keyed by role family.

Backed by `data/seed/interview_questions.json`. The buckets match the
families defined in `backend.app.matching.scorer._ROLE_FAMILIES` (plus
a cross-functional "general" bucket).

This is the *library* — `interview_prep.generate()` uses it to populate
`likely_questions` for a tailored interview prep packet, falling back to
its hardcoded five if the library is unavailable.
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from typing import Optional

from ..config import settings

log = logging.getLogger("jhh.tailoring.interview_library")


# Cache loaded once per process — the seed file is small + read-only.
_CACHE: Optional[dict[str, list[str]]] = None


def _seed_path() -> Path:
    return Path(settings.root) / "data" / "seed" / "interview_questions.json"


def load_questions() -> dict[str, list[str]]:
    """Load + cache the question library.

    Returns a dict ``{family: [question, ...]}``. On any failure (file
    missing, malformed JSON, wrong shape), returns an empty dict so
    callers can fall back to whatever defaults they have.
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = _seed_path()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        log.warning("interview_questions.json not found at %s", path)
        _CACHE = {}
        return _CACHE
    except Exception as exc:  # noqa: BLE001
        log.warning("interview_questions.json could not be parsed: %s", exc)
        _CACHE = {}
        return _CACHE

    if not isinstance(data, dict):
        log.warning("interview_questions.json must be a JSON object")
        _CACHE = {}
        return _CACHE

    cleaned: dict[str, list[str]] = {}
    for fam, qs in data.items():
        if not isinstance(fam, str):
            continue
        if not isinstance(qs, list):
            continue
        cleaned[fam] = [str(q).strip() for q in qs if isinstance(q, str) and q.strip()]
    _CACHE = cleaned
    return _CACHE


def reload_questions() -> dict[str, list[str]]:
    """Force a fresh load (used by tests + admin re-seed flows)."""
    global _CACHE
    _CACHE = None
    return load_questions()


def _classify(role_title: str) -> set[str]:
    """Defensive wrapper around scorer._classify_role_families."""
    try:
        from ..matching.scorer import _classify_role_families  # type: ignore
        return set(_classify_role_families(role_title) or [])
    except Exception:  # noqa: BLE001
        return set()


def questions_for_role(role_title: str, n: int = 6) -> list[str]:
    """Return up to ``n`` interview questions for ``role_title``.

    Strategy:
      1. Classify the title into role families via scorer.
      2. Pull questions from each matched bucket.
      3. Always blend in two "general" questions so the list isn't
         hyper-specialized.
      4. Deduplicate while preserving insertion order.

    If the library is empty (file missing/broken), returns an empty list.
    Callers should treat an empty return as "no library — use defaults".
    """
    lib = load_questions()
    if not lib:
        return []

    target = max(1, int(n))
    families = _classify(role_title or "")
    # Always include "general" as a guaranteed fallback so we never return
    # an empty list when the library is present but the title was unclassifiable.
    fallback_families: list[str] = []
    for fam in sorted(families):
        if fam in lib and fam != "general":
            fallback_families.append(fam)
    if not fallback_families and "general" in lib:
        # Pure-general fallback for unclassifiable titles
        return list(dict.fromkeys(lib["general"]))[:target]

    out: list[str] = []
    seen: set[str] = set()

    # 1. Two general questions up top so the interviewee always sees
    #    something widely applicable.
    general = lib.get("general") or []
    for q in general[:2]:
        if q not in seen:
            out.append(q)
            seen.add(q)

    # 2. Distribute remaining slots across matched families round-robin
    #    so multi-family roles (e.g. "Product Engineer") get coverage from each.
    per_fam_pools: list[list[str]] = [list(lib.get(f) or []) for f in fallback_families]
    idx = 0
    while len(out) < target and any(per_fam_pools):
        pool = per_fam_pools[idx % len(per_fam_pools)]
        if pool:
            q = pool.pop(0)
            if q not in seen:
                out.append(q)
                seen.add(q)
        else:
            # remove empty pool to avoid infinite loop
            per_fam_pools.pop(idx % len(per_fam_pools))
            if not per_fam_pools:
                break
            continue
        idx += 1

    # 3. If still short, top up from "general"
    for q in general[2:]:
        if len(out) >= target:
            break
        if q not in seen:
            out.append(q)
            seen.add(q)

    return out[:target]


def sample_questions(family: str, n: int = 3, seed: int | None = None) -> list[str]:
    """Convenience: random sample of ``n`` questions from a specific family.

    Mostly useful for tests + ad-hoc demos. Deterministic when ``seed`` is given.
    """
    lib = load_questions()
    pool = list(lib.get(family) or [])
    if not pool:
        return []
    rng = random.Random(seed)
    rng.shuffle(pool)
    return pool[: max(1, int(n))]


__all__ = [
    "load_questions",
    "reload_questions",
    "questions_for_role",
    "sample_questions",
]
