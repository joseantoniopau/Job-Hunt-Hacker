"""LinkedIn ingestion: never scrape — only parse what the user paste-dumps.

Heuristic section parser keyed on the common LinkedIn headings.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from . import html_parser

log = logging.getLogger("jhh.evidence")


# Order matters: longest/most-specific synonyms first.
SECTION_HEADERS: list[tuple[str, list[str]]] = [
    ("about", ["about", "summary", "profile summary"]),
    ("experience", ["experience", "work experience", "professional experience",
                    "employment", "work history"]),
    ("education", ["education", "education and training"]),
    ("skills", ["skills", "top skills", "skills & endorsements", "core competencies"]),
    ("certifications", ["certifications", "licenses & certifications",
                        "licenses and certifications", "licenses"]),
    ("recommendations", ["recommendations", "recommendations received"]),
    ("projects", ["projects"]),
    ("publications", ["publications"]),
    ("volunteer", ["volunteer experience", "volunteering", "volunteer"]),
    ("languages", ["languages"]),
    ("courses", ["courses"]),
    ("honors", ["honors", "honors & awards", "honors and awards", "awards"]),
    ("interests", ["interests"]),
    ("contact", ["contact", "contact info"]),
]


def _normalize_headers(text: str) -> list[tuple[int, str]]:
    """Return list of (line_index, canonical_section_name) for each header
    line we recognize."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for i, raw in enumerate(lines):
        stripped = raw.strip().lower().rstrip(":")
        if not stripped or len(stripped) > 60:
            continue
        for canonical, synonyms in SECTION_HEADERS:
            if stripped in synonyms:
                found.append((i, canonical))
                break
    return found


def ingest_text(text: str) -> dict[str, Any]:
    """Parse pasted LinkedIn profile text into ``{sections, raw_text}``."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return {"sections": {}, "raw_text": ""}

    headers = _normalize_headers(text)
    sections: dict[str, str] = {}
    lines = text.splitlines()
    if not headers:
        return {"sections": {}, "raw_text": text}

    for idx, (start_line, canonical) in enumerate(headers):
        end_line = headers[idx + 1][0] if idx + 1 < len(headers) else len(lines)
        body = "\n".join(lines[start_line + 1:end_line]).strip()
        if not body:
            continue
        # If repeated header, append.
        if canonical in sections:
            sections[canonical] += "\n\n" + body
        else:
            sections[canonical] = body

    return {"sections": sections, "raw_text": text}


def ingest_html(html: str) -> dict[str, Any]:
    """Convert pasted LinkedIn HTML to text, then parse sections."""
    if not html:
        return {"sections": {}, "raw_text": ""}
    text = html_parser.html_to_text(html)
    return ingest_text(text)
