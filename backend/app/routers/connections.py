"""Networking / connections HTTP endpoints."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import networking

log = logging.getLogger("jhh.routers.connections")

router = APIRouter(prefix="/api", tags=["connections"])


class ConnectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    relationship: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    contact: Optional[str] = None
    notes: Optional[str] = None
    additional_companies: Optional[list[dict]] = None


class ConnectionUpdate(BaseModel):
    name: Optional[str] = None
    relationship: Optional[str] = None
    company: Optional[str] = None
    role: Optional[str] = None
    contact: Optional[str] = None
    notes: Optional[str] = None
    last_contacted_at: Optional[float] = None


@router.post("/connections")
def create(body: ConnectionCreate) -> dict:
    try:
        cid = networking.add_connection(
            name=body.name,
            relationship=body.relationship,
            company=body.company,
            role=body.role,
            contact=body.contact,
            notes=body.notes,
            additional_companies=body.additional_companies,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": networking.get_connection(cid)}


@router.get("/connections")
def listing(company: Optional[str] = None, limit: int = 500, offset: int = 0) -> dict:
    rows = networking.list_connections(company=company, limit=limit, offset=offset)
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/connections/refer/{company_name}")
def refer_at(company_name: str) -> dict:
    rows = networking.who_could_refer_at(company_name)
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/connections/suggest")
def suggest(max_jobs: int = 10) -> dict:
    rows = networking.suggest_outreach(max_jobs=max_jobs)
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/connections/{connection_id}")
def get_one(connection_id: int) -> dict:
    row = networking.get_connection(connection_id)
    if not row:
        raise HTTPException(404, f"connection {connection_id} not found")
    return {"ok": True, "data": row}


@router.patch("/connections/{connection_id}")
def patch_one(connection_id: int, body: ConnectionUpdate) -> dict:
    fields = body.model_dump(exclude_none=True)
    try:
        row = networking.update_connection(connection_id, fields)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "data": row}


@router.delete("/connections/{connection_id}")
def delete_one(connection_id: int) -> dict:
    ok = networking.delete_connection(connection_id)
    if not ok:
        raise HTTPException(404, f"connection {connection_id} not found")
    return {"ok": True, "detail": "deleted"}
