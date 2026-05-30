"""Cover letter generation + listing + download."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..config import settings
from ..db import get_conn, row_to_dict
from ..models.schemas import CoverLetterRequest
from ..tailoring import cover_letter as cover_letter_svc
from ..utils.text import slug

log = logging.getLogger("jhh.routers.cover_letter")

router = APIRouter(prefix="/api", tags=["cover_letter"])


@router.post("/cover-letter")
def generate(body: CoverLetterRequest) -> dict:
    try:
        result = cover_letter_svc.generate(job_id=body.job_id, tone=body.tone)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        log.warning("cover letter failed: %s", e)
        raise HTTPException(500, f"cover letter failed: {e}")
    return {"ok": True, "data": result}


@router.get("/cover-letters")
def list_letters(job_id: Optional[int] = None, limit: int = 50) -> dict:
    conn = get_conn()
    if job_id is not None:
        rows = conn.execute(
            "SELECT id, job_id, length(text) AS char_count, created_at FROM cover_letter "
            "WHERE job_id = ? ORDER BY created_at DESC LIMIT ?",
            (job_id, int(limit)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, job_id, length(text) AS char_count, created_at FROM cover_letter "
            "ORDER BY created_at DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return {"ok": True, "data": [row_to_dict(r) for r in rows]}


@router.get("/cover-letter/{letter_id}")
def get_letter(letter_id: int) -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM cover_letter WHERE id = ?", (letter_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "cover letter not found")
    return {"ok": True, "data": row_to_dict(row)}


@router.get("/cover-letter/{letter_id}/download")
def download_letter(letter_id: int):
    conn = get_conn()
    row = conn.execute("SELECT id, job_id, text FROM cover_letter WHERE id = ?", (letter_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "cover letter not found")
    text = row["text"] or ""
    name = f"cover_letter_{letter_id}.txt"
    out_path = settings.packets_dir / name
    out_path.write_text(text, encoding="utf-8")
    return FileResponse(out_path, filename=name, media_type="text/plain")
