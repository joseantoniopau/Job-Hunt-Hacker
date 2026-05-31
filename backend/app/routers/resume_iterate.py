"""Endpoints for single-bullet resume rewriting (the "iterate" flow).

Both endpoints wrap ``tailoring.resume_iteration`` and return the
standard ``{ok, data}`` envelope used elsewhere in the API.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..tailoring import resume_iteration

log = logging.getLogger("jhh.routers.resume_iterate")

router = APIRouter(prefix="/api", tags=["resume"])


class ResumeIterateRequest(BaseModel):
    section_index: int = Field(..., ge=0)
    item_index: int = Field(..., ge=0)
    instruction: str = Field(..., min_length=1, max_length=2000)


class ResumeIterateAcceptRequest(BaseModel):
    section_index: int = Field(..., ge=0)
    item_index: int = Field(..., ge=0)
    new_text: str = Field(..., min_length=1)
    new_evidence_ids: list[int] = Field(default_factory=list)


@router.post("/resume/{resume_id}/iterate")
def iterate_bullet_route(resume_id: int, body: ResumeIterateRequest) -> dict:
    try:
        result = resume_iteration.iterate_bullet(
            resume_id=int(resume_id),
            section_index=int(body.section_index),
            item_index=int(body.item_index),
            instruction=body.instruction,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("resume iterate failed: %s", e)
        raise HTTPException(500, f"resume iterate failed: {e}")
    if not result.get("ok"):
        # 404 if the row doesn't exist; 422 otherwise (bad index / no prov)
        detail = result.get("detail") or "iterate failed"
        if "not found" in detail:
            raise HTTPException(404, detail)
        raise HTTPException(422, detail)
    return {"ok": True, "data": result}


@router.post("/resume/{resume_id}/accept-iteration")
def accept_iteration_route(resume_id: int, body: ResumeIterateAcceptRequest) -> dict:
    try:
        result = resume_iteration.accept_iteration(
            resume_id=int(resume_id),
            section_index=int(body.section_index),
            item_index=int(body.item_index),
            new_text=body.new_text,
            new_evidence_ids=body.new_evidence_ids,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("resume accept-iteration failed: %s", e)
        raise HTTPException(500, f"resume accept-iteration failed: {e}")
    if not result.get("ok"):
        detail = result.get("detail") or "accept failed"
        if "not found" in detail:
            raise HTTPException(404, detail)
        raise HTTPException(422, detail)
    return {"ok": True, "data": result}
