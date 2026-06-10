"""LLM-built base resume drawn entirely from the user's vault.

This is NOT job-tailored — it is a clean, faithful resume the user can
view to verify the system understands them. Job-specific tailoring lives
in resume_iterate.

The base resume is stored as a `tailored_resume` row with `job_id=NULL`
and `resume_type='base'`, which keeps the existing download / preview UI
working unchanged.

Honesty rules:
  * Every bullet must be traceable to a vault claim. The model is
    instructed to cite [claim #N] in the provenance map.
  * Names of employers/titles must come from the profile or claims —
    NEVER invented.
  * If the vault is too sparse to populate a section, the section is
    omitted rather than padded.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..db import audit, get_conn
from ..llm import get_llm
from ..llm.json_repair import extract_json
from ..llm.observability import observed_complete

log = logging.getLogger("jhh.base_resume")


SYSTEM_PROMPT = """You are a resume writer building a base (non-tailored)
resume from a candidate's verified claims. Output JSON only.

ABSOLUTE RULES:
- Use ONLY facts in the EVIDENCE PACK. Never invent employers, titles,
  metrics, dates, certifications, or skills.
- Every resume bullet must include the source claim_ids it draws from.
- Distinguish EMPLOYERS (e.g. eBay) from TITLES (e.g. Information Security
  Engineer III).
- If a section has no supporting evidence, omit it.
- This is the candidate's MASTER resume: include EVERY employer and dated
  role present in the evidence — do not editorially cut early-career
  positions. List experience reverse-chronologically (most recent first).
  Copy start/end dates from the role claims.

OUTPUT JSON:
{
  "header": {"name": str, "email": str|null, "phone": str|null,
             "location": str|null, "linkedin_url": str|null,
             "github_url": str|null, "portfolio_url": str|null},
  "summary": "2-3 sentence professional summary, grounded only in
              evidence. May omit if evidence is too thin.",
  "experience": [
    {"title": str, "company": str, "location": str|null,
     "start": str|null, "end": str|null,
     "bullets": [{"text": str, "claim_ids": [int, ...]}, ...]}
  ],
  "skills": ["..."],
  "projects":   [{"name": str, "description": str, "claim_ids": [int,...]}],
  "education":  [{"degree": str, "school": str, "year": str|null}],
  "certifications": ["..."],
  "honesty_notes": ["any place you had to omit something or where evidence was thin"]
}

Output the JSON only. No prose before or after.
"""


def _build_evidence_pack() -> tuple[str, dict]:
    """Build the EVIDENCE PACK and also return raw profile info."""
    conn = get_conn()
    parts: list[str] = []
    p_row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    p = dict(p_row) if p_row else {}

    parts.append("=== PROFILE ===")
    for k in ("name", "email", "phone", "location",
              "linkedin_url", "github_url", "portfolio_url"):
        if p.get(k):
            parts.append(f"{k}: {p[k]}")
    for k in ("target_titles", "target_keywords", "industries",
              "seniority_targets", "preferred_locations"):
        v = p.get(k)
        if v:
            try:
                v = json.loads(v) if isinstance(v, str) else v
            except Exception:
                pass
            if v:
                parts.append(f"{k}: {v}")

    src_rows = conn.execute(
        "SELECT id, source_type, COALESCE(filename, url, '') AS lbl "
        "FROM evidence_source ORDER BY id"
    ).fetchall()
    if src_rows:
        parts.append("\n=== EVIDENCE SOURCES ===")
        for r in src_rows:
            parts.append(f"  src#{r['id']} {r['source_type']} — {r['lbl']}")

    claim_rows = conn.execute(
        "SELECT id, source_id, claim_type, claim_text, "
        "       COALESCE(skill, '') AS skill, "
        "       COALESCE(tool,  '') AS tool, "
        "       COALESCE(employer, '') AS employer, "
        "       COALESCE(date_start, '') AS date_start, "
        "       COALESCE(date_end,  '') AS date_end "
        "FROM career_claim ORDER BY id"
    ).fetchall()
    if claim_rows:
        parts.append("\n=== VERIFIED CLAIMS ===")
        for r in claim_rows:
            tags = []
            if r["claim_type"]:
                tags.append(r["claim_type"])
            if r["skill"]:
                tags.append(f"skill={r['skill']}")
            if r["tool"]:
                tags.append(f"tool={r['tool']}")
            if r["employer"]:
                tags.append(f"employer={r['employer']}")
            if r["date_start"]:
                tags.append(f"dates={r['date_start']} - {r['date_end'] or '?'}")
            parts.append(f"[claim #{r['id']} src#{r['source_id']}] "
                         f"{r['claim_text']}"
                         + (f"  ({' | '.join(tags)})" if tags else ""))

    pack = "\n".join(parts)
    if len(pack) > 14000:
        pack = pack[:14000] + "\n…[truncated]"
    return pack, p


def _resume_to_markdown(resume: dict) -> str:
    """Render the structured resume JSON to clean Markdown."""
    out: list[str] = []
    h = resume.get("header") or {}
    name = h.get("name") or "Your Name"
    out.append(f"# {name}\n")
    contact = " · ".join(filter(None, [
        h.get("location"),
        h.get("email"),
        h.get("phone"),
        h.get("linkedin_url"),
        h.get("github_url"),
        h.get("portfolio_url"),
    ]))
    if contact:
        out.append(f"{contact}\n")
    summary = (resume.get("summary") or "").strip()
    if summary:
        out.append("\n## SUMMARY\n")
        out.append(summary + "\n")

    exp = resume.get("experience") or []
    if exp:
        out.append("\n## EXPERIENCE\n")
        for role in exp:
            title = role.get("title") or ""
            company = role.get("company") or ""
            dates = " — ".join(filter(None, [role.get("start"), role.get("end")]))
            loc = role.get("location") or ""
            header_line = f"**{title}** · {company}"
            if loc:
                header_line += f" · {loc}"
            if dates:
                header_line += f" · _{dates}_"
            out.append(header_line + "\n")
            for b in (role.get("bullets") or []):
                text = b.get("text") if isinstance(b, dict) else str(b)
                cids = b.get("claim_ids") if isinstance(b, dict) else []
                ctag = f"  _[claim {', '.join('#'+str(c) for c in cids)}]_" if cids else ""
                out.append(f"- {text}{ctag}")
            out.append("")

    projects = resume.get("projects") or []
    if projects:
        out.append("\n## PROJECTS\n")
        for pr in projects:
            name = pr.get("name") if isinstance(pr, dict) else str(pr)
            desc = pr.get("description") if isinstance(pr, dict) else ""
            cids = pr.get("claim_ids") if isinstance(pr, dict) else []
            ctag = f" _[claim {', '.join('#'+str(c) for c in cids)}]_" if cids else ""
            out.append(f"- **{name}** — {desc}{ctag}")

    skills = resume.get("skills") or []
    if skills:
        out.append("\n## SKILLS\n")
        out.append(", ".join(skills))

    edu = resume.get("education") or []
    if edu:
        out.append("\n## EDUCATION\n")
        for e in edu:
            line = f"- {e.get('degree','')}, {e.get('school','')}"
            if e.get("year"):
                line += f" ({e['year']})"
            out.append(line)

    certs = resume.get("certifications") or []
    if certs:
        out.append("\n## CERTIFICATIONS\n")
        for c in certs:
            out.append(f"- {c}")

    notes = resume.get("honesty_notes") or []
    if notes:
        out.append("\n## HONESTY NOTES\n")
        out.append("> The skill marked these gaps so you know what's been omitted:\n")
        for n in notes:
            out.append(f"- {n}")

    return "\n".join(out)


def generate_base_resume() -> dict:
    """Generate (or regenerate) the user's base resume from the vault.

    Persists as a `tailored_resume` row with job_id=NULL, resume_type='base'.
    Returns the freshly-stored row.
    """
    started = time.time()
    pack, profile = _build_evidence_pack()
    if "VERIFIED CLAIMS" not in pack:
        return {
            "ok": False,
            "error": "no_evidence",
            "detail": "Vault has no verified claims. Upload a resume or paste evidence first.",
        }

    provider = get_llm()
    if getattr(provider, "name", "template") == "template":
        # Deterministic fallback: emit a minimal resume with a banner
        resume = _deterministic_base_resume(profile)
        markdown = _resume_to_markdown(resume)
        resume_id = _persist_base_resume(resume, markdown, run_id=None)
        return {"ok": True, "data": {
            "id": resume_id, "markdown": markdown, "structured": resume,
            "generated_by": "template", "llm_run_id": None,
            "elapsed_ms": int((time.time() - started) * 1000),
        }}

    user = (
        "Build the base resume.\n\n"
        f"EVIDENCE PACK:\n{pack}\n\n"
        "Output the JSON described in the system prompt. No prose."
    )

    try:
        output, run_id = observed_complete(
            provider,
            stage="base_resume",
            system=SYSTEM_PROMPT,
            user=user,
            max_tokens=3200,
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("base resume LLM call failed: %s", exc)
        resume = _deterministic_base_resume(profile)
        markdown = _resume_to_markdown(resume)
        resume_id = _persist_base_resume(resume, markdown, run_id=None,
                                         note=f"LLM error: {type(exc).__name__}: {exc}")
        return {"ok": True, "data": {
            "id": resume_id, "markdown": markdown, "structured": resume,
            "generated_by": "template_fallback", "llm_run_id": None,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": str(exc),
        }}

    parsed = extract_json(output) if output else None
    if not isinstance(parsed, dict):
        log.warning("base resume output not parseable")
        resume = _deterministic_base_resume(profile)
        markdown = _resume_to_markdown(resume)
        resume_id = _persist_base_resume(resume, markdown, run_id=run_id,
                                         note="LLM output did not parse as JSON")
        return {"ok": True, "data": {
            "id": resume_id, "markdown": markdown, "structured": resume,
            "generated_by": "template_fallback", "llm_run_id": run_id,
            "elapsed_ms": int((time.time() - started) * 1000),
        }}

    markdown = _resume_to_markdown(parsed)
    resume_id = _persist_base_resume(parsed, markdown, run_id=run_id)
    return {"ok": True, "data": {
        "id": resume_id, "markdown": markdown, "structured": parsed,
        "generated_by": "llm", "llm_run_id": run_id,
        "elapsed_ms": int((time.time() - started) * 1000),
    }}


def _deterministic_base_resume(profile: dict) -> dict:
    """Minimal viable resume when no LLM is available — pulls only stated
    profile fields + every verified claim text as a bullet under a single
    'EXPERIENCE' block. Honest about its origin."""
    conn = get_conn()
    claims = conn.execute(
        "SELECT id, claim_text FROM career_claim ORDER BY id"
    ).fetchall()
    bullets = [{"text": r["claim_text"], "claim_ids": [r["id"]]} for r in claims]
    try:
        titles = json.loads(profile.get("target_titles") or "[]")
    except Exception:
        titles = []
    try:
        keywords = json.loads(profile.get("target_keywords") or "[]")
    except Exception:
        keywords = []
    return {
        "header": {
            "name": profile.get("name") or "",
            "email": profile.get("email") or None,
            "phone": profile.get("phone") or None,
            "location": profile.get("location") or None,
            "linkedin_url": profile.get("linkedin_url") or None,
            "github_url": profile.get("github_url") or None,
            "portfolio_url": profile.get("portfolio_url") or None,
        },
        "summary": "Deterministic base resume built from your verified vault claims. Connect an LLM provider and regenerate for a properly structured version.",
        "experience": [{
            "title": titles[0] if titles else "Professional",
            "company": "(see vault for breakdown)",
            "location": profile.get("location"),
            "start": None, "end": None,
            "bullets": bullets,
        }],
        "skills": keywords,
        "projects": [],
        "education": [],
        "certifications": [],
        "honesty_notes": [
            "This resume was assembled deterministically. Bullets are 1:1 claim transcriptions; for a properly-written resume connect an LLM and click REGENERATE.",
        ],
    }


def _persist_base_resume(structured: dict, markdown: str,
                         run_id: int | None, note: str = "") -> int:
    """Insert as a tailored_resume row with job_id=NULL, resume_type='base'.

    Replaces any prior base resume so only one current 'base' exists.
    """
    conn = get_conn()
    conn.execute("DELETE FROM tailored_resume WHERE resume_type = 'base'")
    cur = conn.execute(
        """INSERT INTO tailored_resume
           (job_id, base_resume_id, resume_type, markdown, plain_text,
            provenance_json, honesty_report_json, ats_report_json,
            keyword_report_json, created_at)
           VALUES (NULL, NULL, 'base', ?, ?, ?, ?, NULL, NULL, ?)""",
        (markdown,
         markdown,  # plain text fallback = markdown body
         json.dumps({"structured": structured, "llm_run_id": run_id, "note": note}),
         json.dumps({"honesty_notes": structured.get("honesty_notes") or []}),
         time.time()),
    )
    rid = int(cur.lastrowid)
    audit("base_resume_generated", "tailored_resume", rid, run_id=run_id)
    return rid


def get_base_resume() -> dict | None:
    """Return the current base resume (or None)."""
    row = get_conn().execute(
        "SELECT id, markdown, plain_text, provenance_json, honesty_report_json, created_at "
        "FROM tailored_resume WHERE resume_type = 'base' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        prov = json.loads(row["provenance_json"] or "{}")
    except Exception:
        prov = {}
    try:
        honesty = json.loads(row["honesty_report_json"] or "{}")
    except Exception:
        honesty = {}
    return {
        "id": row["id"],
        "markdown": row["markdown"],
        "plain_text": row["plain_text"],
        "structured": prov.get("structured") or {},
        "llm_run_id": prov.get("llm_run_id"),
        "honesty_notes": honesty.get("honesty_notes") or [],
        "created_at": row["created_at"],
    }
