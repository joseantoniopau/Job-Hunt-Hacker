"""Applications + packets HTTP API.

Also home of the generic notifications API (GET /api/notifications,
POST /api/notifications/{id}/read) — notifications were introduced for
application-deadline reminders, so they live next to the application routes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..applications import assisted_apply, packet_builder, pipeline
from ..config import settings
from ..db import audit, get_conn, tx
from ..models.schemas import ApplicationCreate, ApplicationUpdate, OK

log = logging.getLogger("jhh.routers.applications")

router = APIRouter(prefix="/api", tags=["applications"])


class FollowupBody(BaseModel):
    days: int
    status: Optional[str] = None


class PacketBuildBody(BaseModel):
    job_id: int
    options: Optional[dict] = None


class ApplicationPatchBody(ApplicationUpdate):
    """ApplicationUpdate plus deadline fields.

    deadline_at: epoch seconds (number or numeric string) OR an ISO-8601
    string ("2026-06-15", "2026-06-15T12:00:00Z", "...+02:00"); a naive ISO
    timestamp is interpreted as UTC. An empty string (or "none"/"null"/
    "clear") clears the deadline. Any change to deadline_at resets
    reminder_sent_at so the 48h reminder re-arms for the new deadline.

    deadline_source: free-text provenance ("manual", "jd", "email", ...);
    defaults to "manual" when a deadline is set without an explicit source,
    and is cleared alongside the deadline.
    """
    deadline_at: Optional[Union[float, str]] = None
    deadline_source: Optional[str] = None


def _parse_deadline(value: Union[float, str, None]) -> Optional[float]:
    """Normalize a deadline input to epoch seconds (UTC) or None (= clear).

    Accepts int/float epoch seconds, a numeric string, or an ISO-8601 string
    (date or datetime, 'Z' suffix supported). Raises ValueError on garbage.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in ("none", "null", "clear"):
        return None
    try:
        return float(s)
    except ValueError:
        pass
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(
            f"deadline_at must be epoch seconds or ISO-8601, got {value!r}"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


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
def patch_one(app_id: int, body: ApplicationPatchBody) -> dict:
    """Partial update. Accepts every ApplicationUpdate field (status, notes,
    next_followup_at, application_url) PLUS deadline_at / deadline_source —
    see ApplicationPatchBody for accepted deadline formats.

    Response: {ok, data: <full joined application row>} where the row now
    includes deadline_at (epoch), deadline_source and reminder_sent_at.
    """
    fields = body.model_dump(exclude_none=True)
    deadline_present = "deadline_at" in fields
    deadline_raw = fields.pop("deadline_at", None)
    deadline_source = fields.pop("deadline_source", None)
    if deadline_present or deadline_source is not None:
        try:
            parsed = _parse_deadline(deadline_raw) if deadline_present else None
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        sets: list[str] = []
        vals: list = []
        if deadline_present:
            sets.append("deadline_at = ?")
            vals.append(parsed)
            # Re-arm the one-shot reminder whenever the deadline changes.
            sets.append("reminder_sent_at = NULL")
            if deadline_source is None:
                deadline_source = "manual" if parsed is not None else ""
        sets.append("deadline_source = ?")
        vals.append(deadline_source or None)
        vals.append(int(app_id))
        with tx() as conn:
            cur = conn.execute(
                f"UPDATE application SET {', '.join(sets)} WHERE id = ?", vals
            )
            if cur.rowcount == 0:
                raise HTTPException(404, f"application {app_id} not found")
        try:
            audit("application_deadline_set", "application", int(app_id),
                  deadline_at=parsed, deadline_source=deadline_source or None)
        except Exception:
            pass
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


# --------- notifications ---------

@router.get("/notifications")
def list_notifications(
    unread_only: bool = False,
    kind: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List in-app notifications, newest first.

    Query params: unread_only (bool, default false), kind (exact match, e.g.
    'deadline_reminder'), limit/offset for paging.

    Response: {ok, data: [{id, ts, kind, title, body, read (0/1),
    target_type, target_id}], count, unread_count} — unread_count is the
    global unread total (for a badge), independent of the filters.
    """
    conn = get_conn()
    where: list[str] = []
    params: list = []
    if unread_only:
        where.append("read = 0")
    if kind:
        where.append("kind = ?")
        params.append(kind)
    sql = "SELECT id, ts, kind, title, body, read, target_type, target_id FROM notification"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    rows = conn.execute(sql, params).fetchall()
    unread = conn.execute(
        "SELECT COUNT(*) FROM notification WHERE read = 0"
    ).fetchone()[0]
    return {
        "ok": True,
        "data": [dict(r) for r in rows],
        "count": len(rows),
        "unread_count": int(unread),
    }


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int) -> dict:
    """Mark one notification as read. Idempotent — re-marking an already-read
    row still returns ok. Response: {ok, detail}; 404 when the id is unknown.
    """
    with tx() as conn:
        cur = conn.execute(
            "UPDATE notification SET read = 1 WHERE id = ?",
            (int(notification_id),),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, f"notification {notification_id} not found")
    try:
        audit("notification_read", "notification", int(notification_id))
    except Exception:
        pass
    return {"ok": True, "detail": "read"}
