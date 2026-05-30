"""Resume CRUD + upload (master) + tailor + download.

Upload: persists to ``resume_document`` (is_master=True) AND also pushes the
text through the evidence pipeline so the Career Vault gets populated
automatically from the user's master resume.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..config import settings
from ..db import audit, get_conn, row_to_dict, tx
from ..models.schemas import OK, ResumeTailorRequest
from ..tailoring import resume_tailor
from ..utils.text import slug

log = logging.getLogger("jhh.routers.resume")

router = APIRouter(prefix="/api", tags=["resume"])


_VALID_FORMATS = {"md", "txt", "docx", "pdf"}


def _safe_filename(name: str) -> str:
    keep = "".join(c for c in (name or "resume.bin") if c.isalnum() or c in "._- ")
    return keep.strip() or "resume.bin"


# ---------- upload (master resume) ----------

@router.post("/resume/upload")
async def upload_resume(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    is_master: bool = Form(True),
) -> dict:
    if not file or not file.filename:
        raise HTTPException(400, "no file provided")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")

    safe_name = _safe_filename(file.filename)
    dest = Path(settings.uploads_dir) / f"{int(time.time())}_{safe_name}"
    try:
        dest.write_bytes(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"could not save upload: {e}")

    # Parse
    try:
        from ..services.document_parser import parse_file
        parsed = parse_file(dest)
    except RuntimeError as e:
        raise HTTPException(415, str(e))
    text = (parsed.get("text") or "").strip()
    metadata = parsed.get("metadata") or {}

    if not text:
        raise HTTPException(422, "could not extract text from resume")

    now = time.time()
    suffix = (Path(safe_name).suffix or "").lower().lstrip(".") or metadata.get("parser") or "txt"
    parsed_json = {"metadata": metadata}

    import json as _j
    with tx() as conn:
        # If marking master, demote any prior master
        if is_master:
            conn.execute("UPDATE resume_document SET is_master = 0 WHERE is_master = 1")
        cur = conn.execute(
            "INSERT INTO resume_document (filename, file_type, raw_text, parsed_json, is_master, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (safe_name, suffix, text, _j.dumps(parsed_json), 1 if is_master else 0, now),
        )
        new_id = int(cur.lastrowid)

    # Push into evidence vault (so claims get extracted automatically)
    evidence_info: dict = {}
    try:
        from ..services import career_vault, evidence_extractor  # type: ignore
        source_id = career_vault.add_source(
            source_type="resume",
            title=title or safe_name,
            filename=safe_name,
            raw_text=text,
            parsed_json={"metadata": metadata, "resume_document_id": new_id},
        )
        existing = career_vault.list_claims(source_id=source_id)
        if not existing:
            claims = evidence_extractor.extract_claims(source_id, text, "resume") or []
            inserted = career_vault.add_claims(source_id, claims)
            evidence_info = {"source_id": source_id, "claims_extracted": len(inserted)}
        else:
            evidence_info = {"source_id": source_id, "claims_extracted": 0, "deduped": True}
    except Exception as e:  # noqa: BLE001
        log.warning("evidence ingestion from resume failed: %s", e)
        evidence_info = {"error": str(e)}

    audit("resume_uploaded", "resume_document", new_id, filename=safe_name, is_master=is_master)
    return {"ok": True, "data": {
        "id": new_id,
        "filename": safe_name,
        "file_type": suffix,
        "is_master": bool(is_master),
        "char_count": len(text),
        "evidence": evidence_info,
    }}


# ---------- list ----------

@router.get("/resumes")
def list_resumes() -> dict:
    """Flat list of all resumes (master + tailored) so UI can iterate
    without knowing about the two underlying tables.

    Each row carries `resume_type` ("master" or whatever tailored type
    the user picked) plus `kind` for explicit disambiguation. We also
    surface `char_count` for the table summary column.
    """
    conn = get_conn()
    docs = [row_to_dict(r) for r in conn.execute(
        "SELECT id, filename, file_type, is_master, created_at, length(raw_text) AS char_count "
        "FROM resume_document ORDER BY created_at DESC"
    ).fetchall()]
    tailored = [row_to_dict(r) for r in conn.execute(
        "SELECT id, job_id, base_resume_id, resume_type, "
        "docx_path, pdf_path, created_at FROM tailored_resume ORDER BY created_at DESC"
    ).fetchall()]
    flat: list[dict] = []
    for d in docs:
        d2 = dict(d)
        d2["kind"] = "master"
        d2.setdefault("resume_type", "master")
        flat.append(d2)
    for t in tailored:
        t2 = dict(t)
        t2["kind"] = "tailored"
        flat.append(t2)
    return {"ok": True, "data": flat,
            # Keep the structured shape too for any caller that wants it
            "master_documents": docs, "tailored": tailored}


@router.get("/resumes/{resume_id}")
def get_resume(resume_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM resume_document WHERE id = ?", (resume_id,)).fetchone()
    if row is not None:
        d = row_to_dict(row) or {}
        # Alias raw_text → markdown so the Resume Lab preview pane works on master docs
        if d.get("raw_text") and not d.get("markdown"):
            d["markdown"] = d["raw_text"]
        return {"ok": True, "data": {"kind": "master", **d}}
    row = conn.execute("SELECT * FROM tailored_resume WHERE id = ?", (resume_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "resume not found")
    d = row_to_dict(row) or {}
    return {"ok": True, "data": {"kind": "tailored", **d}}


# ---------- tailor ----------

@router.post("/resume/tailor")
def tailor(body: ResumeTailorRequest) -> dict:
    try:
        result = resume_tailor.tailor_resume(
            job_id=body.job_id,
            resume_type=body.resume_type,
            base_resume_id=body.base_resume_id,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        log.warning("tailor failed: %s", e)
        raise HTTPException(500, f"tailor failed: {e}")
    # Strip None pdf_path so callers can detect absence cleanly.
    if not result.get("pdf_path"):
        result.pop("pdf_path", None)
    return {"ok": True, "data": result}


# ---------- download ----------

@router.get("/resume/{resume_id}/download/{fmt}")
def download_resume(resume_id: int, fmt: str):
    fmt = (fmt or "").lower().strip()
    if fmt not in _VALID_FORMATS:
        raise HTTPException(400, f"invalid format; must be one of {sorted(_VALID_FORMATS)}")
    conn = get_conn()
    row = conn.execute("SELECT * FROM tailored_resume WHERE id = ?", (resume_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "tailored resume not found")
    d = row_to_dict(row) or {}

    # PDF/DOCX have stored paths; md/txt we serve from db or written files.
    if fmt == "docx":
        path = d.get("docx_path")
        if not path or not Path(path).exists():
            raise HTTPException(404, "docx not available")
        return FileResponse(path, filename=Path(path).name,
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if fmt == "pdf":
        path = d.get("pdf_path")
        if not path or not Path(path).exists():
            return JSONResponse(status_code=404,
                                content={"ok": False, "detail": "pdf not available (install reportlab or weasyprint)"})
        return FileResponse(path, filename=Path(path).name, media_type="application/pdf")
    if fmt == "md":
        # Prefer a written file; else write to a temp under resumes_dir.
        title_slug = slug(f"resume_{resume_id}") or f"resume_{resume_id}"
        md_path = settings.resumes_dir / f"resume_{resume_id}_{title_slug}.md"
        if not md_path.exists():
            md_path.write_text(d.get("markdown") or "", encoding="utf-8")
        return FileResponse(md_path, filename=md_path.name, media_type="text/markdown")
    # txt
    title_slug = slug(f"resume_{resume_id}") or f"resume_{resume_id}"
    txt_path = settings.resumes_dir / f"resume_{resume_id}_{title_slug}.txt"
    if not txt_path.exists():
        txt_path.write_text(d.get("plain_text") or "", encoding="utf-8")
    return FileResponse(txt_path, filename=txt_path.name, media_type="text/plain")


# ---------- delete ----------

@router.delete("/resume/{resume_id}")
def delete_resume(resume_id: int) -> OK:
    conn = get_conn()
    # Try master first
    row = conn.execute("SELECT id FROM resume_document WHERE id = ?", (resume_id,)).fetchone()
    if row is not None:
        with tx() as c:
            c.execute("DELETE FROM resume_document WHERE id = ?", (resume_id,))
        audit("resume_deleted", "resume_document", resume_id)
        return OK(detail=f"deleted master resume {resume_id}")
    row = conn.execute("SELECT id, docx_path, pdf_path FROM tailored_resume WHERE id = ?", (resume_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "resume not found")
    # Best-effort file cleanup
    for col in ("docx_path", "pdf_path"):
        p = row[col]
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
    with tx() as c:
        c.execute("DELETE FROM tailored_resume WHERE id = ?", (resume_id,))
    audit("resume_deleted", "tailored_resume", resume_id)
    return OK(detail=f"deleted tailored resume {resume_id}")
