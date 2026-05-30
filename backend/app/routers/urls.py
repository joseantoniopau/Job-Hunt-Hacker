"""POST /api/urls/preview — fetch a URL and return parsed text without ingesting."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..models.schemas import URLIngestRequest
from ..services import url_ingestion
from ..utils.text import truncate

log = logging.getLogger("jhh.evidence")

router = APIRouter(prefix="/api/urls", tags=["urls"])


@router.post("/preview")
def preview(body: URLIngestRequest) -> dict:
    if not body.url:
        raise HTTPException(400, "url required")
    result = url_ingestion.fetch_url(body.url)
    if "error" in result:
        raise HTTPException(400, result["error"])
    text = result.get("text") or ""
    return {"ok": True, "data": {
        "url": result.get("url"),
        "title": result.get("title"),
        "content_type": result.get("content_type"),
        "char_count": len(text),
        "text": truncate(text, 6000),
    }}
