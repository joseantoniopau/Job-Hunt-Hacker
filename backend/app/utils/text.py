"""Small text utilities used throughout the app."""
from __future__ import annotations

import hashlib
import re
from typing import Iterable


_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9+#.\- ]+")


def normalize(text: str) -> str:
    return _WS.sub(" ", (text or "").strip()).lower()


def slug(text: str) -> str:
    t = normalize(text)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:80]


def keyword_tokens(text: str) -> list[str]:
    t = (text or "").lower()
    t = _NON_ALNUM.sub(" ", t)
    return [w for w in t.split() if len(w) >= 2]


def content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8", errors="ignore"))
        h.update(b"|")
    return h.hexdigest()


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        k = (it or "").strip().lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def truncate(text: str, n: int) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"
