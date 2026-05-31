"""Followup email orchestration HTTP endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..tailoring import followup_emails

log = logging.getLogger("jhh.routers.followups")

router = APIRouter(prefix="/api", tags=["followups"])


class FollowupDraftBody(BaseModel):
    application_id: int
    stage: str


@router.post("/followup/draft")
def draft(body: FollowupDraftBody) -> dict:
    try:
        data = followup_emails.draft(application_id=body.application_id, stage=body.stage)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": data}


@router.get("/followup/stages")
def stages() -> dict:
    return {"ok": True, "data": followup_emails.list_stages()}
