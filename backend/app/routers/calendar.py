"""Calendar router. Google Calendar + ICS fallback."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..config import settings
from ..db import get_conn, row_to_dict
from ..integrations import calendar_google, ics

log = logging.getLogger("jhh.routers.calendar")

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


class SlotsBody(BaseModel):
    window_days: int = 7
    slot_minutes: int = 30
    work_hours_start: int = 9
    work_hours_end: int = 17
    # User-drawn weekly availability grid: { "mon": [9,10,11,...], "tue": [...] }
    # If provided, slot suggestions are filtered to hours marked available.
    availability: Optional[dict] = None


class EventBody(BaseModel):
    title: str
    start: str  # ISO
    end: str  # ISO
    attendees: list[str] = []
    description: str = ""
    application_id: Optional[int] = None
    confirmed: bool = False


@router.get("/status")
def status() -> dict:
    return {"ok": True, "data": calendar_google.status()}


@router.post("/slots")
def slots(body: SlotsBody) -> dict:
    # Honor the user's timezone so work hours are local, not server-UTC.
    user_tz = None
    try:
        row = get_conn().execute("SELECT timezone FROM user_profile WHERE id = 1").fetchone()
        if row is not None:
            user_tz = row["timezone"]
    except Exception:
        user_tz = None
    out = calendar_google.find_slots(
        window_days=int(body.window_days),
        slot_minutes=int(body.slot_minutes),
        work_hours=(int(body.work_hours_start), int(body.work_hours_end)),
        tz=user_tz,
    )
    # If the user supplied an availability grid, filter the suggested slots
    # to only those whose weekday+hour appears in their available hours.
    if body.availability and isinstance(body.availability, dict) and out:
        avail = {str(k).lower()[:3]: set(int(h) for h in (v or []))
                 for k, v in body.availability.items()}
        from datetime import datetime
        filtered = []
        for s in out:
            try:
                dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
                weekday = dt.strftime("%a").lower()[:3]
                if weekday in avail and dt.hour in avail[weekday]:
                    filtered.append(s)
            except Exception:
                filtered.append(s)
        out = filtered
    return {"ok": True, "data": out}


@router.get("/events")
def list_events(application_id: Optional[int] = None,
                limit: int = 100) -> dict:
    """List stored calendar events. Used by the Calendar tab to populate
    the upcoming-events table.
    """
    conn = get_conn()
    sql = "SELECT * FROM calendar_event"
    params: list = []
    if application_id is not None:
        sql += " WHERE application_id = ?"
        params.append(int(application_id))
    sql += " ORDER BY start_time ASC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    out = []
    from datetime import datetime, timezone
    for r in rows:
        d = row_to_dict(r) or {}
        # UI-friendly aliases
        st = d.get("start_time")
        en = d.get("end_time")
        if isinstance(st, (int, float)):
            d["start_at"] = datetime.fromtimestamp(float(st), tz=timezone.utc).isoformat()
        if isinstance(en, (int, float)):
            d["end_at"] = datetime.fromtimestamp(float(en), tz=timezone.utc).isoformat()
        if d.get("attendees"):
            d["with"] = d["attendees"]
        out.append(d)
    return {"ok": True, "data": out, "events": out}


@router.post("/event")
def create_event(body: EventBody) -> dict:
    if calendar_google.is_configured() and body.confirmed:
        res = calendar_google.create_event(
            title=body.title,
            start_iso=body.start,
            end_iso=body.end,
            attendees=body.attendees,
            description=body.description,
            application_id=body.application_id,
            confirmed=True,
        )
        if not res.get("ok"):
            raise HTTPException(400, res.get("detail") or "calendar create failed")
        return {"ok": True, "data": res}
    # fallback: persist + return ICS path
    from ..db import tx
    raw_ics = ics.to_ics({
        "title": body.title,
        "start": body.start,
        "end": body.end,
        "attendees": body.attendees,
        "description": body.description,
    })
    fname = f"event_{int(time.time())}_{body.title[:24].replace(' ', '_')}.ics"
    path = settings.data_dir / "calendar_ics" / fname
    ics.save_to_file({
        "title": body.title,
        "start": body.start,
        "end": body.end,
        "attendees": body.attendees,
        "description": body.description,
    }, path)
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO calendar_event (application_id, title, start_time, end_time, status, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                body.application_id,
                body.title,
                _to_ts(body.start),
                _to_ts(body.end),
                "tentative" if not body.confirmed else "confirmed",
                __import__("json").dumps({"ics_path": str(path)}),
            ),
        )
        eid = int(cur.lastrowid)
    return {
        "ok": True,
        "data": {
            "event_id": eid,
            "ics_path": str(path),
            "ics": raw_ics,
            "detail": "Saved as ICS (Google Calendar not authorized or not confirmed)",
        },
    }


def _to_ts(iso: str) -> float:
    from datetime import datetime, timezone
    s = (iso or "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return time.time()


@router.get("/{event_id}/ics")
def event_ics(event_id: int) -> Response:
    conn = get_conn()
    row = conn.execute("SELECT * FROM calendar_event WHERE id = ?", (int(event_id),)).fetchone()
    if not row:
        raise HTTPException(404, f"calendar event {event_id} not found")
    rec = row_to_dict(row)
    raw = rec.get("raw_json") or {}
    # Prefer the saved .ics file written at create time — same UID + DTSTAMP
    # the original event had — so repeat fetches are byte-identical and the
    # downloaded file matches whatever calendar apps cached earlier.
    if isinstance(raw, dict) and raw.get("ics_path"):
        p = Path(raw["ics_path"])
        if p.exists():
            return Response(
                p.read_text(encoding="utf-8"),
                media_type="text/calendar",
                headers={"Content-Disposition": f'attachment; filename="event_{event_id}.ics"'},
            )
    # Fallback: regenerate fresh (new UID + DTSTAMP) only when the saved
    # file is missing — e.g. data dir wiped.
    start = rec.get("start_time") or time.time()
    end = rec.get("end_time") or (start + 1800)
    from datetime import datetime, timezone
    body = {
        "title": rec.get("title") or "Interview",
        "start": datetime.fromtimestamp(float(start), tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(float(end), tz=timezone.utc).isoformat(),
        "description": "",
    }
    raw_ics = ics.to_ics(body)
    return Response(raw_ics, media_type="text/calendar")
