"""Google Calendar via REST.

Shares OAuth tokens with gmail.py (same scopes include calendar.events).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ..config import settings
from ..db import audit, get_conn, tx
from . import oauth_tokens
from .gmail import _access_token, is_configured  # reuse the shared token flow

log = logging.getLogger("jhh.integrations.calendar")

CAL_API = "https://www.googleapis.com/calendar/v3"


def status() -> dict:
    configured = is_configured()
    tokens = oauth_tokens.load_tokens() if configured else {}
    return {
        "configured": configured,
        "authorized": bool(tokens),
        "expires_at": tokens.get("expires_at") if tokens else None,
    }


# -------- low level --------

def _get(path: str, params: dict | None = None) -> dict:
    tok = _access_token()
    if not tok:
        raise RuntimeError("no Google OAuth token; authorize first")
    r = httpx.get(
        f"{CAL_API}{path}",
        params=params or {},
        headers={"Authorization": f"Bearer {tok}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    tok = _access_token()
    if not tok:
        raise RuntimeError("no Google OAuth token; authorize first")
    r = httpx.post(
        f"{CAL_API}{path}",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# -------- availability --------

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _parse(s: str) -> datetime:
    s = (s or "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)


def list_availability(start_iso: str, end_iso: str) -> list[dict]:
    """Return free time slots derived from primary-calendar busy events.

    Falls back to "(calendar not configured)" stub if no oauth.
    """
    if not is_configured():
        return [{"detail": "(calendar not configured)"}]
    try:
        body = {
            "timeMin": start_iso,
            "timeMax": end_iso,
            "items": [{"id": "primary"}],
        }
        tok = _access_token()
        if not tok:
            return [{"detail": "(not authorized)"}]
        r = httpx.post(
            f"{CAL_API}/freeBusy",
            json=body,
            headers={"Authorization": f"Bearer {tok}"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        busy = ((data.get("calendars") or {}).get("primary") or {}).get("busy") or []
        # invert busy into free
        start_dt = _parse(start_iso)
        end_dt = _parse(end_iso)
        free: list[dict] = []
        cursor = start_dt
        for b in sorted(busy, key=lambda x: x.get("start", "")):
            b_start = _parse(b.get("start", ""))
            b_end = _parse(b.get("end", ""))
            if b_start > cursor:
                free.append({"start": _iso(cursor), "end": _iso(b_start)})
            cursor = max(cursor, b_end)
        if cursor < end_dt:
            free.append({"start": _iso(cursor), "end": _iso(end_dt)})
        return free
    except Exception as exc:  # noqa: BLE001
        log.warning("free/busy lookup failed: %s", exc)
        return [{"detail": f"error: {type(exc).__name__}: {exc}"}]


def _tzinfo(tz: str | None):
    """Resolve an IANA tz name to tzinfo, falling back to UTC. The work-hour
    window is meaningful in the USER's local time, not the server's UTC."""
    if not tz or str(tz).upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(tz))
    except Exception:
        return timezone.utc


def find_slots(window_days: int = 7, slot_minutes: int = 30,
               work_hours: tuple[int, int] = (9, 17), tz: str | None = None) -> list[str]:
    """Suggest interview slots within window. Returns ISO strings (slot
    starts, UTC). Work-hours + weekday are evaluated in the user's timezone
    `tz` (IANA name) so 9-17 means 9-17 *local*, not UTC."""
    zone = _tzinfo(tz)

    def _in_window(cursor_utc) -> bool:
        local = cursor_utc.astimezone(zone)
        return local.weekday() < 5 and work_hours[0] <= local.hour < work_hours[1]

    now = datetime.now(timezone.utc)
    start_dt = now + timedelta(hours=1)
    end_dt = now + timedelta(days=int(window_days))
    free_blocks = list_availability(_iso(start_dt), _iso(end_dt)) if is_configured() else []

    # if calendar isn't available, generate naive slots
    if not is_configured() or (free_blocks and isinstance(free_blocks[0], dict) and free_blocks[0].get("detail")):
        out: list[str] = []
        cursor = start_dt.replace(minute=0, second=0, microsecond=0)
        while cursor < end_dt and len(out) < 20:
            if _in_window(cursor):
                out.append(_iso(cursor))
            cursor += timedelta(minutes=int(slot_minutes))
        return out

    slots: list[str] = []
    for blk in free_blocks:
        bs = _parse(blk["start"])
        be = _parse(blk["end"])
        cursor = bs.replace(minute=0, second=0, microsecond=0)
        if cursor < bs:
            cursor += timedelta(hours=1)
        while cursor + timedelta(minutes=int(slot_minutes)) <= be:
            if _in_window(cursor):
                slots.append(_iso(cursor))
                if len(slots) >= 20:
                    return slots
            cursor += timedelta(minutes=int(slot_minutes))
    return slots


# -------- event creation --------

def create_event(
    title: str,
    start_iso: str,
    end_iso: str,
    attendees: list[str] | None = None,
    description: str = "",
    application_id: int | None = None,
    confirmed: bool = False,
) -> dict:
    if not confirmed:
        return {"ok": False, "detail": "calendar event requires explicit confirmation (confirmed=True)"}
    if not is_configured():
        return {"ok": False, "detail": "(calendar not configured)"}
    body = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
        "attendees": [{"email": a} for a in (attendees or [])],
    }
    try:
        res = _post("/calendars/primary/events", body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    # persist
    try:
        with tx() as conn:
            conn.execute(
                "INSERT INTO calendar_event (application_id, title, start_time, end_time, "
                "location, meeting_link, status, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    application_id,
                    title,
                    _parse(start_iso).timestamp(),
                    _parse(end_iso).timestamp(),
                    res.get("location"),
                    (res.get("hangoutLink") or res.get("htmlLink")),
                    res.get("status") or "confirmed",
                    __import__("json").dumps(res, default=str),
                ),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("persist calendar_event failed: %s", exc)
    try:
        audit("calendar_event_created", "calendar_event", None,
              title=title, attendees=attendees, application_id=application_id)
    except Exception:
        pass
    return {"ok": True, "event": res}


def propose_interview_reply(message_id: str, slots: list[str]) -> str:
    """Render a polite reply body suggesting times. Drafting only."""
    if not slots:
        return "Hi,\n\nThanks for reaching out. Could you share a couple of times that work on your end?\n\nThanks,\n(your name)\n"
    bullets = "\n".join(f"- {s}" for s in slots[:5])
    return (
        "Hi,\n\nThanks so much — I'd be glad to chat. Any of these times work for me:\n\n"
        f"{bullets}\n\nIf none of those fit, happy to suggest more.\n\nThanks,\n(your name)\n"
    )
