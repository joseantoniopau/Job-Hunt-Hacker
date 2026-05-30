"""Email integration router. Gmail + IMAP."""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..db import audit, get_conn, row_to_dict, tx
from ..integrations import gmail, imap

log = logging.getLogger("jhh.routers.email")

router = APIRouter(prefix="", tags=["email"])


class DraftReplyBody(BaseModel):
    event_id: int
    type: Optional[str] = None


class EmailEventPatch(BaseModel):
    status: str


_ALLOWED_EVENT_STATUSES = {"replied", "ignored", "actioned", "unread", "read"}


# ---- status / sweep ----

@router.get("/api/email/status")
def status() -> dict:
    return {
        "ok": True,
        "data": {
            "gmail": gmail.status(),
            "imap": imap.status(),
        },
    }


@router.post("/api/email/sweep")
def sweep() -> dict:
    g = gmail.ingest_all() if gmail.is_configured() else {"ok": False, "detail": "(gmail not configured)"}
    i = imap.ingest_all() if imap.is_configured() else {"ok": False, "detail": "(imap not configured)"}
    return {"ok": True, "data": {"gmail": g, "imap": i}}


@router.get("/api/email/events")
def events(
    type: Optional[str] = Query(None),
    application_id: Optional[int] = Query(None),
    limit: int = 100,
    offset: int = 0,
) -> dict:
    conn = get_conn()
    sql = "SELECT * FROM email_event"
    where: list[str] = []
    params: list = []
    if type:
        where.append("detected_type = ?")
        params.append(type)
    if application_id is not None:
        where.append("application_id = ?")
        params.append(int(application_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY received_at DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r) or {}
        # UI-friendly aliases (frontend reads these names)
        if "sender" in d:
            d["from_address"] = d["sender"]
        if "detected_type" in d:
            d["classification"] = d["detected_type"]
        if "application_id" in d and "job_id" not in d:
            d["job_id"] = d.get("application_id")
        out.append(d)
    return {"ok": True, "data": out}


@router.patch("/api/email/events/{event_id}")
def patch_email_event(event_id: int, body: EmailEventPatch) -> dict:
    """Update an email event's local status flag.

    Schema migration is handled in db._init_schema, but if we're talking
    to an older DB that somehow missed it, ALTER the table on first call.
    """
    status = (body.status or "").strip().lower()
    if status not in _ALLOWED_EVENT_STATUSES:
        raise HTTPException(
            400,
            f"invalid status: {body.status!r}; allowed={sorted(_ALLOWED_EVENT_STATUSES)}",
        )
    conn = get_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(email_event)").fetchall()}
    if "status" not in cols:
        try:
            with tx() as c2:
                c2.execute("ALTER TABLE email_event ADD COLUMN status TEXT")
                c2.execute("ALTER TABLE email_event ADD COLUMN status_updated_at REAL")
        except Exception as exc:  # noqa: BLE001
            log.warning("email_event ALTER failed (might be OK): %s", exc)
    row = conn.execute("SELECT id FROM email_event WHERE id = ?", (int(event_id),)).fetchone()
    if row is None:
        raise HTTPException(404, f"email event {event_id} not found")
    with tx() as c3:
        c3.execute(
            "UPDATE email_event SET status = ?, status_updated_at = ? WHERE id = ?",
            (status, time.time(), int(event_id)),
        )
    audit("email_event_status", "email_event", int(event_id), status=status)
    return {"ok": True, "data": {"id": int(event_id), "status": status}}


@router.post("/api/email/draft-reply")
def draft_reply(body: DraftReplyBody) -> dict:
    res = gmail.draft_reply(int(body.event_id), body.type)
    if not res.get("ok"):
        raise HTTPException(404, res.get("detail") or "draft failed")
    return {"ok": True, "data": res}


# ---- OAuth ----

@router.get("/oauth/google/start")
def oauth_start():
    url = gmail.oauth_url()
    if url == "(gmail not configured)":
        raise HTTPException(503, "Google OAuth not configured (set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env)")
    return RedirectResponse(url)


@router.get("/oauth/google/callback")
def oauth_callback(code: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(f"/?oauth=error&detail={error}")
    if not code:
        raise HTTPException(400, "missing code")
    res = gmail.exchange_code(code)
    if not res.get("ok"):
        return RedirectResponse(f"/?oauth=error&detail={res.get('detail', 'unknown')}")
    return RedirectResponse("/?oauth=success")
