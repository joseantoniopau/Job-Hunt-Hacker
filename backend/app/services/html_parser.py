"""HTML helpers: text extraction, link extraction, main-content heuristic."""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

log = logging.getLogger("jhh.evidence")

try:
    from bs4 import BeautifulSoup  # type: ignore
    _BS_OK = True
except Exception as _e:  # noqa: BLE001
    BeautifulSoup = None  # type: ignore
    _BS_OK = False
    _BS_ERR = str(_e)


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{3,}")


def _strip_tags(html: str) -> str:
    text = _TAG_RE.sub(" ", html or "")
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n\n", text)
    return text.strip()


def html_to_text(html: str) -> str:
    """Best-effort plain text conversion."""
    if not html:
        return ""
    if not _BS_OK:
        return _strip_tags(html)
    try:
        soup = BeautifulSoup(html, "html.parser")  # type: ignore
        for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        text = soup.get_text("\n")
        text = _WS_RE.sub(" ", text)
        text = _BLANK_RE.sub("\n\n", text)
        return text.strip()
    except Exception as e:  # noqa: BLE001
        log.warning("html_to_text failed: %s", e)
        return _strip_tags(html)


def extract_links(html: str, base: str = "") -> list[str]:
    """Return absolute links from anchor tags, deduped, in order."""
    if not html:
        return []
    seen: set[str] = set()
    out: list[str] = []
    if not _BS_OK:
        for m in re.finditer(r'href=["\']([^"\']+)["\']', html, flags=re.I):
            href = m.group(1).strip()
            if base:
                href = urljoin(base, href)
            if href and href not in seen:
                seen.add(href)
                out.append(href)
        return out
    try:
        soup = BeautifulSoup(html, "html.parser")  # type: ignore
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("javascript:"):
                continue
            if base:
                href = urljoin(base, href)
            if href in seen:
                continue
            seen.add(href)
            out.append(href)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("extract_links failed: %s", e)
        return []


def _candidate_score(tag) -> int:
    """Score a tag's likelihood of being the main content block."""
    if tag is None:
        return 0
    text = tag.get_text(" ", strip=True) if hasattr(tag, "get_text") else ""
    return len(text)


def extract_main_content(html: str) -> str:
    """Return HTML of the largest <main>/<article>/content-like block.

    Falls back to the whole document if nothing scores well.
    """
    if not html:
        return ""
    if not _BS_OK:
        return html
    try:
        soup = BeautifulSoup(html, "html.parser")  # type: ignore
        for tag in soup(["script", "style", "noscript", "iframe", "svg",
                          "header", "footer", "nav", "aside"]):
            tag.decompose()

        candidates = []
        # Standard semantic tags
        candidates.extend(soup.find_all("main"))
        candidates.extend(soup.find_all("article"))
        # class~="content" / id~="content" heuristics
        for tag in soup.find_all(True, attrs={"class": True}):
            cls = " ".join(tag.get("class") or []).lower()
            if any(k in cls for k in ("content", "post", "article", "main", "entry", "body")):
                candidates.append(tag)
        for tag in soup.find_all(True, attrs={"id": True}):
            tid = (tag.get("id") or "").lower()
            if any(k in tid for k in ("content", "post", "article", "main", "entry")):
                candidates.append(tag)

        # de-dup by id(tag)
        seen_ids: set[int] = set()
        uniq = []
        for c in candidates:
            if id(c) in seen_ids:
                continue
            seen_ids.add(id(c))
            uniq.append(c)

        if not uniq:
            body = soup.body or soup
            return str(body)

        best = max(uniq, key=_candidate_score)
        # If best is tiny, fall back to body
        if _candidate_score(best) < 200:
            body = soup.body or soup
            return str(body)
        return str(best)
    except Exception as e:  # noqa: BLE001
        log.warning("extract_main_content failed: %s", e)
        return html
