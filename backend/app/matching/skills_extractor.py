"""Extract canonical skill / tech keywords from job text.

Loads `data/seed/ats_keywords.json` once (lru_cache) and builds a flat
alias -> canonical map. Matching is case-insensitive and whole-token —
"Java" will not match inside "JavaScript".
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from ..config import settings
from ..utils.text import dedupe_preserve_order

# Tokens may contain letters, digits, dots, plus, sharp, hyphen, slash.
# Must start AND end with an alphanumeric char (no trailing punctuation).
_TOKEN_RX = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9+#./\-]*[A-Za-z0-9+#])?")

# Common inflection suffixes we'll strip when looking up single tokens.
_SUFFIXES = ("ing", "ed", "es", "s")


@lru_cache(maxsize=1)
def _load_keywords() -> dict:
    path = Path(settings.root) / "data" / "seed" / "ats_keywords.json"
    if not path.exists():
        return {"categories": {}}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"categories": {}}


@lru_cache(maxsize=1)
def _alias_index() -> dict:
    """Returns a dict:
      {
        "by_alias": { alias_lower: (canonical, category) },
        "categories": { canonical: category },
        "phrase_aliases": [ (alias_lower, canonical, category), ... ]
      }
    Phrase aliases (contain whitespace) get a separate list because we
    match them by substring before tokenizing.
    """
    data = _load_keywords()
    by_alias: dict[str, tuple[str, str]] = {}
    categories: dict[str, str] = {}
    phrases: list[tuple[str, str, str]] = []
    for cat, items in data.get("categories", {}).items():
        for canonical, aliases in items.items():
            categories[canonical] = cat
            all_aliases = set(aliases or [])
            all_aliases.add(canonical)
            for al in all_aliases:
                a = (al or "").strip().lower()
                if not a:
                    continue
                if " " in a or "/" in a:
                    phrases.append((a, canonical, cat))
                else:
                    by_alias[a] = (canonical, cat)
    # longest phrases first to avoid partial dupes
    phrases.sort(key=lambda t: -len(t[0]))
    return {"by_alias": by_alias, "categories": categories, "phrase_aliases": phrases}


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN_RX.finditer(text or "")]


def extract_skills(text: str) -> list[str]:
    """Return canonical skill names mentioned in text. Order-preserving, deduped."""
    if not text:
        return []
    idx = _alias_index()
    by_alias = idx["by_alias"]
    phrase_aliases = idx["phrase_aliases"]

    found: list[str] = []
    consumed_spans: list[tuple[int, int]] = []

    lower = text.lower()

    # 1) phrase matches first (so we don't double-count "spring boot" as just "spring")
    for alias, canonical, _cat in phrase_aliases:
        start = 0
        while True:
            i = lower.find(alias, start)
            if i < 0:
                break
            # boundary check
            left_ok = i == 0 or not lower[i - 1].isalnum()
            right = i + len(alias)
            right_ok = right >= len(lower) or not lower[right].isalnum()
            if left_ok and right_ok:
                found.append(canonical)
                consumed_spans.append((i, right))
            start = i + max(1, len(alias))

    # 2) single-token matches
    for m in _TOKEN_RX.finditer(text):
        tok = m.group(0).lower()
        # Skip if inside an already-consumed phrase span
        ms, me = m.start(), m.end()
        if any(s <= ms and me <= e for s, e in consumed_spans):
            continue
        hit = by_alias.get(tok)
        if not hit:
            # try stripping common inflections (mentored -> mentor, coaching -> coach)
            for suf in _SUFFIXES:
                if len(tok) > len(suf) + 2 and tok.endswith(suf):
                    stem = tok[: -len(suf)]
                    hit = by_alias.get(stem)
                    if hit:
                        break
                    # double-letter ending: 'mentored' -> 'mentor' ok, 'mapped' -> 'map'
                    if len(stem) >= 2 and stem[-1] == stem[-2]:
                        hit = by_alias.get(stem[:-1])
                        if hit:
                            break
        if hit:
            found.append(hit[0])

    return dedupe_preserve_order(found)


def categorize_skill(canonical: str) -> str:
    """Return the broad category for a canonical skill, or '' if unknown."""
    return _alias_index()["categories"].get(canonical, "")


def all_canonical_skills() -> list[str]:
    return list(_alias_index()["categories"].keys())
