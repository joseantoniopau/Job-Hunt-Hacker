"""Heuristic resume parser with optional LLM refinement.

Output shape:
{
  "name", "email", "phone", "links",
  "summary",
  "experience": [{title, company, location, dates, bullets}],
  "education": [...],
  "skills": [...],
  "projects": [...]
}

Heuristic = ground truth for "did this appear at all". LLM may *refine*
field assignments but cannot introduce wholly new content; we filter LLM
output against the source text before merging.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..llm import get_llm
from ..utils.text import dedupe_preserve_order, normalize

log = logging.getLogger("jhh.evidence")

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s\-.]?)?(?:\(?\d{2,4}\)?[\s\-.]?){2,4}\d{2,4}"
)
URL_RE = re.compile(r"\bhttps?://[^\s,;]+|\bwww\.[^\s,;]+", re.I)
LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[^\s,;]+", re.I)
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[^\s,;]+", re.I)

MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_TOKEN = rf"(?:(?:\d{{1,2}}/\d{{4}})|(?:(?:{MONTHS})[a-z]*\.?\s+\d{{4}})|(?:\d{{4}}))"
DATE_RANGE_RE = re.compile(
    rf"({DATE_TOKEN})\s*[-–—to]+\s*({DATE_TOKEN}|present|current|now)",
    re.I,
)

SECTION_KEYWORDS = {
    "summary": ["summary", "professional summary", "profile", "about me", "objective"],
    "experience": ["experience", "work experience", "professional experience",
                   "employment", "employment history", "work history", "career history"],
    "education": ["education", "academic background"],
    "skills": ["skills", "technical skills", "core skills", "core competencies",
               "technologies", "tools"],
    "projects": ["projects", "selected projects", "side projects", "personal projects"],
    "certifications": ["certifications", "licenses", "licenses & certifications"],
    "publications": ["publications", "papers"],
    "awards": ["awards", "honors", "honors and awards"],
}


def _is_header(line: str) -> str | None:
    s = line.strip().lower().rstrip(":")
    if not s or len(s) > 50:
        return None
    for canon, syns in SECTION_KEYWORDS.items():
        if s in syns:
            return canon
    # all-caps short line counts as a header if it matches keyword stems
    if line.strip().isupper() and len(line.strip().split()) <= 4:
        words = re.sub(r"[^a-z ]", "", s).strip()
        for canon, syns in SECTION_KEYWORDS.items():
            for syn in syns:
                if syn in words:
                    return canon
    return None


def _split_sections(text: str) -> dict[str, str]:
    lines = text.splitlines()
    sections: dict[str, list[str]] = {"_header": []}
    current = "_header"
    for line in lines:
        canon = _is_header(line)
        if canon:
            current = canon
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _extract_contacts(header_text: str, full_text: str) -> dict[str, Any]:
    """Pull name/email/phone/links from header region with fallback to full text."""
    email_match = EMAIL_RE.search(header_text) or EMAIL_RE.search(full_text)
    email = email_match.group(0) if email_match else ""

    phone = ""
    for region in (header_text, full_text):
        for m in PHONE_RE.finditer(region):
            digits = re.sub(r"\D", "", m.group(0))
            if 7 <= len(digits) <= 15:
                phone = m.group(0).strip()
                break
        if phone:
            break

    links = set()
    for region in (header_text, full_text):
        for m in URL_RE.finditer(region):
            links.add(m.group(0).rstrip(".,;"))
        for m in LINKEDIN_RE.finditer(region):
            links.add(m.group(0))
        for m in GITHUB_RE.finditer(region):
            links.add(m.group(0))

    # Name extraction is the highest-fabrication-risk path. Require a
    # genuine signal that this header is actually a resume header — at
    # minimum ONE of {email, phone, URL} must be present. If none, refuse
    # to guess a name from arbitrary text like "this is not a resume".
    name = ""
    has_signal = bool(email or phone or links)
    if has_signal:
        for line in header_text.splitlines():
            s = line.strip()
            if not s:
                continue
            if EMAIL_RE.search(s) or URL_RE.search(s):
                continue
            digits = re.sub(r"\D", "", s)
            if len(digits) >= 7:  # phone-ish
                continue
            words = s.split()
            # Stricter name heuristic:
            #  - 2-5 words (single-word names too risky to extract from junk)
            #  - every word starts with uppercase (proper noun pattern)
            #  - every word is a-z A-Z hyphen apostrophe only
            #  - line is not a section header (uppercase title like "EXPERIENCE")
            if not (2 <= len(words) <= 5):
                continue
            if s.isupper() and not any(w[0].islower() for w in words):
                # Could be a header word like "SUMMARY" or all-caps name "MARIA CHEN"
                # Accept only if multi-word AND looks like a person (no "EXPERIENCE" etc.)
                JUNK_HEADERS = {"summary","experience","education","skills","projects",
                                "certifications","publications","awards","contact",
                                "objective","profile"}
                if any(w.lower() in JUNK_HEADERS for w in words):
                    continue
            ok = all(re.match(r"[A-Z][A-Za-z\-']*$", w) for w in words)
            if ok:
                name = s
                break

    return {"name": name, "email": email, "phone": phone,
            "links": sorted(links)}


def _is_date_line(line: str) -> bool:
    """True when a line is just a date range plus separators / duration —
    e.g. "Jan 2020 - Present" or "2018 – 2019 · 1 yr 2 mos". Such lines must
    never be classified as a title or company name (Title/Date/Bullets
    layouts have no company line at all)."""
    s = (line or "").strip()
    if not s or not DATE_RANGE_RE.search(s):
        return False
    rest = re.sub(r"[\s\-–—,|·().]+", " ", DATE_RANGE_RE.sub("", s)).strip()
    return bool(re.fullmatch(r"(?:\d+\s*yrs?)?\s*(?:\d+\s*mos?)?", rest, re.I))


def _parse_experience(block: str) -> list[dict[str, Any]]:
    """Split experience block into entries by blank lines or date-range anchors."""
    if not block:
        return []
    # Split on blank lines
    raw_entries = re.split(r"\n\s*\n", block)
    out: list[dict[str, Any]] = []
    for raw in raw_entries:
        raw = raw.strip()
        if not raw:
            continue
        lines = [l for l in raw.splitlines() if l.strip()]
        if not lines:
            continue

        # Find date range
        date_match = None
        for line in lines:
            m = DATE_RANGE_RE.search(line)
            if m:
                date_match = m
                break

        dates = ""
        if date_match:
            dates = f"{date_match.group(1)} - {date_match.group(2)}"

        # Heuristic: top non-bullet lines = title/company/location
        non_bullets: list[str] = []
        bullets: list[str] = []
        for line in lines:
            stripped = line.strip()
            if re.match(r"^[\-\*••·▪◦]\s*", stripped):
                bullets.append(re.sub(r"^[\-\*••·▪◦]\s*", "", stripped))
            elif stripped.startswith(("-", "*")) or stripped[:2] in ("- ", "* "):
                bullets.append(stripped.lstrip("-* ").strip())
            else:
                non_bullets.append(stripped)

        # Title/company/location only come from non-bullet lines that are
        # NOT pure date ranges — in a Title/Date/Bullets layout the date
        # line must never be mistaken for a company name.
        info_lines = [l for l in non_bullets if not _is_date_line(l)]
        title = info_lines[0] if info_lines else ""
        company = info_lines[1] if len(info_lines) > 1 else ""
        location = ""
        if len(info_lines) > 2:
            # location often contains comma or city/state
            for cand in info_lines[2:]:
                if "," in cand or re.search(r"\b[A-Z]{2}\b", cand):
                    location = cand
                    break

        # Strip inline date ranges from mixed lines like
        # "Senior Engineer  Jan 2020 - Present" so titles/companies stay clean.
        if title and DATE_RANGE_RE.search(title):
            cleaned = DATE_RANGE_RE.sub("", title).strip(" -–—,|·")
            if cleaned:
                title = cleaned
        if company and DATE_RANGE_RE.search(company):
            cleaned = DATE_RANGE_RE.sub("", company).strip(" -–—,|·")
            if cleaned:
                company = cleaned

        out.append({
            "title": title,
            "company": company,
            "location": location,
            "dates": dates,
            "bullets": bullets,
        })
    return out


def _parse_education(block: str) -> list[dict[str, Any]]:
    if not block:
        return []
    entries = re.split(r"\n\s*\n", block)
    out = []
    for raw in entries:
        raw = raw.strip()
        if not raw:
            continue
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if not lines:
            continue
        date_match = None
        for line in lines:
            m = DATE_RANGE_RE.search(line) or re.search(r"\b(19|20)\d{2}\b", line)
            if m:
                date_match = m.group(0)
                break
        out.append({
            "institution": lines[0],
            "details": " | ".join(lines[1:]) if len(lines) > 1 else "",
            "dates": date_match or "",
        })
    return out


def _parse_skills(block: str) -> list[str]:
    if not block:
        return []
    # Split on common skill separators
    items = re.split(r"[,••·▪;\|\n]", block)
    out = []
    for it in items:
        s = re.sub(r"^[\-\*\s]+", "", it).strip()
        # drop "Category:" prefixes
        s = re.sub(r"^[A-Za-z &/]+:\s*", "", s)
        if s and 1 <= len(s) <= 60:
            out.append(s)
    return dedupe_preserve_order(out)


def _parse_projects(block: str) -> list[dict[str, Any]]:
    if not block:
        return []
    entries = re.split(r"\n\s*\n", block)
    out = []
    for raw in entries:
        raw = raw.strip()
        if not raw:
            continue
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if not lines:
            continue
        out.append({
            "name": lines[0],
            "description": " ".join(lines[1:]) if len(lines) > 1 else "",
        })
    return out


def _heuristic_parse(text: str) -> dict[str, Any]:
    sections = _split_sections(text)
    header = sections.get("_header", "")
    contacts = _extract_contacts(header, text)

    summary = sections.get("summary", "")
    experience = _parse_experience(sections.get("experience", ""))
    education = _parse_education(sections.get("education", ""))
    skills = _parse_skills(sections.get("skills", ""))
    projects = _parse_projects(sections.get("projects", ""))

    return {
        "name": contacts["name"],
        "email": contacts["email"],
        "phone": contacts["phone"],
        "links": contacts["links"],
        "summary": summary,
        "experience": experience,
        "education": education,
        "skills": skills,
        "projects": projects,
    }


def _llm_refine(text: str, heuristic: dict[str, Any]) -> dict[str, Any]:
    """Optionally call the LLM to refine. Returns {} on failure."""
    try:
        llm = get_llm()
    except Exception as e:  # noqa: BLE001
        log.debug("LLM unavailable: %s", e)
        return {}

    system = ("You are a resume parser. Output JSON only. "
              "Do NOT invent facts. If a field is absent from the resume text, "
              "use an empty string or empty array. Never fabricate employers, "
              "titles, dates, schools, skills, or achievements.")
    schema = {
        "name": "string",
        "email": "string",
        "phone": "string",
        "links": ["string"],
        "summary": "string",
        "experience": [{
            "title": "string", "company": "string", "location": "string",
            "dates": "string", "bullets": ["string"],
        }],
        "education": [{"institution": "string", "details": "string", "dates": "string"}],
        "skills": ["string"],
        "projects": [{"name": "string", "description": "string"}],
    }
    user = ("Parse this resume into JSON matching the schema. "
            "Source text follows.\n\n---\n" + text[:12000])
    try:
        out = llm.complete_json(system, user, schema_hint=schema)
        if isinstance(out, dict):
            return out
    except Exception as e:  # noqa: BLE001
        log.debug("LLM refine failed: %s", e)
    return {}


def _supported_in_source(value: str, source_text_lower: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return False
    if len(v) <= 40:
        return v in source_text_lower
    # For longer strings, require most words to appear
    words = [w for w in re.split(r"\W+", v) if len(w) >= 3]
    if not words:
        return v[:20] in source_text_lower
    hits = sum(1 for w in words if w in source_text_lower)
    return hits / len(words) >= 0.6


def _merge(heuristic: dict[str, Any], llm: dict[str, Any], text: str) -> dict[str, Any]:
    """LLM wins for non-empty fields, but only if the value is supported in the source."""
    if not llm:
        return heuristic
    source_lc = (text or "").lower()
    merged = dict(heuristic)

    def take(key: str) -> None:
        llm_val = llm.get(key)
        if not llm_val:
            return
        if isinstance(llm_val, str):
            if _supported_in_source(llm_val, source_lc):
                merged[key] = llm_val
        else:
            merged[key] = llm_val

    for k in ("name", "email", "phone", "summary"):
        take(k)

    # links: union (after filtering)
    if isinstance(llm.get("links"), list):
        combined = list(heuristic.get("links") or [])
        for link in llm["links"]:
            if isinstance(link, str) and link.strip() and link not in combined:
                if _supported_in_source(link, source_lc):
                    combined.append(link)
        merged["links"] = combined

    # skills: union with source check
    if isinstance(llm.get("skills"), list):
        combined_skills = list(heuristic.get("skills") or [])
        for s in llm["skills"]:
            if isinstance(s, str) and s.strip() and _supported_in_source(s, source_lc):
                if s not in combined_skills:
                    combined_skills.append(s)
        merged["skills"] = dedupe_preserve_order(combined_skills)

    # experience / education / projects: prefer richer (more entries with bullets)
    for k in ("experience", "education", "projects"):
        llm_list = llm.get(k) or []
        heur_list = heuristic.get(k) or []
        if not isinstance(llm_list, list):
            continue
        # Filter LLM entries by source presence (check titles/institutions)
        filtered = []
        for entry in llm_list:
            if not isinstance(entry, dict):
                continue
            anchor = entry.get("title") or entry.get("institution") or entry.get("name") or ""
            if anchor and _supported_in_source(anchor, source_lc):
                filtered.append(entry)
        if not filtered:
            continue
        # Prefer whichever has more total bullets/content
        def score(lst):
            n = 0
            for e in lst:
                if isinstance(e, dict):
                    n += len(e.get("bullets") or []) + 1
            return n
        if score(filtered) > score(heur_list):
            merged[k] = filtered

    return merged


def parse(text: str) -> dict[str, Any]:
    """Parse resume text into a structured dict."""
    text = (text or "").strip()
    if not text:
        return {"name": "", "email": "", "phone": "", "links": [],
                "summary": "", "experience": [], "education": [],
                "skills": [], "projects": []}
    heuristic = _heuristic_parse(text)
    llm_out = _llm_refine(text, heuristic)
    return _merge(heuristic, llm_out, text)
