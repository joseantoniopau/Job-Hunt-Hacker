"""Email integration router. Gmail + IMAP."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..db import get_conn, row_to_dict
from ..integrations import gmail, imap

log = logging.getLogger("jhh.routers.email")

router = APIRouter(prefix="", tags=["email"])


class DraftReplyBody(BaseModel):
    event_id: int
    type: Optional[str] = None


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
    return {"ok": True, "data": [row_to_dict(r) for r in rows]}


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
