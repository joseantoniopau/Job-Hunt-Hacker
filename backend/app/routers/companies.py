"""Company research HTTP endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..services import company_research

log = logging.getLogger("jhh.routers.companies")

router = APIRouter(prefix="/api", tags=["companies"])


@router.get("/companies")
def listing(limit: int = 200, offset: int = 0) -> dict:
    if limit <= 0 or limit > 1000:
        raise HTTPException(400, "limit must be between 1 and 1000")
    rows = company_research.list_companies(limit=limit, offset=offset)
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/companies/{company_name}")
def get_one(company_name: str) -> dict:
    try:
        data = company_research.enrich(company_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": data}
