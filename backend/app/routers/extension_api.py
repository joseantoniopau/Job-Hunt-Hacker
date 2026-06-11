"""Browser-extension support API (`/api/extension`).

Read-only endpoints consumed by the JHH browser extension (popup +
injected autofill panel). The extension NEVER auto-submits anything —
these endpoints only surface vault data so the user can review and fill
application forms themselves.

Endpoints
---------
GET /api/extension/status
    Liveness + identity probe for the popup.
    Response: ``{ok: true, data: {reachable: true, app: "Job Hunt Hacker",
    version: "<semver>", profile_name: str|null, profile_email: str|null,
    counts: {jobs: int, claims: int, tailored_resumes: int,
    cover_letters: int}}}``

GET /api/extension/fill-data?url=<page-url>&company=<name>&title=<role>
    Everything the autofill panel needs for the page the user is on.
    Job matching order: (1) ``url`` host+path against job_posting.apply_url
    (exact normalized match, then longest path-prefix match on the same
    host), (2) fuzzy company+title. All query params optional — with no
    match the response still carries the profile + base resume.
    Response: ``{ok: true, data: {
        profile: {name, email, phone, location, linkedin_url, github_url,
                  portfolio_url},
        job: {id, title, company, location, apply_url, source,
              matched_by: "url"|"company_title"} | null,
        resume_text: str|null,            # tailored plain_text for the job,
        resume_id: int|null,              # else base resume raw_text
        resume_source: "tailored"|"base"|null,
        cover_letter_text: str|null,      # latest letter for the job
        cover_letter_id: int|null,
        answers: {why_company: str|null, experience_summary: str|null,
                  evidence_claim_ids: [int, ...]}
    }}``
    The ``answers`` strings are template-composed from verified vault
    claims only (claim text verbatim + job-posting facts). When the vault
    holds no usable claims both answers are null — nothing is fabricated.
"""
from __future__ import annotations

import difflib
import logging
import re
from typing import Any
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter

from ..config import APP_VERSION
from ..db import audit, get_conn

log = logging.getLogger("jhh.routers.extension_api")

router = APIRouter(prefix="/api/extension", tags=["extension"])

# Claim types that read as full sentences (safe to drop into prose).
# Mirrors the cover-letter generator's honesty rule: a bare skill token
# like "postgresql" must never be passed off as a sentence.
_SENTENCE_TYPES = {"role", "accomplishment", "responsibility",
                   "leadership", "project", "metric", "experience"}

_STOPWORDS = {"the", "a", "an", "and", "of", "at", "in", "for", "to", "on",
              "with", "inc", "llc", "ltd", "corp", "co", "company", "remote"}

_PROFILE_FIELDS = ("name", "email", "phone", "location",
                   "linkedin_url", "github_url", "portfolio_url")


# ---------------------------------------------------------------------------
# helpers — URL + fuzzy matching
# ---------------------------------------------------------------------------

def _normalize_url(raw: str) -> tuple[str, str] | None:
    """Reduce a URL to a comparable (host, path) pair.

    Lowercases, strips scheme / leading ``www.`` / port / query / fragment /
    trailing slashes, and percent-decodes the path. Returns None when no
    host can be extracted.
    """
    u = (raw or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u.lstrip("/")
    try:
        parts = urlsplit(u)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None
    path = unquote(parts.path or "/").rstrip("/").lower() or "/"
    return host, path


def _match_job_by_url(conn: Any, url: str) -> dict | None:
    """Match a job_posting by apply_url host+path.

    Exact normalized (host, path) equality wins. Otherwise, on the same
    host, the job whose apply path shares the longest prefix relationship
    with the page path wins (handles tracking suffixes like
    ``/jobs/123/application`` vs ``/jobs/123``). Root-path-only overlap is
    never a match.
    """
    target = _normalize_url(url)
    if target is None:
        return None
    t_host, t_path = target
    rows = conn.execute(
        "SELECT id, title, company, location, apply_url, source FROM job_posting "
        "WHERE apply_url IS NOT NULL AND TRIM(apply_url) != '' "
        "ORDER BY id DESC LIMIT 5000"
    ).fetchall()
    best: dict | None = None
    best_score = 0
    for r in rows:
        norm = _normalize_url(r["apply_url"])
        if norm is None or norm[0] != t_host:
            continue
        j_path = norm[1]
        score = 0
        if j_path == t_path:
            score = 1_000_000 + len(j_path)
        elif j_path != "/" and t_path.startswith(j_path + "/"):
            score = len(j_path)
        elif t_path != "/" and j_path.startswith(t_path + "/"):
            score = len(t_path)
        if score > best_score:
            best_score = score
            best = dict(r)
    return best


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9+#]+", (s or "").lower())
            if t not in _STOPWORDS}


def _similarity(a: str, b: str) -> float:
    """Fuzzy 0..1 similarity: max of token-set Jaccard and char ratio."""
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0
    ta, tb = _tokens(a), _tokens(b)
    jaccard = (len(ta & tb) / len(ta | tb)) if (ta | tb) else 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return max(jaccard, ratio)


def _company_score(query: str, candidate: str) -> float:
    q = (query or "").strip().lower()
    c = (candidate or "").strip().lower()
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c or c in q:
        return 0.85
    return _similarity(q, c)


def _match_job_by_company_title(conn: Any, company: str, title: str) -> dict | None:
    """Fuzzy match by company and/or title. Newest job wins ties."""
    company = (company or "").strip()
    title = (title or "").strip()
    if not company and not title:
        return None
    rows = conn.execute(
        "SELECT id, title, company, location, apply_url, source FROM job_posting "
        "ORDER BY id DESC LIMIT 5000"
    ).fetchall()
    best: dict | None = None
    best_score = 0.0
    for r in rows:
        cs = _company_score(company, r["company"] or "") if company else 0.0
        ts = _similarity(title, r["title"] or "") if title else 0.0
        if company and title:
            if cs < 0.6 or ts < 0.5:
                continue
            score = 0.5 * cs + 0.5 * ts
        elif company:
            if cs < 0.85:
                continue
            score = cs
        else:
            if ts < 0.75:
                continue
            score = ts
        if score > best_score:
            best_score = score
            best = dict(r)
    return best


# ---------------------------------------------------------------------------
# helpers — vault lookups
# ---------------------------------------------------------------------------

def _load_profile(conn: Any) -> dict:
    row = conn.execute(
        "SELECT name, email, phone, location, linkedin_url, github_url, "
        "portfolio_url FROM user_profile WHERE id = 1"
    ).fetchone()
    if row is None:
        return {k: None for k in _PROFILE_FIELDS}
    return {k: row[k] for k in _PROFILE_FIELDS}


def _resume_for(conn: Any, job_id: int | None) -> tuple[str | None, int | None, str | None]:
    """Latest tailored plain_text for the job, else the base resume.

    Returns (text, id, source) where source is 'tailored' | 'base' | None.
    """
    if job_id is not None:
        row = conn.execute(
            "SELECT id, plain_text FROM tailored_resume "
            "WHERE job_id = ? AND plain_text IS NOT NULL AND TRIM(plain_text) != '' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (int(job_id),),
        ).fetchone()
        if row is not None:
            return row["plain_text"], int(row["id"]), "tailored"
    row = conn.execute(
        "SELECT id, raw_text FROM resume_document "
        "WHERE raw_text IS NOT NULL AND TRIM(raw_text) != '' "
        "ORDER BY is_master DESC, created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        return row["raw_text"], int(row["id"]), "base"
    return None, None, None


def _cover_letter_for(conn: Any, job_id: int | None) -> tuple[str | None, int | None]:
    if job_id is None:
        return None, None
    row = conn.execute(
        "SELECT id, text FROM cover_letter WHERE job_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (int(job_id),),
    ).fetchone()
    if row is None:
        return None, None
    return row["text"], int(row["id"])


def _fetch_claims(conn: Any, limit: int = 40) -> list[dict]:
    rows = conn.execute(
        "SELECT id, claim_type, claim_text, skill, tool, employer, "
        "user_verified, confidence FROM career_claim "
        "WHERE allowed_for_resume = 1 AND contradiction_status != 'contradicted' "
        "ORDER BY user_verified DESC, confidence DESC, id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def _sentence_claims(claims: list[dict]) -> list[dict]:
    out: list[dict] = []
    for c in claims:
        text = (c.get("claim_text") or "").strip()
        if not text or len(text.split()) < 4:
            continue
        ctype = (c.get("claim_type") or "").lower()
        if ctype and ctype not in _SENTENCE_TYPES:
            continue
        out.append(c)
    return out


def _ensure_period(s: str) -> str:
    s = s.strip()
    if s and s[-1] not in ".!?":
        s += "."
    return s


def _job_text(conn: Any, job_id: int) -> str:
    row = conn.execute(
        "SELECT title, description, requirements FROM job_posting WHERE id = ?",
        (int(job_id),),
    ).fetchone()
    if row is None:
        return ""
    return " ".join(str(row[k] or "") for k in ("title", "description", "requirements"))


def _compose_answers(conn: Any, job: dict | None,
                     company_hint: str, title_hint: str) -> dict:
    """Template-composed, evidence-grounded answer drafts.

    Every sentence is built from (a) verbatim verified-claim text or
    (b) facts taken from the matched job posting / caller-supplied
    company+title. No claims in the vault => both answers are null.
    """
    claims = _fetch_claims(conn)
    sentences = _sentence_claims(claims)
    if not claims:
        return {"why_company": None, "experience_summary": None,
                "evidence_claim_ids": []}

    used_ids: list[int] = []

    # ---- experience_summary: top 3 sentence-shaped claims, verbatim ----
    experience_summary: str | None = None
    if sentences:
        picked = sentences[:3]
        experience_summary = " ".join(_ensure_period(c["claim_text"]) for c in picked)
        used_ids.extend(int(c["id"]) for c in picked)

    # ---- why_company: job facts + documented skill overlap + one claim ----
    company = ((job or {}).get("company") or company_hint or "").strip()
    title = ((job or {}).get("title") or title_hint or "").strip()
    why_company: str | None = None
    if company or title:
        # Skills documented in the vault that the posting actually mentions.
        posting_text = _job_text(conn, int(job["id"])).lower() if job else ""
        overlap: list[str] = []
        overlap_ids: list[int] = []
        if posting_text:
            seen: set[str] = set()
            for c in claims:
                for key in ("skill", "tool"):
                    val = (c.get(key) or "").strip()
                    low = val.lower()
                    if not val or low in seen or len(low) < 2:
                        continue
                    if re.search(r"(?<![a-z0-9])" + re.escape(low) + r"(?![a-z0-9])",
                                 posting_text):
                        seen.add(low)
                        overlap.append(val)
                        overlap_ids.append(int(c["id"]))
            overlap = overlap[:5]
            overlap_ids = overlap_ids[:5]
        parts: list[str] = []
        if company and title:
            parts.append(f"I'm applying for the {title} role at {company}.")
        elif company:
            parts.append(f"I'm interested in joining {company}.")
        else:
            parts.append(f"I'm interested in the {title} role.")
        if overlap:
            parts.append("The posting calls for "
                         + ", ".join(overlap)
                         + " — all documented in my verified work history.")
            used_ids.extend(overlap_ids)
        if sentences:
            parts.append("For example: " + _ensure_period(sentences[0]["claim_text"]))
            used_ids.append(int(sentences[0]["id"]))
        why_company = " ".join(parts)

    # de-dupe ids, preserve order
    deduped: list[int] = []
    for i in used_ids:
        if i not in deduped:
            deduped.append(i)
    return {"why_company": why_company,
            "experience_summary": experience_summary,
            "evidence_claim_ids": deduped}


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
def status() -> dict:
    """Connection probe for the extension popup. See module docstring."""
    conn = get_conn()
    profile = _load_profile(conn)
    counts = {}
    for label, table in (("jobs", "job_posting"),
                         ("claims", "career_claim"),
                         ("tailored_resumes", "tailored_resume"),
                         ("cover_letters", "cover_letter")):
        try:
            counts[label] = int(conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except Exception:  # noqa: BLE001
            counts[label] = 0
    return {"ok": True, "data": {
        "reachable": True,
        "app": "Job Hunt Hacker",
        "version": APP_VERSION,
        "profile_name": profile.get("name"),
        "profile_email": profile.get("email"),
        "counts": counts,
    }}


@router.get("/fill-data")
def fill_data(url: str = "", company: str = "", title: str = "") -> dict:
    """Autofill payload for the extension's panel. See module docstring."""
    conn = get_conn()
    profile = _load_profile(conn)

    job: dict | None = None
    matched_by: str | None = None
    if url.strip():
        job = _match_job_by_url(conn, url)
        if job is not None:
            matched_by = "url"
    if job is None and (company.strip() or title.strip()):
        job = _match_job_by_company_title(conn, company, title)
        if job is not None:
            matched_by = "company_title"

    job_id = int(job["id"]) if job else None
    resume_text, resume_id, resume_source = _resume_for(conn, job_id)
    cover_letter_text, cover_letter_id = _cover_letter_for(conn, job_id)
    answers = _compose_answers(conn, job, company, title)

    job_out: dict | None = None
    if job is not None:
        job_out = {
            "id": job_id,
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
            "apply_url": job.get("apply_url"),
            "source": job.get("source"),
            "matched_by": matched_by,
        }

    audit("extension_fill_data", "job_posting", job_id,
          matched_by=matched_by, url=url[:300], company=company[:120],
          title=title[:120], resume_source=resume_source,
          has_cover_letter=cover_letter_id is not None,
          evidence_claim_ids=answers.get("evidence_claim_ids") or [])

    return {"ok": True, "data": {
        "profile": profile,
        "job": job_out,
        "resume_text": resume_text,
        "resume_id": resume_id,
        "resume_source": resume_source,
        "cover_letter_text": cover_letter_text,
        "cover_letter_id": cover_letter_id,
        "answers": answers,
    }}
