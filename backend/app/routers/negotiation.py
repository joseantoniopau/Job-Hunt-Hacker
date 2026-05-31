"""Negotiation prep HTTP endpoints."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..tailoring import negotiation

log = logging.getLogger("jhh.routers.negotiation")

router = APIRouter(prefix="/api", tags=["negotiation"])


class NegotiationScriptBody(BaseModel):
    application_id: int
    offer_base: int
    offer_total: Optional[int] = None
    currency: str = "USD"


@router.post("/negotiation/script")
def script(body: NegotiationScriptBody) -> dict:
    try:
        data = negotiation.generate(
            application_id=body.application_id,
            offer_base=body.offer_base,
            offer_total=body.offer_total or body.offer_base,
            currency=body.currency or "USD",
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": data}


@router.get("/negotiation/compare")
def compare(role: str, offer: int, location: Optional[str] = None, currency: str = "USD") -> dict:
    if not role or not role.strip():
        raise HTTPException(400, "role is required")
    if offer is None or offer <= 0:
        raise HTTPException(400, "offer must be positive")
    try:
        data = negotiation.compare_to_market(
            offer=offer, role=role, location=location, currency=currency
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": data}
