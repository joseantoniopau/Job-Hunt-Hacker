"""Fetch a URL and return readable text. Best-effort robots.txt respect."""
from __future__ import annotations

import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("jhh.evidence")

try:
    import httpx  # type: ignore
    _HTTPX_OK = True
except Exception as _e:  # noqa: BLE001
    httpx = None  # type: ignore
    _HTTPX_OK = False
    _HTTPX_ERR = str(_e)

from . import html_parser, document_parser

_UA = "JobHuntHacker/0.1 (+https://github.com/) HTTPClient"
_TIMEOUT = 15.0

# robots cache: host -> (parsed_rules, fetched_at)
_ROBOTS_CACHE: dict[str, tuple[list[tuple[str, str]], float]] = {}
_ROBOTS_TTL = 3600.0


def _parse_robots(text: str) -> list[tuple[str, str]]:
    """Return list of (directive, value) pairs for User-agent: *."""
    rules: list[tuple[str, str]] = []
    current_agents: list[str] = []
    star_active = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            current_agents = [value.lower()]
            star_active = value == "*"
            continue
        if directive in ("disallow", "allow") and star_active:
            rules.append((directive, value))
    return rules


def _allowed_by_robots(url: str) -> bool:
    if not _HTTPX_OK:
        return True
    try:
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        cached = _ROBOTS_CACHE.get(host)
        now = time.time()
        if cached is None or (now - cached[1]) > _ROBOTS_TTL:
            try:
                r = httpx.get(f"{host}/robots.txt", headers={"User-Agent": _UA},
                              timeout=5.0, follow_redirects=True)
                rules = _parse_robots(r.text) if r.status_code < 400 else []
            except Exception:
                rules = []
            _ROBOTS_CACHE[host] = (rules, now)
            cached = _ROBOTS_CACHE[host]
        rules = cached[0]
        path = parsed.path or "/"
        # Longest-match wins
        best: tuple[int, str] | None = None
        for directive, value in rules:
            if not value:
                continue
            if path.startswith(value):
                if best is None or len(value) > best[0]:
                    best = (len(value), directive)
        if best is None:
            return True
        return best[1] == "allow"
    except Exception as e:  # noqa: BLE001
        log.debug("robots check failed for %s: %s", url, e)
        return True


def _looks_like_pdf(content_type: str, url: str) -> bool:
    ct = (content_type or "").lower()
    if "application/pdf" in ct:
        return True
    return url.lower().split("?", 1)[0].endswith(".pdf")


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:300]


def fetch_url(url: str) -> dict[str, Any]:
    """Return ``{url, title, text, fetched_at, content_type}`` or ``{error}``."""
    if not url:
        return {"error": "empty url"}
    if not _HTTPX_OK:
        return {"error": "httpx not installed"}

    if not _allowed_by_robots(url):
        log.info("robots disallowed: %s", url)
        return {"error": "blocked by robots.txt", "url": url}

    try:
        with httpx.Client(
            headers={"User-Agent": _UA, "Accept": "*/*"},
            timeout=_TIMEOUT,
            follow_redirects=True,
        ) as client:
            r = client.get(url)
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_url request failed for %s: %s", url, e)
        return {"error": f"request failed: {e}", "url": url}

    if r.status_code >= 400:
        log.info("fetch_url %s -> %d", url, r.status_code)
        return {"error": f"http {r.status_code}", "url": url,
                "status_code": r.status_code}

    content_type = (r.headers.get("content-type") or "").split(";", 1)[0].strip()
    final_url = str(r.url)
    now = time.time()

    # PDF
    if _looks_like_pdf(content_type, final_url):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tf:
                tf.write(r.content)
                tmp_path = Path(tf.name)
            parsed = document_parser.parse_file(tmp_path)
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            return {
                "url": final_url,
                "title": Path(urlparse(final_url).path).name or final_url,
                "text": parsed.get("text", ""),
                "fetched_at": now,
                "content_type": content_type or "application/pdf",
            }
        except Exception as e:  # noqa: BLE001
            log.warning("pdf-from-url failed for %s: %s", url, e)
            return {"error": f"pdf parse failed: {e}", "url": final_url}

    # HTML / text
    try:
        html_body = r.text
    except Exception:
        html_body = r.content.decode("utf-8", errors="replace")

    if "html" in content_type or "<html" in html_body[:2000].lower():
        title = _extract_title(html_body)
        main_html = html_parser.extract_main_content(html_body)
        text = html_parser.html_to_text(main_html)
    else:
        title = ""
        text = html_body

    return {
        "url": final_url,
        "title": title,
        "text": text or "",
        "fetched_at": now,
        "content_type": content_type or "text/plain",
    }
