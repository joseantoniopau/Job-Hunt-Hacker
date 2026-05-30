"""Packet builder. Assembles a single-application packet on disk:
resume (md/txt/docx), cover letter, recruiter outreach, interview prep,
provenance, and a manifest.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from ..config import settings
from ..db import get_conn, row_to_dict
from ..utils.text import slug

log = logging.getLogger("jhh.packet_builder")


# --- defensive imports for tailoring modules ---

def _import_resume_tailor():
    try:
        from ..tailoring import resume_tailor  # type: ignore
        return resume_tailor
    except Exception as exc:  # noqa: BLE001
        log.debug("resume_tailor unavailable: %s", exc)
        return None


def _import_cover_letter():
    try:
        from ..tailoring import cover_letter  # type: ignore
        return cover_letter
    except Exception as exc:  # noqa: BLE001
        log.debug("cover_letter unavailable: %s", exc)
        return None


def _import_recruiter():
    try:
        from ..tailoring import recruiter_messages  # type: ignore
        return recruiter_messages
    except Exception as exc:  # noqa: BLE001
        log.debug("recruiter_messages unavailable: %s", exc)
        return None


def _import_interview_prep():
    try:
        from ..tailoring import interview_prep  # type: ignore
        return interview_prep
    except Exception as exc:  # noqa: BLE001
        log.debug("interview_prep unavailable: %s", exc)
        return None


def _safe_call(label: str, fn, *args, **kwargs) -> dict:
    """Call an optional tailoring fn; return {ok, result?, error?}."""
    if fn is None:
        return {"ok": False, "error": f"{label} not configured"}
    try:
        res = fn(*args, **kwargs)
        return {"ok": True, "result": res}
    except Exception as exc:  # noqa: BLE001
        log.warning("%s failed: %s", label, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --- data loading ---

def _load_job(job_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT j.*, m.overall_score, m.skills_score, m.experience_score, "
        "m.salary_score, m.location_score, m.seniority_score, m.keyword_score, "
        "m.evidence_score, m.explanation, m.matched_keywords, m.missing_keywords, "
        "m.transferable_keywords, m.unsupported_keywords, m.red_flags, "
        "m.recommended_resume_strategy "
        "FROM job_posting j LEFT JOIN job_match m ON m.job_id = j.id "
        "WHERE j.id = ?",
        (int(job_id),),
    ).fetchone()
    return row_to_dict(row) if row else None


def _load_tailored_resume(job_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM tailored_resume WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (int(job_id),),
    ).fetchone()
    return row_to_dict(row) if row else None


def _load_cover_letter(job_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM cover_letter WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (int(job_id),),
    ).fetchone()
    return row_to_dict(row) if row else None


# --- packet I/O helpers ---

def _packet_dir(job: dict) -> Path:
    company = slug(job.get("company") or "company")
    title = slug(job.get("title") or "role")
    name = f"packet_{int(job['id'])}_{company}_{title}"
    p = settings.packets_dir / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write(p: Path, content: str | bytes) -> None:
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")


def _extract_text(value: Any) -> str:
    """Best-effort extraction from a tailoring result dict or string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for k in ("markdown", "text", "plain_text", "body", "content"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return json.dumps(value, indent=2, default=str)
    return str(value)


def _summary_md(job: dict) -> str:
    lines = [
        f"# Job Summary",
        "",
        f"**Title:** {job.get('title') or ''}",
        f"**Company:** {job.get('company') or ''}",
        f"**Location:** {job.get('location') or ''}  ({job.get('remote_type') or 'n/a'})",
        f"**Employment Type:** {job.get('employment_type') or 'n/a'}",
        f"**Salary:** {job.get('salary_min') or '?'}–{job.get('salary_max') or '?'} {job.get('currency') or ''}",
        f"**Posted:** {job.get('posted_at') or 'n/a'}",
        f"**Source:** {job.get('source') or ''}",
        f"**Apply URL:** {job.get('apply_url') or ''}",
        f"**Company URL:** {job.get('company_url') or ''}",
        "",
        f"## Match",
        f"**Overall score:** {job.get('overall_score') or 'n/a'}",
        f"**Skills:** {job.get('skills_score') or 'n/a'}  ",
        f"**Salary fit:** {job.get('salary_score') or 'n/a'}  ",
        f"**Location fit:** {job.get('location_score') or 'n/a'}  ",
        f"**Seniority fit:** {job.get('seniority_score') or 'n/a'}  ",
        "",
        f"## Explanation",
        str(job.get("explanation") or "(none)"),
        "",
        f"## Honesty notes",
        f"- Matched keywords: {', '.join(job.get('matched_keywords') or []) or 'n/a'}",
        f"- Transferable: {', '.join(job.get('transferable_keywords') or []) or 'n/a'}",
        f"- Missing: {', '.join(job.get('missing_keywords') or []) or 'n/a'}",
        f"- Unsupported (do NOT claim): {', '.join(job.get('unsupported_keywords') or []) or 'n/a'}",
        f"- Red flags: {', '.join(job.get('red_flags') or []) or 'n/a'}",
        "",
        f"## Recommended resume strategy",
        str(job.get("recommended_resume_strategy") or "(none)"),
    ]
    return "\n".join(lines)


def _resume_files(resume_row: dict | None, resume_result: dict | None, out_dir: Path) -> list[str]:
    """Write resume artifacts to packet dir. Returns list of file names."""
    written: list[str] = []

    # Pull md/txt either from tailoring result or DB row
    md = ""
    txt = ""
    docx_src: str | None = None

    if resume_result and isinstance(resume_result, dict):
        md = resume_result.get("markdown") or resume_result.get("text") or md
        txt = resume_result.get("plain_text") or resume_result.get("text") or txt
        docx_src = resume_result.get("docx_path") or docx_src

    if resume_row:
        if not md:
            md = resume_row.get("markdown") or ""
        if not txt:
            txt = resume_row.get("plain_text") or md
        if not docx_src:
            docx_src = resume_row.get("docx_path")

    if md:
        _write(out_dir / "resume.md", md)
        written.append("resume.md")
    if txt:
        _write(out_dir / "resume.txt", txt)
        written.append("resume.txt")
    if docx_src:
        try:
            src = Path(docx_src)
            if src.exists():
                shutil.copy2(src, out_dir / "resume.docx")
                written.append("resume.docx")
        except Exception as exc:  # noqa: BLE001
            log.debug("docx copy failed: %s", exc)
    return written


# --- public API ---

def build(job_id: int, options: dict | None = None) -> dict:
    options = options or {}
    job = _load_job(job_id)
    if not job:
        return {"ok": False, "error": f"job {job_id} not found"}

    # 1. tailoring (defensive)
    rt = _import_resume_tailor()
    cl = _import_cover_letter()
    rm = _import_recruiter()
    ip = _import_interview_prep()

    resume_call = _safe_call(
        "resume_tailor.tailor_resume",
        getattr(rt, "tailor_resume", None) if rt else None,
        int(job_id),
        options.get("resume_type") or "job_specific",
    )
    cover_call = _safe_call(
        "cover_letter.generate",
        getattr(cl, "generate", None) if cl else None,
        int(job_id),
        options.get("tone") or "professional",
    )
    recruiter_call = _safe_call(
        "recruiter_messages.generate",
        getattr(rm, "generate", None) if rm else None,
        int(job_id),
    )
    interview_call = _safe_call(
        "interview_prep.generate",
        getattr(ip, "generate", None) if ip else None,
        int(job_id),
    )

    # 2. Pull persisted artifacts as fallback / source of truth
    resume_row = _load_tailored_resume(int(job_id))
    cover_row = _load_cover_letter(int(job_id))

    # 3. Write packet directory
    out_dir = _packet_dir(job)
    files: list[str] = []
    file_meta: dict[str, dict] = {}

    # resume artifacts
    rfiles = _resume_files(
        resume_row,
        resume_call.get("result") if isinstance(resume_call.get("result"), dict) else None,
        out_dir,
    )
    files.extend(rfiles)

    # cover letter
    cover_text = ""
    if cover_call["ok"]:
        cover_text = _extract_text(cover_call["result"])
    if not cover_text and cover_row:
        cover_text = cover_row.get("text") or ""
    if cover_text:
        _write(out_dir / "cover_letter.txt", cover_text)
        files.append("cover_letter.txt")

    # recruiter message
    rec_text = ""
    if recruiter_call["ok"]:
        rec_text = _extract_text(recruiter_call["result"])
    if rec_text:
        _write(out_dir / "recruiter_message.txt", rec_text)
        files.append("recruiter_message.txt")

    # interview prep
    ip_text = ""
    if interview_call["ok"]:
        ip_text = _extract_text(interview_call["result"])
    if ip_text:
        _write(out_dir / "interview_prep.md", ip_text)
        files.append("interview_prep.md")

    # job summary
    summary_md = _summary_md(job)
    _write(out_dir / "job_summary.md", summary_md)
    files.append("job_summary.md")

    # provenance
    provenance = {
        "job_id": int(job_id),
        "company": job.get("company"),
        "title": job.get("title"),
        "source": job.get("source"),
        "apply_url": job.get("apply_url"),
        "match_explanation": job.get("explanation"),
        "matched_keywords": job.get("matched_keywords"),
        "transferable_keywords": job.get("transferable_keywords"),
        "missing_keywords": job.get("missing_keywords"),
        "unsupported_keywords": job.get("unsupported_keywords"),
        "red_flags": job.get("red_flags"),
        "resume_provenance": (resume_row or {}).get("provenance_json"),
        "honesty_report": (resume_row or {}).get("honesty_report_json"),
        "cover_provenance": (cover_row or {}).get("provenance_json"),
        "tailoring_results": {
            "resume": {"ok": resume_call["ok"], "error": resume_call.get("error")},
            "cover_letter": {"ok": cover_call["ok"], "error": cover_call.get("error")},
            "recruiter": {"ok": recruiter_call["ok"], "error": recruiter_call.get("error")},
            "interview_prep": {"ok": interview_call["ok"], "error": interview_call.get("error")},
        },
    }
    _write(out_dir / "provenance.json", json.dumps(provenance, indent=2, default=str))
    files.append("provenance.json")

    # file_meta with timestamps
    now = time.time()
    for f in files:
        try:
            st = (out_dir / f).stat()
            file_meta[f] = {"size": st.st_size, "mtime": st.st_mtime}
        except Exception:
            file_meta[f] = {"size": 0, "mtime": now}

    manifest = {
        "job_id": int(job_id),
        "packet_dir": str(out_dir),
        "company": job.get("company"),
        "title": job.get("title"),
        "source": job.get("source"),
        "created_at": now,
        "files": file_meta,
        "tailoring_status": {
            "resume": resume_call["ok"],
            "cover_letter": cover_call["ok"],
            "recruiter": recruiter_call["ok"],
            "interview_prep": interview_call["ok"],
        },
        "options": options,
    }
    _write(out_dir / "manifest.json", json.dumps(manifest, indent=2, default=str))
    files.append("manifest.json")
    manifest["files"]["manifest.json"] = {"size": (out_dir / "manifest.json").stat().st_size, "mtime": now}

    summary = {
        "company": job.get("company"),
        "title": job.get("title"),
        "score": job.get("overall_score"),
        "apply_url": job.get("apply_url"),
        "files_count": len(files),
    }

    return {
        "ok": True,
        "packet_dir": str(out_dir),
        "files": files,
        "summary": summary,
        "manifest": manifest,
    }
