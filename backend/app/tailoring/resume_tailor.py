"""Tailor a resume to a specific job — with provenance enforcement.

Flow:
    1. Load job from job_posting.
    2. Pull keyword matrix via ats_analyzer.analyze_job (defensive).
    3. Retrieve relevant claims via career_vault.retrieve_for_job (defensive).
    4. Call LLM with RESUME_TAILOR_SYS + RESUME_TAILOR_USER (evidence embedded
       as JSON in the user message so even TemplateProvider can work).
    5. Run guardrails.validate_provenance to drop any segment without
       backing evidence.
    6. Render to markdown + plain text + docx + pdf.
    7. Persist row in tailored_resume.
    8. Return the full bundle (markdown, plain_text, paths, provenance,
       honesty_report, ats_report, keyword_report).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import audit, get_conn, row_to_dict, tx
from ..llm import get_llm
from ..llm import guardrails
from ..llm.prompts import RESUME_TAILOR_SYS, RESUME_TAILOR_USER
from ..utils.exporters import to_docx, to_markdown, to_pdf, to_plain_text
from ..utils.text import slug
from .honesty_report import build_report
from .provenance import ProvenanceMap

log = logging.getLogger("jhh.tailoring.resume")


# ---------- defensive imports ----------

def _safe_analyze_job(job: dict, claims: list[dict]) -> dict:
    try:
        from ..matching.ats_analyzer import analyze_job  # type: ignore
        return analyze_job(job, claims) or {}
    except Exception as e:  # noqa: BLE001
        log.info("ats_analyzer unavailable (%s); using fallback", e)
        return _fallback_analyze_job(job, claims)


def _safe_retrieve_claims(job_text: str, top: int = 20) -> list[dict]:
    try:
        from ..services.career_vault import retrieve_for_job  # type: ignore
        out = retrieve_for_job(job_text, top=top) or []
        return [c for c in out if isinstance(c, dict)]
    except Exception as e:  # noqa: BLE001
        log.info("career_vault.retrieve_for_job unavailable (%s); falling back to allowed claims", e)
        return _fallback_list_claims()


def _fallback_list_claims() -> list[dict]:
    try:
        from ..services.career_vault import list_claims  # type: ignore
        out = list_claims(allowed_only=True) or []
        return [c for c in out if isinstance(c, dict)]
    except Exception:
        pass
    # Last-ditch: read directly from DB
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM career_claim WHERE allowed_for_resume = 1 ORDER BY confidence DESC LIMIT 100"
    ).fetchall()
    return [row_to_dict(r) or {} for r in rows]


def _fallback_analyze_job(job: dict, claims: list[dict]) -> dict:
    """Lightweight ATS fallback when matching/ats_analyzer isn't available."""
    from ..utils.text import keyword_tokens, normalize

    desc = " ".join([
        job.get("description") or "",
        " ".join(job.get("requirements") or []) if isinstance(job.get("requirements"), list) else (job.get("requirements") or ""),
        " ".join(job.get("benefits") or []) if isinstance(job.get("benefits"), list) else (job.get("benefits") or ""),
    ])
    job_tokens = set(keyword_tokens(desc))

    claim_blob = " ".join(
        normalize((c.get("claim_text") or "") + " " + (c.get("normalized_claim") or "") +
                  " " + (c.get("skill") or "") + " " + (c.get("tool") or ""))
        for c in (claims or [])
    )
    claim_tokens = set(keyword_tokens(claim_blob))

    # promote multi-word phrases? skip — simple version.
    matrix: list[dict] = []
    matched: list[str] = []
    missing: list[str] = []
    for tok in sorted(job_tokens):
        if len(tok) < 3:
            continue
        backed = tok in claim_tokens
        matrix.append({"keyword": tok, "resume_safe": backed, "evidence_ids": []})
        if backed:
            matched.append(tok)
        else:
            missing.append(tok)
    coverage = (len(matched) / max(1, len(matched) + len(missing))) if (matched or missing) else 0.0
    risk = "low" if coverage > 0.7 else ("medium" if coverage > 0.4 else "high")
    return {
        "keywords": matrix,
        "matched_keywords": matched[:50],
        "missing_keywords": missing[:50],
        "coverage": round(coverage, 3),
        "ats_risk": risk,
        "suggestions": [],
    }


# ---------- resume style profiles ----------

_STYLES: dict[str, dict[str, Any]] = {
    "master": {
        "section_order": ["Summary", "Experience", "Projects", "Skills", "Education", "Certifications"],
        "max_items_per_section": 50,
        "emphasis": "comprehensive",
    },
    "one_page": {
        "section_order": ["Summary", "Experience", "Skills", "Education"],
        "max_items_per_section": 4,
        "emphasis": "concise",
    },
    "two_page_senior": {
        "section_order": ["Summary", "Experience", "Leadership", "Skills", "Education"],
        "max_items_per_section": 8,
        "emphasis": "leadership_scope",
    },
    "technical": {
        "section_order": ["Summary", "Technical Skills", "Experience", "Projects", "Education"],
        "max_items_per_section": 6,
        "emphasis": "technical_depth",
    },
    "leadership": {
        "section_order": ["Summary", "Leadership Experience", "Experience", "Skills", "Education"],
        "max_items_per_section": 6,
        "emphasis": "impact_scope",
    },
    "executive": {
        "section_order": ["Summary", "Executive Experience", "Board & Advisory", "Education"],
        "max_items_per_section": 5,
        "emphasis": "outcomes_revenue",
    },
    "project_heavy": {
        "section_order": ["Summary", "Selected Projects", "Experience", "Skills", "Education"],
        "max_items_per_section": 6,
        "emphasis": "projects_first",
    },
    "transition": {
        "section_order": ["Summary", "Transferable Skills", "Experience", "Education"],
        "max_items_per_section": 6,
        "emphasis": "transferable",
    },
    "ai_ml": {
        "section_order": ["Summary", "ML & AI Experience", "Projects", "Technical Skills", "Education", "Publications"],
        "max_items_per_section": 6,
        "emphasis": "ml_depth",
    },
    "cybersecurity": {
        "section_order": ["Summary", "Security Experience", "Certifications", "Tools", "Education"],
        "max_items_per_section": 6,
        "emphasis": "security_depth",
    },
    "engineering": {
        "section_order": ["Summary", "Engineering Experience", "Projects", "Skills", "Education"],
        "max_items_per_section": 6,
        "emphasis": "engineering_outcomes",
    },
    "product": {
        "section_order": ["Summary", "Product Experience", "Outcomes & Launches", "Skills", "Education"],
        "max_items_per_section": 6,
        "emphasis": "product_outcomes",
    },
    "job_specific": {  # the default
        "section_order": ["Summary", "Experience", "Skills", "Projects", "Education"],
        "max_items_per_section": 6,
        "emphasis": "job_match",
    },
}


def _style_for(resume_type: str) -> dict:
    return _STYLES.get(resume_type) or _STYLES["job_specific"]


# ---------- main entry points ----------

def _load_job(job_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM job_posting WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"job_posting id={job_id} not found")
    return row_to_dict(row) or {}


def _coerce_claims_for_prompt(claims: list[dict]) -> list[dict]:
    """Trim each claim to the small subset the LLM needs to ground citations."""
    out: list[dict] = []
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        cid = c.get("id") or c.get("claim_id")
        if cid is None:
            continue
        out.append({
            "id": int(cid),
            "claim_type": c.get("claim_type") or "",
            "claim_text": (c.get("claim_text") or c.get("normalized_claim") or "")[:500],
            "employer": c.get("employer") or "",
            "project": c.get("project") or "",
            "skill": c.get("skill") or "",
            "tool": c.get("tool") or "",
            "date_start": c.get("date_start") or "",
            "date_end": c.get("date_end") or "",
            "evidence_strength": c.get("evidence_strength") or "medium",
        })
    return out


def _user_header_from_profile() -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    if row is None:
        return {}
    d = row_to_dict(row) or {}
    return {
        "name": d.get("name") or "",
        "email": d.get("email") or "",
        "phone": d.get("phone") or "",
        "location": d.get("location") or "",
        "links": [u for u in [d.get("linkedin_url"), d.get("github_url"), d.get("portfolio_url")] if u],
    }


def _deterministic_resume(job: dict, claims_for_prompt: list[dict], style: dict) -> dict:
    """Build a structured resume from claims when the LLM returns nothing usable.

    This is the honest fallback: every bullet is a verbatim claim with its id.
    """
    header = _user_header_from_profile()
    summary_lines = []
    role = job.get("title") or ""
    company = job.get("company") or ""
    if role:
        summary_lines.append(f"Targeting: {role}" + (f" at {company}" if company else ""))

    # Group claims by employer/project for an Experience section
    by_employer: dict[str, list[dict]] = {}
    skills: list[dict] = []
    projects: list[dict] = []
    education: list[dict] = []
    certs: list[dict] = []
    for c in claims_for_prompt:
        ct = (c.get("claim_type") or "").lower()
        if ct == "skill":
            skills.append(c)
        elif ct == "project":
            projects.append(c)
        elif ct == "education":
            education.append(c)
        elif ct == "certification":
            certs.append(c)
        else:
            key = c.get("employer") or "Experience"
            by_employer.setdefault(key, []).append(c)

    cap = int(style.get("max_items_per_section") or 6)
    sections: list[dict] = []

    if by_employer:
        items = []
        for emp, cs in by_employer.items():
            for c in cs[:cap]:
                items.append({
                    "text": (f"[{emp}] " if emp and emp != "Experience" else "") + (c.get("claim_text") or ""),
                    "evidence_ids": [c["id"]],
                })
        sections.append({"title": "Experience", "items": items[: cap * 3]})

    if projects:
        sections.append({
            "title": "Projects",
            "items": [{"text": c.get("claim_text") or "", "evidence_ids": [c["id"]]} for c in projects[:cap]],
        })

    if skills:
        sections.append({
            "title": "Skills",
            "items": [{"text": c.get("skill") or c.get("claim_text") or "", "evidence_ids": [c["id"]]}
                      for c in skills[:cap]],
        })

    if education:
        sections.append({
            "title": "Education",
            "items": [{"text": c.get("claim_text") or "", "evidence_ids": [c["id"]]} for c in education[:cap]],
        })

    if certs:
        sections.append({
            "title": "Certifications",
            "items": [{"text": c.get("claim_text") or "", "evidence_ids": [c["id"]]} for c in certs[:cap]],
        })

    return {
        "header": header,
        "summary": " ".join(summary_lines),
        "sections": sections,
        "keywords_used": [],
        "keywords_excluded_as_unsupported": [],
        "gaps": [],
    }


def tailor_resume(
    job_id: int,
    resume_type: str = "job_specific",
    base_resume_id: int | None = None,
) -> dict:
    job = _load_job(job_id)
    style = _style_for(resume_type)
    claims = _safe_retrieve_claims(
        " ".join([job.get("title") or "", job.get("company") or "", job.get("description") or ""]),
        top=25,
    )
    if not claims:
        claims = _fallback_list_claims()

    claims_for_prompt = _coerce_claims_for_prompt(claims)
    allowed_ids: set[int] = {c["id"] for c in claims_for_prompt}

    # ATS / keyword matrix
    ats_report = _safe_analyze_job(job, claims)
    keyword_matrix = ats_report.get("keywords") if isinstance(ats_report, dict) else []
    if not isinstance(keyword_matrix, list):
        keyword_matrix = []

    # Ask the LLM
    llm = get_llm()
    sys_prompt = RESUME_TAILOR_SYS
    user_prompt = RESUME_TAILOR_USER(
        {
            "title": job.get("title"),
            "company": job.get("company"),
            "description": job.get("description"),
            "keywords": [m.get("keyword") for m in keyword_matrix if isinstance(m, dict)],
            "required": ats_report.get("required") or [],
            "preferred": ats_report.get("preferred") or [],
        },
        claims_for_prompt,
        style.get("emphasis") or "job_match",
    )

    structured: dict = {}
    try:
        structured = llm.complete_json(sys_prompt, user_prompt, max_tokens=3200) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("LLM tailoring failed: %s", e)
        structured = {}

    # If the provider returned nothing usable, fall back to the deterministic
    # claim-stitching resume (the template provider returns "" for JSON tasks).
    if not isinstance(structured, dict) or not (structured.get("sections") or structured.get("summary")):
        structured = _deterministic_resume(job, claims_for_prompt, style)

    # Make sure header is populated from the profile if the LLM left it blank
    if not structured.get("header"):
        structured["header"] = _user_header_from_profile()
    else:
        profile_header = _user_header_from_profile()
        for k, v in profile_header.items():
            if not structured["header"].get(k):
                structured["header"][k] = v

    # Provenance enforcement
    cleaned = guardrails.validate_provenance(structured, allowed_ids)
    dropped = (cleaned.get("honesty_report") or {}).get("dropped_segments") or []

    # Build ProvenanceMap from the (cleaned) output
    pm = ProvenanceMap()
    for s_idx, sec in enumerate(cleaned.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        for i_idx, item in enumerate(sec.get("items") or []):
            seg_id = f"sections[{s_idx}].items[{i_idx}]"
            pm.link(seg_id, item.get("evidence_ids") or [])

    # Keyword safety split
    raw_used = cleaned.get("keywords_used") or []
    safe_kw, unsafe_kw = guardrails.enforce_keyword_safety(raw_used, keyword_matrix)
    cleaned["keywords_used"] = safe_kw
    cleaned["keywords_excluded_as_unsupported"] = sorted(set(
        (cleaned.get("keywords_excluded_as_unsupported") or []) + unsafe_kw
    ))

    # Missing evidence / unsupported job requirements
    unsupported_reqs: list[str] = []
    if isinstance(ats_report, dict):
        unsupported_reqs = list(ats_report.get("missing_keywords") or [])

    honesty = build_report(
        provenance=pm,
        keyword_matrix=keyword_matrix,
        gaps_flagged=cleaned.get("gaps") or [],
        dropped_segments=dropped,
        keywords_added=cleaned.get("keywords_used") or [],
        keywords_excluded_as_unsupported=cleaned.get("keywords_excluded_as_unsupported") or [],
        unsupported_job_requirements=unsupported_reqs,
        missing_evidence=unsupported_reqs,
    )

    # Render markdown + plain text
    markdown = to_markdown(cleaned)
    plain_text = to_plain_text(cleaned)

    # Persist FIRST to get an id for file naming
    now = time.time()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO tailored_resume
                (job_id, base_resume_id, resume_type, markdown, plain_text,
                 docx_path, pdf_path, provenance_json, honesty_report_json,
                 ats_report_json, keyword_report_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                base_resume_id,
                resume_type,
                markdown,
                plain_text,
                None,
                None,
                json.dumps(pm.to_dict(), default=str),
                json.dumps(honesty, default=str),
                json.dumps(ats_report, default=str),
                json.dumps({"keywords_used": cleaned.get("keywords_used") or [],
                            "keywords_excluded_as_unsupported": cleaned.get("keywords_excluded_as_unsupported") or [],
                            "matrix": keyword_matrix}, default=str),
                now,
            ),
        )
        new_id = int(cur.lastrowid)

    # Render docx + pdf, then update the row with paths
    title_slug = slug(job.get("title") or "resume") or "resume"
    base_name = f"resume_{new_id}_{title_slug}"
    docx_path: Path | None = None
    pdf_path: Path | None = None
    try:
        docx_path = to_docx(cleaned, settings.resumes_dir / f"{base_name}.docx")
    except Exception as e:  # noqa: BLE001
        log.warning("docx export failed for resume %d: %s", new_id, e)
    try:
        pdf_path = to_pdf(cleaned, settings.resumes_dir / f"{base_name}.pdf")
    except Exception as e:  # noqa: BLE001
        log.warning("pdf export failed for resume %d: %s", new_id, e)

    # Also write a markdown + txt file for easy download
    md_path = settings.resumes_dir / f"{base_name}.md"
    txt_path = settings.resumes_dir / f"{base_name}.txt"
    try:
        md_path.write_text(markdown, encoding="utf-8")
        txt_path.write_text(plain_text, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning("md/txt write failed: %s", e)

    with tx() as conn:
        conn.execute(
            "UPDATE tailored_resume SET docx_path = ?, pdf_path = ? WHERE id = ?",
            (str(docx_path) if docx_path else None,
             str(pdf_path) if pdf_path else None,
             new_id),
        )

    audit("resume_tailored", "tailored_resume", new_id, job_id=job_id, resume_type=resume_type,
          provider=getattr(llm, "name", "unknown"))

    return {
        "id": new_id,
        "job_id": job_id,
        "resume_type": resume_type,
        "structured": cleaned,
        "markdown": markdown,
        "plain_text": plain_text,
        "docx_path": str(docx_path) if docx_path else None,
        "pdf_path": str(pdf_path) if pdf_path else None,
        "md_path": str(md_path),
        "txt_path": str(txt_path),
        "provenance": pm.to_dict(),
        "honesty_report": honesty,
        "ats_report": ats_report,
        "keyword_report": {
            "keywords_used": cleaned.get("keywords_used") or [],
            "keywords_excluded_as_unsupported": cleaned.get("keywords_excluded_as_unsupported") or [],
            "matrix": keyword_matrix,
        },
    }


def bulk_tailor(job_ids: list[int], resume_type: str = "job_specific") -> list[dict]:
    out: list[dict] = []
    for jid in job_ids:
        try:
            out.append(tailor_resume(jid, resume_type=resume_type))
        except Exception as e:  # noqa: BLE001
            log.warning("bulk_tailor: job %s failed: %s", jid, e)
            out.append({"job_id": jid, "error": str(e)})
    return out


__all__ = ["tailor_resume", "bulk_tailor"]
