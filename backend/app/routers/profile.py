"""GET/PUT /api/profile — singleton user profile.

Also: POST /api/profile/infer — parse an uploaded resume + LinkedIn paste
into a draft UserProfileIn dict WITHOUT saving. The UI uses this to
prefill the Setup form so the user reviews + edits before committing.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..db import get_conn, row_to_dict, audit
from ..models.schemas import UserProfileIn, OK
from ..utils.text import dedupe_preserve_order

log = logging.getLogger("jhh.profile")

router = APIRouter(prefix="/api", tags=["profile"])


_LIST_FIELDS = ["target_titles", "target_keywords", "excluded_keywords",
                "preferred_locations", "employment_types", "seniority_targets",
                "industries", "excluded_industries", "preferred_companies",
                "excluded_companies", "visa_preferences"]

_JSON_FIELDS = ["interview_availability_json", "scoring_weights_json"]

_CITY_STATE_RE = re.compile(
    r"\b([A-Z][A-Za-z\.\-' ]+),\s+([A-Z]{2})\b"
)
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[^\s,;)]+", re.I)
_GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[^\s,;)]+", re.I)
_URL_RE = re.compile(r"https?://[^\s,;)]+", re.I)


# ----- existing endpoints -----

@router.get("/profile")
def get_profile() -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    if row is None:
        raise HTTPException(404, "profile row missing")
    return {"ok": True, "data": row_to_dict(row)}


@router.put("/profile")
def put_profile(body: UserProfileIn) -> OK:
    conn = get_conn()
    cols = []
    vals = []
    payload = body.model_dump(exclude_none=False)
    for k, v in payload.items():
        if k in _LIST_FIELDS:
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v or []))
        elif k in _JSON_FIELDS:
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v or {}))
        else:
            cols.append(f"{k} = ?")
            vals.append(v)
    cols.append("updated_at = ?")
    vals.append(time.time())
    sql = f"UPDATE user_profile SET {', '.join(cols)} WHERE id = 1"
    conn.execute(sql, vals)
    audit("profile_update", "user_profile", 1)
    return OK(detail="profile updated")


# ----- infer endpoint -----

@router.post("/profile/infer")
async def infer_profile(
    resume_file: UploadFile | None = File(default=None),
    linkedin_text: str | None = Form(default=None),
    linkedin_html: str | None = Form(default=None),
    linkedin_url: str | None = Form(default=None),
    github_url: str | None = Form(default=None),
    portfolio_url: str | None = Form(default=None),
) -> dict:
    """Parse the supplied resume + LinkedIn text and return a draft
    UserProfileIn. Nothing is persisted. Caller decides what to keep,
    edits it, and POSTs to PUT /api/profile to save.
    """
    inferred_fields: dict[str, Any] = {}
    inferred_meta: dict[str, list[str]] = {}   # field_name -> [sources]
    notes: list[str] = []
    sources_used: list[dict] = []

    resume_data: dict[str, Any] = {}
    linkedin_data: dict[str, Any] = {}

    # ---- parse resume if provided ----
    if resume_file is not None:
        try:
            resume_data = await _parse_resume_upload(resume_file)
            sources_used.append({
                "kind": "resume",
                "filename": resume_file.filename or "uploaded",
                "size_bytes": resume_data.get("_size", 0),
                "skills_found": len(resume_data.get("skills", []) or []),
                "experience_entries": len(resume_data.get("experience", []) or []),
            })
        except Exception as exc:  # noqa: BLE001
            notes.append(f"resume parse failed: {exc}")
            log.warning("resume parse failed: %s", exc)

    # ---- parse LinkedIn text or html if provided ----
    if linkedin_text or linkedin_html:
        try:
            linkedin_data = _parse_linkedin(linkedin_text, linkedin_html)
            sections = linkedin_data.get("sections") or {}
            sources_used.append({
                "kind": "linkedin",
                "sections_found": list(sections.keys()),
                "raw_text_chars": len(linkedin_data.get("raw_text", "") or ""),
            })
        except Exception as exc:  # noqa: BLE001
            notes.append(f"linkedin parse failed: {exc}")
            log.warning("linkedin parse failed: %s", exc)

    # ---- merge into profile fields ----
    def _set(field: str, value: Any, source: str) -> None:
        if value in (None, "", [], {}):
            return
        if field not in inferred_fields:
            inferred_fields[field] = value
            inferred_meta[field] = [source]
        else:
            inferred_meta[field].append(source)

    # name / email / phone — resume header is best
    if resume_data:
        _set("name", resume_data.get("name", "") or None, "resume")
        _set("email", resume_data.get("email", "") or None, "resume")
        _set("phone", resume_data.get("phone", "") or None, "resume")

    # location — try resume contacts header first, then linkedin "Contact" section
    loc = _detect_location(resume_data.get("_text") if resume_data else None,
                           linkedin_data.get("raw_text") if linkedin_data else None)
    if loc:
        _set("location", loc, "resume" if resume_data else "linkedin")

    # urls — explicit form fields win, then auto-detect from text
    if linkedin_url:
        _set("linkedin_url", linkedin_url.strip(), "user_input")
    elif resume_data:
        _set("linkedin_url", _first_match(_LINKEDIN_RE, resume_data.get("_text", "")), "resume")

    if github_url:
        _set("github_url", github_url.strip(), "user_input")
    elif resume_data:
        _set("github_url", _first_match(_GITHUB_RE, resume_data.get("_text", "")), "resume")

    if portfolio_url:
        _set("portfolio_url", portfolio_url.strip(), "user_input")
    elif resume_data:
        # Pick the first non-linkedin/github URL from links
        for link in resume_data.get("links", []) or []:
            if "linkedin.com" in link.lower() or "github.com" in link.lower():
                continue
            _set("portfolio_url", link, "resume")
            break

    # target_titles — most recent role title is the obvious default
    titles = _collect_titles(resume_data, linkedin_data)
    if titles:
        _set("target_titles", titles, "resume+linkedin" if resume_data and linkedin_data else
             ("resume" if resume_data else "linkedin"))

    # target_keywords — top skills from resume; supplement from linkedin Skills section
    keywords = _collect_keywords(resume_data, linkedin_data)
    if keywords:
        _set("target_keywords", keywords, "resume+linkedin" if resume_data and linkedin_data else
             ("resume" if resume_data else "linkedin"))

    # seniority_targets — derive from most recent title via seniority_parser
    if titles:
        sen = _detect_seniority_targets(titles[0])
        if sen:
            _set("seniority_targets", sen, "resume")

    # preferred_locations — default to current location if known; add "Remote" mention if LinkedIn open-to-remote signals
    if loc:
        prefs = [loc]
        if linkedin_data and _signals_remote_openness(linkedin_data.get("raw_text", "") or ""):
            prefs.append("Remote")
        _set("preferred_locations", prefs, "resume+linkedin" if loc and linkedin_data else "resume")

    # currency — defaulted via location heuristic
    cur = _guess_currency(loc or "")
    if cur:
        _set("currency", cur, "location")

    # ---- assemble draft, fill remaining schema fields with their defaults ----
    draft = UserProfileIn().model_dump(exclude_none=False)
    # Don't clobber non-empty existing inferred values; keep defaults for the rest
    for k, v in inferred_fields.items():
        draft[k] = v

    if not sources_used:
        notes.append("nothing supplied; returning blank draft")

    meta = {
        "inferred_fields": sorted(inferred_meta.keys()),
        "inferred_meta": inferred_meta,
        "sources_used": sources_used,
        "notes": notes,
    }
    # Embed the meta INSIDE data so the standard `{ok, data:{...}}` envelope
    # holds. Keep the top-level keys for backward-compat with v0.1 callers.
    return {
        "ok": True,
        "data": {**draft, **meta},
        **meta,
    }


# ----- helpers -----

async def _parse_resume_upload(upload: UploadFile) -> dict[str, Any]:
    """Read upload into a temp file, parse via document + resume parser.

    Returns the resume parser's dict plus `_text` (raw text) and `_size`.
    """
    from ..services.document_parser import parse_file
    from ..services.resume_parser import parse as parse_resume

    suffix = ""
    name = upload.filename or "resume"
    if "." in name:
        suffix = "." + name.rsplit(".", 1)[1].lower()

    raw = await upload.read()
    with tempfile.NamedTemporaryFile(prefix="jhh_infer_", suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    try:
        doc = parse_file(tmp_path)
        text = (doc or {}).get("text", "") or ""
        if not text.strip():
            raise RuntimeError("no extractable text — is it a scanned PDF?")
        parsed = parse_resume(text)
        parsed["_text"] = text
        parsed["_size"] = len(raw)
        return parsed
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _parse_linkedin(text: str | None, html: str | None) -> dict[str, Any]:
    from ..services.linkedin_ingestion import ingest_html, ingest_text
    if html:
        return ingest_html(html)
    return ingest_text(text or "")


def _first_match(pat: re.Pattern[str], text: str) -> str:
    if not text:
        return ""
    m = pat.search(text)
    if not m:
        return ""
    val = m.group(0).rstrip(".,;)")
    if not val.startswith("http"):
        val = "https://" + val
    return val


def _detect_location(resume_text: str | None, linkedin_text: str | None) -> str:
    """Look for "City, ST" pattern in the first 800 chars of resume header,
    falling back to LinkedIn's `Contact` section.
    """
    candidates: list[str] = []
    if resume_text:
        candidates.append(resume_text[:800])
    if linkedin_text:
        candidates.append(linkedin_text)
    for c in candidates:
        m = _CITY_STATE_RE.search(c)
        if m:
            return f"{m.group(1).strip()}, {m.group(2)}"
        # common LinkedIn `City, State, Country` triple — accept first two
        m2 = re.search(r"\b([A-Z][A-Za-z\-' ]+),\s+([A-Z][A-Za-z\-' ]+)(?:,\s+[A-Z][A-Za-z ]+)?\b",
                       c[:800])
        if m2 and m2.group(2).lower() not in {"present", "current"}:
            cand = f"{m2.group(1).strip()}, {m2.group(2).strip()}"
            if 5 <= len(cand) <= 50:
                candidates.append(cand)
                return cand
    return ""


def _collect_titles(resume: dict[str, Any], linkedin: dict[str, Any]) -> list[str]:
    """Build a forward-looking target_titles list.

    target_titles must be CLEAN job-title strings — never "Title — Company
    (dates)" suffixes. Strategy:
      1. Take the most recent role title from `resume.experience[*].title`,
         strip any "— Company" / "@ Company" / "(dates)" / trailing "()" noise.
      2. Add the forward-looking promotion (Senior → Staff → Principal, etc.).
      3. Add at most one sibling-discipline title (Senior Engineer →
         Engineering Manager).

    The user can edit any of these in Setup; this is the smart default.
    """
    raw: list[str] = []
    for exp in (resume.get("experience") or []):
        t = (exp.get("title") or "").strip()
        if t and 3 <= len(t) <= 120:
            raw.append(t)
    # LinkedIn 'experience' is a single text block — grab the first title-looking line
    if linkedin:
        exp_block = (linkedin.get("sections") or {}).get("experience", "")
        for line in (exp_block or "").splitlines():
            s = line.strip()
            if 3 <= len(s) <= 120 and not re.search(r"\d{4}", s):
                if any(w[0].isupper() for w in s.split()[:3] if w):
                    raw.append(s)
                    break

    titles: list[str] = []
    for t in raw:
        cleaned = _clean_title(t)
        if cleaned and 3 <= len(cleaned) <= 60:
            titles.append(cleaned)

    # Forward-looking suggestion based on the cleanest (first) title
    if titles:
        bumped = _bump_title(titles[0])
        for b in bumped:
            if b and b not in titles:
                titles.append(b)

    return dedupe_preserve_order(titles)[:6]


def _clean_title(raw: str) -> str:
    """Strip employer + date noise from a role title string.

    Examples:
      "Staff Backend Engineer — Lattice Data Systems (2020-2023)" → "Staff Backend Engineer"
      "Senior PM @ Stripe" → "Senior PM"
      "Software Engineer | Acme | 2018 - Present" → "Software Engineer"
      "Designer ()" → "Designer"
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # Remove anything from the first em-dash / hyphen / pipe / @ onward.
    # These are the canonical "Title — Company" / "Title | Company" / "Title @ Company" separators.
    for sep in (" — ", " – ", " | ", " @ ", " at "):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    # Strip trailing date range: "Senior Engineer 2019-2022" / "PM 2020 - Present"
    s = re.sub(r"\s+\d{4}\s*[-–—]\s*(?:\d{4}|present|now|current)\s*$",
               "", s, flags=re.I).strip()
    # Strip "(...) YYYY" patterns: "Senior Engineer (Brightline) 2019-2022"
    s = re.sub(r"\s*\([^)]*\)\s*\d{4}.*$", "", s).strip()
    # Strip any remaining trailing (…) — empty parens or date span at end
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    # Strip trailing employer in plain hyphen form: "Title - Company"
    s = re.sub(r"\s+-\s+[A-Z][A-Za-z0-9 .,&'-]+$", "", s).strip()
    # Drop trailing punctuation
    s = s.rstrip(" -,|·")
    return s


def _bump_title(title: str) -> list[str]:
    """Given a current title, return zero or more forward-looking variants
    that represent the next likely step on the user's trajectory."""
    if not title:
        return []
    t = title.strip()
    out: list[str] = []

    # 1) Roman/numeric level: II → III, III → IV, etc.
    m = re.search(r"\b(I{1,4}V?|IV|V)\b\s*$", t)
    if m:
        roman = m.group(1)
        roman_map = {"I": "II", "II": "III", "III": "IV", "IV": "V", "V": "VI"}
        if roman in roman_map:
            out.append(t[:m.start()].rstrip() + " " + roman_map[roman])
    m2 = re.search(r"\b(\d+)\b\s*$", t)
    if m2:
        try:
            n = int(m2.group(1))
            out.append(t[:m2.start()].rstrip() + " " + str(n + 1))
        except ValueError:
            pass

    # 2) Word-prefix bump: Junior → Mid → Senior → Staff → Principal
    PROMOTIONS = [
        (r"^\bJunior\b\s*", "Mid-Level "),
        (r"^\bJr\.?\b\s*", "Mid-Level "),
        (r"^\bAssociate\b\s*", "Mid-Level "),
        (r"^\bMid[-\s]?Level\b\s*", "Senior "),
        (r"^\bMid\b\s*", "Senior "),
        (r"^\bSenior\b\s*", "Staff "),
        (r"^\bSr\.?\b\s*", "Staff "),
        (r"^\bStaff\b\s*", "Principal "),
        (r"^\bPrincipal\b\s*", "Distinguished "),
    ]
    for pat, repl in PROMOTIONS:
        if re.search(pat, t, re.I):
            out.append(re.sub(pat, repl, t, count=1, flags=re.I).strip())
            break
    # If no seniority prefix at all, prepend "Senior" as a likely next rung
    if not any(re.search(p[0], t, re.I) for p in PROMOTIONS) and not m and not m2:
        if not re.search(r"^\b(VP|Chief|Director|Head|CTO|CEO|CFO|COO)\b", t, re.I):
            out.append("Senior " + t)
            out.append("Staff " + t)

    # 3) Sibling promotion: IC → Manager (e.g. "Senior Engineer" → "Engineering Manager")
    m3 = re.search(r"(?i)\b(engineer|designer|analyst|scientist|developer)\b", t)
    if m3:
        discipline = m3.group(1).lower()
        if discipline == "engineer":
            out.append("Engineering Manager")
        elif discipline == "designer":
            out.append("Design Manager")
        elif discipline == "analyst":
            out.append("Analytics Manager")
        elif discipline == "scientist":
            out.append("Data Science Manager")
        elif discipline == "developer":
            out.append("Engineering Manager")

    # 4) Manager → Director step
    if re.search(r"(?i)\bManager\b", t) and not re.search(r"(?i)\b(Director|VP|Head)\b", t):
        out.append(re.sub(r"(?i)\bManager\b", "Director", t, count=1).strip())

    return [o.strip() for o in out if o and o.strip() != t.strip()]


def _collect_keywords(resume: dict[str, Any], linkedin: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for s in (resume.get("skills") or []):
        s = s.strip()
        if s:
            out.append(s)
    if linkedin:
        skills_block = (linkedin.get("sections") or {}).get("skills", "")
        for s in re.split(r"[\n,•·\|;]", skills_block or ""):
            s = re.sub(r"^[\-\*\s]+", "", s).strip()
            # drop endorsement counts like "Python · 32"
            s = re.sub(r"·.*$", "", s).strip()
            if s and 2 <= len(s) <= 60:
                out.append(s)
    # Also pass-through via the canonical extractor so we promote known
    # canonical names (e.g. "k8s" → "Kubernetes")
    try:
        from ..matching.skills_extractor import extract_skills
        combined = " ".join(out) + " " + (resume.get("_text", "") or "")
        canonical = extract_skills(combined)
        out = canonical + [s for s in out if s.lower() not in {c.lower() for c in canonical}]
    except Exception:
        pass
    return dedupe_preserve_order(out)[:12]


def _detect_seniority_targets(title: str) -> list[str]:
    try:
        from ..matching.seniority_parser import detect_seniority
    except Exception:
        return []
    lvl = detect_seniority(title or "")
    if not lvl:
        return []
    # Suggest the detected level + the next-step level (the user's likely target)
    ladder = ["intern", "entry", "mid", "senior", "staff", "principal",
              "manager", "director", "vp", "exec"]
    out = [lvl]
    try:
        i = ladder.index(lvl)
        # Suggest next non-IC step intelligently — keep one rung up on IC ladder
        if lvl in ("entry", "mid", "senior", "staff") and i + 1 < len(ladder):
            nxt = ladder[i + 1]
            if nxt not in out:
                out.append(nxt)
    except ValueError:
        pass
    return out


def _signals_remote_openness(text: str) -> bool:
    t = (text or "").lower()
    return any(s in t for s in (
        "open to remote", "remote-friendly", "remote first", "remote-first",
        "open to relocation", "open to work · remote", "remote ·",
    ))


def _guess_currency(location: str) -> str:
    if not location:
        return ""
    t = location.lower()
    if any(x in t for x in (", uk", ", gb", "london", "england")):
        return "GBP"
    if any(x in t for x in ("ireland", "germany", "france", "spain", "italy",
                            "netherlands", "portugal", "belgium")):
        return "EUR"
    if "canada" in t or re.search(r",\s+(ON|BC|AB|QC|MB|NS|NB|SK|NL|PE|YT|NT|NU)$", location):
        return "CAD"
    if "australia" in t:
        return "AUD"
    if "switzerland" in t or "zurich" in t or "geneva" in t:
        return "CHF"
    return "USD"
