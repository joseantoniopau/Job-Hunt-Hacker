"""Career vault endpoints — list/update claims, contradiction scan, retrieval."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..models.schemas import ClaimUpdate, OK
from ..services import career_vault, contradiction_detector

log = logging.getLogger("jhh.evidence")

router = APIRouter(prefix="/api/vault", tags=["vault"])


class RetrieveRequest(BaseModel):
    text: str
    top: int = 15


@router.get("/summary")
def summary() -> dict:
    return {"ok": True, "data": career_vault.summary()}


@router.get("/claims")
def list_claims(type: Optional[str] = None,
                allowed_only: bool = False,
                verified_only: bool = False,
                source_id: Optional[int] = None) -> dict:
    rows = career_vault.list_claims(
        source_id=source_id,
        claim_type=type,
        allowed_only=allowed_only,
        verified_only=verified_only,
    )
    return {"ok": True, "data": rows}


@router.patch("/claims/{claim_id}")
def patch_claim(claim_id: int, body: ClaimUpdate) -> dict:
    fields = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    try:
        updated = career_vault.update_claim(claim_id, fields)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "data": updated}


@router.delete("/claims/{claim_id}")
def delete_claim(claim_id: int) -> OK:
    n = career_vault.delete_claim(claim_id)
    if not n:
        raise HTTPException(404, "claim not found")
    return OK(detail=f"deleted claim {claim_id}")


@router.post("/contradictions/scan")
def scan_contradictions() -> dict:
    findings = contradiction_detector.find_contradictions()
    return {"ok": True, "data": {
        "count": len(findings),
        "findings": findings,
    }}


@router.post("/retrieve")
def retrieve(body: RetrieveRequest) -> dict:
    rows = career_vault.retrieve_for_job(body.text, top=body.top)
    return {"ok": True, "data": rows}
