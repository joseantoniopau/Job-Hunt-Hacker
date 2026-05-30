"""Applications + packets HTTP API."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..applications import assisted_apply, packet_builder, pipeline
from ..config import settings
from ..models.schemas import ApplicationCreate, ApplicationUpdate, OK

log = logging.getLogger("jhh.routers.applications")

router = APIRouter(prefix="/api", tags=["applications"])


class FollowupBody(BaseModel):
    days: int
    status: Optional[str] = None


class PacketBuildBody(BaseModel):
    job_id: int
    options: Optional[dict] = None


# --------- applications ---------

@router.post("/applications")
def create(body: ApplicationCreate) -> dict:
    try:
        app_id = pipeline.create_application(
            job_id=int(body.job_id),
            status=body.status or "saved",
            notes=body.notes or "",
            mode=body.mode,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": pipeline.get_application(app_id)}


@router.get("/applications")
def listing(status: Optional[str] = None, limit: int = 100, offset: int = 0) -> dict:
    try:
        rows = pipeline.list_applications(status=status, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/applications/board")
def board() -> dict:
    return {"ok": True, "data": pipeline.pipeline_board()}


@router.get("/applications/{app_id}")
def get_one(app_id: int) -> dict:
    row = pipeline.get_application(app_id)
    if not row:
        raise HTTPException(404, f"application {app_id} not found")
    return {"ok": True, "data": row}


@router.patch("/applications/{app_id}")
def patch_one(app_id: int, body: ApplicationUpdate) -> dict:
    fields = body.model_dump(exclude_none=True)
    try:
        out = pipeline.update_application(app_id, fields)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": out}


@router.delete("/applications/{app_id}")
def delete_one(app_id: int) -> dict:
    ok = pipeline.delete_application(app_id, hard=False)
    if not ok:
        raise HTTPException(404, f"application {app_id} not found")
    return {"ok": True, "detail": "archived"}


@router.post("/applications/{app_id}/followup")
def set_followup(app_id: int, body: FollowupBody) -> dict:
    try:
        out = pipeline.set_followup(app_id, days=int(body.days), status=body.status)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "data": out}


# --------- packets ---------

@router.post("/packet/build")
def build_packet(body: PacketBuildBody) -> dict:
    """Build packet + create application row at status=prepared."""
    res = assisted_apply.prepare(int(body.job_id), body.options or {})
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "packet build failed")
    return {"ok": True, "data": res}


def _packet_dir_for(app_id: int) -> Path:
    """Resolve packet dir from application -> job."""
    app = pipeline.get_application(app_id)
    if not app:
        raise HTTPException(404, f"application {app_id} not found")
    job_id = app.get("job_id")
    if not job_id:
        raise HTTPException(404, "application has no job_id")
    # walk packets dir for matching prefix
    prefix = f"packet_{int(job_id)}_"
    if not settings.packets_dir.exists():
        raise HTTPException(404, "no packets directory")
    for entry in settings.packets_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix):
            return entry
    raise HTTPException(404, "packet directory not found; rebuild via POST /api/packet/build")


@router.get("/packet/{app_id}/manifest")
def packet_manifest(app_id: int) -> dict:
    d = _packet_dir_for(app_id)
    mp = d / "manifest.json"
    if not mp.exists():
        raise HTTPException(404, "manifest missing")
    import json as _json
    try:
        return {"ok": True, "data": _json.loads(mp.read_text(encoding="utf-8"))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"manifest parse error: {exc}")


@router.get("/packet/{app_id}/file/{filename}")
def packet_file(app_id: int, filename: str) -> FileResponse:
    # validate filename — no traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")
    d = _packet_dir_for(app_id)
    f = d / filename
    try:
        f_resolved = f.resolve()
        d_resolved = d.resolve()
        if not str(f_resolved).startswith(str(d_resolved)):
            raise HTTPException(400, "invalid path")
    except Exception:
        raise HTTPException(400, "invalid path")
    if not f.exists() or not f.is_file():
        raise HTTPException(404, f"{filename} not found in packet")
    return FileResponse(str(f), filename=filename)
