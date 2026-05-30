"""Seniority / level detection from titles + descriptions."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from ..config import settings

# Priority order: most specific first.
SENIORITY_ORDER = [
    "intern", "exec", "vp", "director", "manager",
    "principal", "staff", "senior", "mid", "entry",
]

SENIORITY_PATTERNS: dict[str, re.Pattern] = {
    "intern":    re.compile(r"\b(intern|internship)\b", re.I),
    "exec":      re.compile(r"\b(cto|ceo|cfo|coo|svp|evp|chief\s+\w+\s+officer|vp\s+of?\s+engineering)\b", re.I),
    "vp":        re.compile(r"\b(vp|vice\s+president)\b", re.I),
    "director":  re.compile(r"\b(director|head\s+of)\b", re.I),
    "manager":   re.compile(r"\b(manager|mgr)\b", re.I),
    "principal": re.compile(r"\bprincipal\b", re.I),
    "staff":     re.compile(r"\bstaff\b", re.I),
    "senior":    re.compile(r"\b(sr\.?|senior|snr)\b", re.I),
    "mid":       re.compile(r"\b(mid[-\s]?level|software\s+engineer\s+ii|engineer\s+ii)\b", re.I),
    "entry":     re.compile(r"\b(entry[-\s]?level|junior|jr\.?|associate|graduate|new\s+grad)\b", re.I),
}


@lru_cache(maxsize=1)
def _load_signals() -> dict:
    path = Path(settings.root) / "data" / "seed" / "seniority_signals.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def detect_seniority(title: str, description: str = "") -> str:
    """Return one of: intern, entry, mid, senior, staff, principal, manager, director, vp, exec.

    Title weighted more heavily than description. Title-level signals take priority;
    description only used to break ties when title is ambiguous.
    """
    title_text = (title or "")
    desc_text = (description or "")

    # 1. Try the high-priority regex patterns against the title first.
    for level in SENIORITY_ORDER:
        rx = SENIORITY_PATTERNS.get(level)
        if rx and rx.search(title_text):
            return level

    # 2. Also check the signal dictionary (substring match) against the title.
    signals = _load_signals()
    lower_title = title_text.lower()
    for level in SENIORITY_ORDER:
        for token in signals.get(level, []):
            tok = token.strip().lower()
            if not tok:
                continue
            if tok in lower_title:
                return level

    # 3. Fall back to the description.
    for level in SENIORITY_ORDER:
        rx = SENIORITY_PATTERNS.get(level)
        if rx and rx.search(desc_text):
            return level

    # 4. Default
    return "mid"


_PROXIMITY: dict[str, list[str]] = {
    "intern":    ["entry"],
    "entry":     ["mid", "intern"],
    "mid":       ["senior", "entry"],
    "senior":    ["staff", "mid"],
    "staff":     ["principal", "senior"],
    "principal": ["staff"],
    "manager":   ["director", "senior"],
    "director":  ["vp", "manager"],
    "vp":        ["exec", "director"],
    "exec":      ["vp"],
}


def match_seniority(job_level: str, user_targets: list[str]) -> float:
    """Return 0..1 score for how well job level fits user-targeted levels.

    Exact match = 1.0. Adjacent level = 0.6. Two away = 0.3. Else 0.0.
    If user_targets is empty, returns 0.7 (neutral — don't penalize for missing prefs).
    """
    if not user_targets:
        return 0.7
    if not job_level:
        return 0.5

    job = job_level.lower().strip()
    targets = [t.lower().strip() for t in user_targets if t and t.strip()]
    if not targets:
        return 0.7

    if job in targets:
        return 1.0

    # adjacency
    near = _PROXIMITY.get(job, [])
    for t in targets:
        if t in near:
            return 0.6
        # two-away — anyone in target's near or job's near's near
        far = set()
        for n in near:
            far.update(_PROXIMITY.get(n, []))
        if t in far:
            return 0.3

    return 0.1
