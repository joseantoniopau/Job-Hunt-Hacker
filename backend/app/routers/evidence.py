"""Evidence ingestion endpoints — file upload, text paste, URL fetch."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..config import settings
from ..db import audit
from ..models.schemas import OK, TextIngestRequest, URLIngestRequest
from ..security.uploads import validate_upload
from ..services import (
    career_vault,
    document_parser,
    evidence_extractor,
    url_ingestion,
)

log = logging.getLogger("jhh.evidence")

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


def _safe_filename(name: str) -> str:
    keep = "".join(c for c in (name or "upload.bin") if c.isalnum() or c in "._- ")
    return keep.strip() or "upload.bin"


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    source_type: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
) -> dict:
    """Accept a file, parse it, store as evidence_source, extract claims."""
    if not file or not file.filename:
        raise HTTPException(400, "no file provided")
    # Cheap header check before consuming bytes.
    validate_upload(file, ("pdf", "docx", "doc", "md", "txt", "rtf", "html"))
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    # Safety-net byte-count check + MIME probe now that we have the body.
    validate_upload(file, ("pdf", "docx", "doc", "md", "txt", "rtf", "html"), raw_bytes=raw)

    safe_name = _safe_filename(file.filename)
    dest = Path(settings.uploads_dir) / f"{int(time.time())}_{safe_name}"
    try:
        dest.write_bytes(raw)
    except Exception as e:  # noqa: BLE001
        log.warning("upload write failed: %s", e)
        raise HTTPException(500, f"could not write upload: {e}")

    try:
        parsed = document_parser.parse_file(dest)
    except RuntimeError as e:
        raise HTTPException(415, str(e))
    text = parsed.get("text") or ""
    metadata = parsed.get("metadata") or {}

    stype = (source_type or "").strip() or _guess_source_type(safe_name)

    source_id = career_vault.add_source(
        source_type=stype,
        title=title or safe_name,
        filename=safe_name,
        url=None,
        raw_text=text,
        parsed_json={"metadata": metadata},
    )

    # Only run extraction if claims for this source don't already exist
    existing = career_vault.list_claims(source_id=source_id)
    if existing:
        return {"ok": True, "data": {
            "source_id": source_id,
            "claims_extracted": 0,
            "claims_total": len(existing),
            "deduped": True,
        }}

    claims = evidence_extractor.extract_claims(source_id, text, stype)
    inserted = career_vault.add_claims(source_id, claims)
    audit("evidence_upload", "evidence_source", source_id,
          filename=safe_name, claims=len(inserted))
    return {"ok": True, "data": {
        "source_id": source_id,
        "claims_extracted": len(inserted),
    }}


@router.post("/text")
def ingest_text(body: TextIngestRequest) -> dict:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    source_id = career_vault.add_source(
        source_type=body.source_type or "manual_paste",
        title=body.title or "Pasted text",
        raw_text=text,
    )
    existing = career_vault.list_claims(source_id=source_id)
    if existing:
        return {"ok": True, "data": {
            "source_id": source_id,
            "claims_extracted": 0,
            "claims_total": len(existing),
            "deduped": True,
        }}
    claims = evidence_extractor.extract_claims(source_id, text, body.source_type)
    inserted = career_vault.add_claims(source_id, claims)
    return {"ok": True, "data": {
        "source_id": source_id,
        "claims_extracted": len(inserted),
    }}


@router.post("/url")
def ingest_url(body: URLIngestRequest) -> dict:
    if not body.url:
        raise HTTPException(400, "url required")
    fetched = url_ingestion.fetch_url(body.url)
    if "error" in fetched:
        raise HTTPException(400, fetched["error"])
    text = fetched.get("text") or ""
    if not text.strip():
        raise HTTPException(422, "no readable text at url")
    stype = (body.source_type or _guess_source_type_for_url(body.url))
    source_id = career_vault.add_source(
        source_type=stype,
        title=fetched.get("title") or body.url,
        url=fetched.get("url") or body.url,
        raw_text=text,
        parsed_json={"content_type": fetched.get("content_type"),
                     "fetched_at": fetched.get("fetched_at")},
    )
    existing = career_vault.list_claims(source_id=source_id)
    if existing:
        return {"ok": True, "data": {
            "source_id": source_id,
            "claims_extracted": 0,
            "claims_total": len(existing),
            "deduped": True,
        }}
    claims = evidence_extractor.extract_claims(source_id, text, stype)
    inserted = career_vault.add_claims(source_id, claims)
    return {"ok": True, "data": {
        "source_id": source_id,
        "claims_extracted": len(inserted),
        "title": fetched.get("title"),
    }}


@router.get("/sources")
def list_sources() -> dict:
    return {"ok": True, "data": career_vault.list_sources()}


@router.get("/sources/{source_id}")
def get_source(source_id: int) -> dict:
    src = career_vault.get_source(source_id)
    if src is None:
        raise HTTPException(404, "source not found")
    return {"ok": True, "data": src}


@router.delete("/sources/{source_id}")
def delete_source(source_id: int) -> OK:
    n = career_vault.delete_source(source_id)
    if not n:
        raise HTTPException(404, "source not found")
    return OK(detail=f"deleted source {source_id}")


# ---- helpers ----

def _guess_source_type(filename: str) -> str:
    lower = (filename or "").lower()
    if any(w in lower for w in ("resume", "cv")):
        return "resume"
    if "linkedin" in lower:
        return "linkedin"
    if any(lower.endswith(s) for s in (".pdf", ".docx", ".doc")):
        return "document"
    if any(lower.endswith(s) for s in (".html", ".htm")):
        return "webpage"
    return "document"


def _guess_source_type_for_url(url: str) -> str:
    u = (url or "").lower()
    if "linkedin.com" in u:
        return "linkedin"
    if "github.com" in u:
        return "github"
    if u.endswith(".pdf"):
        return "pdf_url"
    return "webpage"
