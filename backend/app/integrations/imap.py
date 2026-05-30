"""IMAP fallback for non-Gmail users.

Uses imaplib (stdlib). Shape parity with gmail.ingest_all().
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import json
import logging
import time
from email.header import decode_header
from typing import Optional

from ..config import settings
from ..db import audit, get_conn, tx
from .gmail import CLASSIFY_PATTERNS, classify, link_to_application

log = logging.getLogger("jhh.integrations.imap")


def is_configured() -> bool:
    return bool(settings.imap_host and settings.imap_user and settings.imap_pass)


def _decode_header(val: str) -> str:
    if not val:
        return ""
    try:
        parts = decode_header(val)
        out = []
        for txt, enc in parts:
            if isinstance(txt, bytes):
                out.append(txt.decode(enc or "utf-8", errors="ignore"))
            else:
                out.append(txt)
        return "".join(out)
    except Exception:
        return val


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    payload = part.get_payload(decode=True) or b""
                    return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                except Exception:
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True) or b""
        return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
    except Exception:
        return ""


def search_recent(days: int = 30, mailbox: str = "INBOX", limit: int = 200) -> list[dict]:
    if not is_configured():
        return []
    out: list[dict] = []
    try:
        port = 993
        if ":" in settings.imap_host:
            host, p = settings.imap_host.rsplit(":", 1)
            port = int(p)
        else:
            host = settings.imap_host
        with imaplib.IMAP4_SSL(host, port) as cli:
            cli.login(settings.imap_user, settings.imap_pass)
            cli.select(mailbox)
            cutoff = (time.time() - days * 86400)
            since = time.strftime("%d-%b-%Y", time.gmtime(cutoff))
            typ, data = cli.search(None, f'(SINCE "{since}")')
            if typ != "OK":
                return []
            ids = (data[0] or b"").split()
            ids = ids[-int(limit):]
            for mid in ids:
                typ, mdata = cli.fetch(mid, "(RFC822)")
                if typ != "OK" or not mdata:
                    continue
                raw = mdata[0][1] if isinstance(mdata[0], tuple) else None
                if not raw:
                    continue
                m = email.message_from_bytes(raw)
                date_str = m.get("Date") or ""
                ts = time.time()
                try:
                    parsed = email.utils.parsedate_to_datetime(date_str)
                    if parsed:
                        ts = parsed.timestamp()
                except Exception:
                    pass
                msg_id = m.get("Message-ID") or f"imap-{mid.decode('ascii', errors='ignore')}"
                out.append({
                    "id": msg_id,
                    "sender": _decode_header(m.get("From") or ""),
                    "subject": _decode_header(m.get("Subject") or ""),
                    "body_text": _body_text(m),
                    "received_at": ts,
                    "raw": {"id": msg_id, "imap_uid": mid.decode("ascii", errors="ignore")},
                })
    except Exception as exc:  # noqa: BLE001
        log.warning("imap fetch failed: %s", exc)
    return out


def _already_ingested(message_id: str) -> bool:
    if not message_id:
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM email_event WHERE raw_json LIKE ? LIMIT 1",
        (f'%"id": "{message_id}"%',),
    ).fetchone()
    return row is not None


def ingest_all(days: int = 30, max: int = 200) -> dict:
    if not is_configured():
        return {"ok": False, "detail": "(imap not configured)", "processed": 0, "linked": 0, "events_added": 0}
    msgs = search_recent(days=days, limit=max)
    from ..applications.pipeline import list_applications

    apps = list_applications(limit=1000)
    processed = 0
    linked = 0
    added = 0
    for m in msgs:
        processed += 1
        mid = m.get("id") or ""
        if _already_ingested(mid):
            continue
        label = classify(m)
        app_id = link_to_application(m, apps)
        if app_id:
            linked += 1
        with tx() as conn:
            conn.execute(
                "INSERT INTO email_event (application_id, sender, subject, body_text, "
                "detected_type, received_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    app_id,
                    m.get("sender"),
                    m.get("subject"),
                    (m.get("body_text") or "")[:10000],
                    label,
                    m.get("received_at"),
                    json.dumps({"id": mid}),
                ),
            )
        added += 1
    try:
        audit("imap_ingest", "email_event", None, processed=processed, linked=linked, added=added)
    except Exception:
        pass
    return {"ok": True, "processed": processed, "linked": linked, "events_added": added}


def status() -> dict:
    return {
        "configured": is_configured(),
        "host": settings.imap_host or None,
        "user": settings.imap_user or None,
    }
