"""Gmail integration. OAuth + lightweight classifier.

Without GOOGLE_CLIENT_ID / SECRET configured, all public functions return
"(gmail not configured)" responses but never raise on import.

Drafts replies; NEVER auto-sends.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from ..config import settings
from ..db import audit, get_conn, row_to_dict, tx
from ..security import oauth_tokens

log = logging.getLogger("jhh.integrations.gmail")

# Scopes: readonly for inbox sweeps, send for drafts (NOT auto-send;
# the spec allows drafting reply emails, but user must review and send).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
]

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


CLASSIFY_PATTERNS = {
    "interview_request": [
        r"\binterview\b",
        r"schedule a call",
        r"available next week",
        r"book a time",
        r"set up a time",
    ],
    "rejection": [
        r"\bregret\b",
        r"moved forward with other",
        r"not move forward",
        r"unfortunately",
        r"decided not to",
        r"will not be moving forward",
    ],
    "assessment": [
        r"coding challenge",
        r"take[- ]home",
        r"hackerrank",
        r"codility",
        r"technical assessment",
        r"coderpad",
    ],
    "recruiter_screen": [
        r"initial screen",
        r"30[- ]minute",
        r"intro call",
        r"phone screen",
        r"recruiter call",
    ],
    "offer": [
        r"offer letter",
        r"compensation package",
        r"we'd like to extend",
        r"offer of employment",
    ],
}


def is_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


# --------------- OAuth ---------------

# Never let token material reach the logs: redact Bearer headers and
# access_token / refresh_token / id_token values (both literal values we
# hold and key=value / "key": "value" shapes inside exception text).
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]+")
_TOKEN_FIELD_RE = re.compile(
    r"((?:access|refresh|id)_token[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9\-._~+/=]+"
)


def _sanitize_token_text(text: str, tokens: dict | None = None) -> str:
    out = text or ""
    for key in ("access_token", "refresh_token", "id_token"):
        val = (tokens or {}).get(key)
        if isinstance(val, str) and val:
            out = out.replace(val, "[REDACTED]")
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    out = _TOKEN_FIELD_RE.sub(r"\1[REDACTED]", out)
    return out


def oauth_url(state: str = "") -> str:
    if not is_configured():
        return "(gmail not configured)"
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state or "jhh",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    if not is_configured():
        return {"ok": False, "detail": "(gmail not configured)"}
    tokens: dict = {}
    try:
        r = httpx.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=30,
        )
        r.raise_for_status()
        tokens = r.json()
        if "expires_in" in tokens:
            tokens["expires_at"] = time.time() + int(tokens["expires_in"])
        oauth_tokens.save_tokens(tokens)
        try:
            audit("google_oauth_exchange", "system", scope=tokens.get("scope"))
        except Exception:
            pass
        return {"ok": True, "scope": tokens.get("scope")}
    except Exception as exc:  # noqa: BLE001
        detail = _sanitize_token_text(f"{type(exc).__name__}: {exc}", tokens)
        log.warning("oauth exchange failed: %s", detail)
        return {"ok": False, "detail": detail}


def _refresh_if_needed(tokens: dict) -> dict:
    if not tokens:
        return tokens
    exp = tokens.get("expires_at") or 0
    if exp and exp - time.time() > 60:
        return tokens
    rt = tokens.get("refresh_token")
    if not rt or not is_configured():
        return tokens
    try:
        r = httpx.post(
            TOKEN_URL,
            data={
                "refresh_token": rt,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        r.raise_for_status()
        new = r.json()
        tokens.update({k: v for k, v in new.items() if v is not None})
        if "expires_in" in new:
            tokens["expires_at"] = time.time() + int(new["expires_in"])
        oauth_tokens.save_tokens(tokens)
        return tokens
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "token refresh failed: %s",
            _sanitize_token_text(f"{type(exc).__name__}: {exc}", tokens),
        )
        return tokens


def _access_token() -> Optional[str]:
    tokens = oauth_tokens.load_tokens()
    if not tokens:
        return None
    tokens = _refresh_if_needed(tokens)
    return tokens.get("access_token")


# --------------- Gmail API ---------------

def _api_get(path: str, params: dict | None = None) -> dict:
    tok = _access_token()
    if not tok:
        raise RuntimeError("no Google OAuth token; authorize first")
    r = httpx.get(
        f"{GMAIL_API}{path}",
        params=params or {},
        headers={"Authorization": f"Bearer {tok}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _api_post(path: str, body: dict) -> dict:
    tok = _access_token()
    if not tok:
        raise RuntimeError("no Google OAuth token; authorize first")
    r = httpx.post(
        f"{GMAIL_API}{path}",
        json=body,
        headers={"Authorization": f"Bearer {tok}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _decode_b64url(s: str) -> bytes:
    s = s or ""
    pad = 4 - (len(s) % 4)
    if pad and pad < 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _extract_body_text(payload: dict) -> str:
    """Walk MIME parts pulling text/plain (preferred) or text/html (stripped)."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    data = body.get("data")
    if mime == "text/plain" and data:
        try:
            return _decode_b64url(data).decode("utf-8", errors="ignore")
        except Exception:
            return ""
    if mime == "text/html" and data:
        try:
            html = _decode_b64url(data).decode("utf-8", errors="ignore")
            return re.sub(r"<[^>]+>", " ", html)
        except Exception:
            return ""
    parts = payload.get("parts") or []
    # prefer text/plain over text/html
    for p in parts:
        if p.get("mimeType") == "text/plain":
            t = _extract_body_text(p)
            if t:
                return t
    for p in parts:
        t = _extract_body_text(p)
        if t:
            return t
    return ""


def _headers_dict(message: dict) -> dict:
    out: dict = {}
    for h in (message.get("payload") or {}).get("headers", []) or []:
        out[h.get("name", "").lower()] = h.get("value", "")
    return out


def _parse_message(msg: dict) -> dict:
    headers = _headers_dict(msg)
    body = _extract_body_text(msg.get("payload") or {})
    received_ms = int(msg.get("internalDate") or 0)
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "to": headers.get("to", ""),
        "body_text": body,
        "received_at": received_ms / 1000.0 if received_ms else time.time(),
        "raw": msg,
    }


def search_recent(query: str = "newer_than:30d", max: int = 50) -> list[dict]:
    if not is_configured():
        return []
    try:
        listing = _api_get("/users/me/messages", {"q": query, "maxResults": int(max)})
    except Exception as exc:  # noqa: BLE001
        log.warning("gmail list failed: %s", exc)
        return []
    msgs = listing.get("messages") or []
    out: list[dict] = []
    for m in msgs:
        mid = m.get("id")
        if not mid:
            continue
        try:
            full = _api_get(f"/users/me/messages/{mid}", {"format": "full"})
            out.append(_parse_message(full))
        except Exception as exc:  # noqa: BLE001
            log.debug("gmail get %s failed: %s", mid, exc)
            continue
    return out


# --------------- Classifier ---------------

def classify(message: dict) -> str:
    text = " ".join([
        (message.get("subject") or ""),
        (message.get("body_text") or "")[:4000],
    ]).lower()
    if not text.strip():
        return "generic_update"
    for label, patterns in CLASSIFY_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return label
    return "generic_update"


# --------------- Linker ---------------

_DOMAIN_RE = re.compile(r"@([a-z0-9.\-]+)", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _tokens(s: str) -> set[str]:
    return set(w.lower() for w in _WORD_RE.findall(s or "") if len(w) >= 3)


def link_to_application(message: dict, applications: list[dict]) -> Optional[int]:
    if not message or not applications:
        return None
    sender = (message.get("sender") or "").lower()
    subject = (message.get("subject") or "").lower()
    body = (message.get("body_text") or "").lower()[:5000]
    sender_domain = ""
    m = _DOMAIN_RE.search(sender)
    if m:
        sender_domain = m.group(1).lower()

    best_id: Optional[int] = None
    best_score = 0
    for app in applications:
        company = (app.get("job_company") or "").lower()
        title = (app.get("job_title") or "").lower()
        score = 0
        if company:
            ctoks = _tokens(company)
            if not ctoks:
                continue
            # company in sender domain
            for tok in ctoks:
                if tok in sender_domain:
                    score += 5
                if tok in subject:
                    score += 3
                if tok in body:
                    score += 1
        if title:
            ttoks = _tokens(title)
            common = ttoks & _tokens(subject + " " + body)
            score += min(3, len(common))
        if score > best_score:
            best_score = score
            best_id = int(app["id"])
    # require at least 3 to call it a match
    return best_id if best_score >= 3 else None


# --------------- Ingest + persist ---------------

def _already_ingested(message_id: str) -> bool:
    if not message_id:
        return False
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM email_event WHERE raw_json LIKE ? LIMIT 1",
        (f'%"id": "{message_id}"%',),
    ).fetchone()
    return row is not None


def ingest_all(query: str = "newer_than:30d", max: int = 50) -> dict:
    if not is_configured():
        return {"ok": False, "detail": "(gmail not configured)", "processed": 0, "linked": 0, "events_added": 0}
    msgs = search_recent(query=query, max=max)
    # Load all active applications once for matching
    from ..applications.pipeline import list_applications

    apps = list_applications(limit=1000)
    processed = 0
    linked = 0
    added = 0
    for m in msgs:
        processed += 1
        if _already_ingested(m.get("id") or ""):
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
                    json.dumps({"id": m.get("id"), "thread_id": m.get("thread_id")}),
                ),
            )
        added += 1
        # auto-update application status on strong signals
        if app_id and label in ("interview_request", "offer", "rejection", "recruiter_screen", "assessment", "replied"):
            try:
                from ..applications import pipeline as appp
                status_map = {
                    "interview_request": "interview",
                    "offer": "offer",
                    "rejection": "rejected",
                    "recruiter_screen": "screened",
                    "assessment": "screened",
                }
                new_status = status_map.get(label) or "replied"
                appp.update_application(int(app_id), {"status": new_status, "last_contact_at": m.get("received_at")})
            except Exception as exc:  # noqa: BLE001
                log.debug("auto-status update failed: %s", exc)
    try:
        audit("gmail_ingest", "email_event", None, processed=processed, linked=linked, added=added)
    except Exception:
        pass
    return {"ok": True, "processed": processed, "linked": linked, "events_added": added}


# --------------- Drafts ---------------

def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _build_mime_text(to: str, subject: str, body: str, in_reply_to: str | None = None) -> str:
    headers = [
        f"To: {to}",
        f"Subject: {subject}",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
        headers.append(f"References: {in_reply_to}")
    return "\r\n".join(headers) + "\r\n\r\n" + (body or "")


REPLY_TEMPLATES = {
    "interview_request": (
        "Hi,\n\nThanks so much for reaching out — I'd be glad to schedule a time to talk.\n\n"
        "I'm generally available {AVAILABILITY}. If any of those work, let me know and I'll send a calendar invite. "
        "Otherwise, happy to fit your schedule.\n\nThanks,\n{NAME}\n"
    ),
    "rejection": (
        "Hi,\n\nThanks very much for considering me and for letting me know. I appreciate the time the team took. "
        "If a future role opens that's a closer fit, I'd welcome the chance to reconnect.\n\nBest,\n{NAME}\n"
    ),
    "assessment": (
        "Hi,\n\nThanks for sharing the assessment. I'll plan to complete it by {DEADLINE} and will reach out if I have any clarifying questions.\n\nThanks,\n{NAME}\n"
    ),
    "recruiter_screen": (
        "Hi,\n\nThanks for reaching out. I'd be glad to chat. I'm available {AVAILABILITY} — let me know what works on your end.\n\nThanks,\n{NAME}\n"
    ),
    "offer": (
        "Hi,\n\nThanks so much for the offer — I'm very excited. I'd like to review the details and follow up with any questions in the next day or two. "
        "Could you confirm the timeline for a decision?\n\nThanks,\n{NAME}\n"
    ),
    "generic_update": (
        "Hi,\n\nThanks for the note. Following up to confirm receipt; happy to discuss next steps.\n\nBest,\n{NAME}\n"
    ),
}


def draft_reply(event_id: int, type: str | None = None) -> dict:
    """Build a draft reply text for a previously-ingested email event. Never sends."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM email_event WHERE id = ?", (int(event_id),)).fetchone()
    if not row:
        return {"ok": False, "detail": f"event {event_id} not found"}
    ev = row_to_dict(row)
    label = type or ev.get("detected_type") or "generic_update"
    template = REPLY_TEMPLATES.get(label, REPLY_TEMPLATES["generic_update"])

    # pull user profile for name
    name = "(your name)"
    avail = "weekday afternoons (US Eastern)"
    try:
        prow = conn.execute("SELECT name, interview_availability_json FROM user_profile WHERE id = 1").fetchone()
        if prow:
            name = (prow["name"] or name) if prow["name"] else name
            ia = prow["interview_availability_json"]
            if ia:
                try:
                    parsed = json.loads(ia) if isinstance(ia, str) else ia
                    if isinstance(parsed, dict):
                        avail = parsed.get("free_text") or avail
                except Exception:
                    pass
    except Exception:
        pass

    body = template.format(NAME=name, AVAILABILITY=avail, DEADLINE="end of the week")
    return {
        "ok": True,
        "event_id": int(event_id),
        "type": label,
        "to": ev.get("sender"),
        "subject": f"Re: {ev.get('subject') or ''}",
        "draft": body,
        "note": "Draft only — review before sending. To send via Gmail use the Gmail UI or POST /api/email/send (not implemented; never auto-send).",
    }


def status() -> dict:
    configured = is_configured()
    tokens = oauth_tokens.load_tokens() if configured else {}
    return {
        "configured": configured,
        "authorized": bool(tokens),
        "expires_at": tokens.get("expires_at") if tokens else None,
        "scopes": tokens.get("scope") if tokens else None,
    }
