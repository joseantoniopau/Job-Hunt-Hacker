"""Tests for PII-redacted export (`GET /api/data/export?redact_pii=true`).

Covers:
  * redact_pii=true  -> profile email/phone/name masked, email_event body
    replaced + sender masked (subject kept), calendar_event description
    redacted + attendee emails masked inside raw_json.
  * default (false)  -> bundle byte-identical behavior: PII untouched,
    `redacted` flag false. Snapshot creation reuses this default path.
  * a redacted bundle is still structurally valid: POST /api/data/import
    accepts it with zero errors.

conftest.py redirects the DB to a temp file so nothing here touches the
user's real vault.
"""
from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from backend.app.db import get_conn, init_db, tx
from backend.app.main import app
from backend.app.routers.data import _build_export_bundle

client = TestClient(app)

PROFILE_NAME = "Jane Marie Doe"
PROFILE_EMAIL = "jane.doe@example.com"
PROFILE_PHONE = "+1 (415) 555-1234"
EMAIL_SENDER = "Rex Recruiter <rex@corp.example>"
EMAIL_SUBJECT = "Interview availability"
EMAIL_BODY = "Hi Jane, can you meet Tuesday? My cell is 415-555-9999."
CAL_DESCRIPTION = "Panel interview prep notes: ask about comp band."
CAL_ATTENDEES = [
    {"email": "hr@corp.example", "displayName": "HR Team"},
    "candidate@example.com",
]


def _seed() -> tuple[int, int]:
    """Write known PII into the temp vault. Returns (email_id, cal_id)."""
    init_db()
    now = time.time()
    with tx() as conn:
        conn.execute(
            "UPDATE user_profile SET name = ?, email = ?, phone = ?, updated_at = ? "
            "WHERE id = 1",
            (PROFILE_NAME, PROFILE_EMAIL, PROFILE_PHONE, now),
        )
        cur = conn.execute(
            "INSERT INTO email_event (application_id, sender, subject, body_text, "
            "detected_type, received_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (None, EMAIL_SENDER, EMAIL_SUBJECT, EMAIL_BODY, "interview", now,
             json.dumps({"id": "msg-1", "thread_id": "thr-1"})),
        )
        email_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO calendar_event (application_id, title, start_time, "
            "end_time, status, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
            (None, "Panel interview", now, now + 3600, "confirmed",
             json.dumps({"description": CAL_DESCRIPTION,
                         "attendees": CAL_ATTENDEES,
                         "status": "confirmed"})),
        )
        cal_id = int(cur.lastrowid)
    return email_id, cal_id


def _export_bundle(redact: bool) -> dict:
    url = "/api/data/export"
    if redact:
        url += "?redact_pii=true"
    r = client.get(url)
    assert r.status_code == 200, r.text
    assert "attachment" in (r.headers.get("Content-Disposition") or "")
    return json.loads(r.content)


def _row_by_id(bundle: dict, table: str, row_id: int) -> dict:
    rows = bundle["tables"][table]
    matches = [r for r in rows if r.get("id") == row_id]
    assert matches, f"{table} id={row_id} missing from bundle"
    return matches[0]


# ----------------------- redact_pii=true -----------------------

def test_redacted_export_masks_profile_email_and_calendar() -> None:
    email_id, cal_id = _seed()
    bundle = _export_bundle(redact=True)

    assert bundle["redacted"] is True

    # user_profile: email first-char+***+@domain, phone last 2 digits,
    # name first name + initial.
    profile = _row_by_id(bundle, "user_profile", 1)
    assert profile["email"] == "j***@example.com"
    assert profile["phone"] == "***34"
    assert profile["name"] == "Jane M."

    # email_event: body gone, subject kept, sender masked like an email.
    ev = _row_by_id(bundle, "email_event", email_id)
    assert ev["body_text"] == "[redacted]"
    assert ev["subject"] == EMAIL_SUBJECT
    assert ev["sender"] == "r***@corp.example"
    assert "Rex" not in ev["sender"]

    # calendar_event: description redacted, attendee emails masked
    # (raw_json holds both — there are no dedicated columns).
    cal = _row_by_id(bundle, "calendar_event", cal_id)
    raw = json.loads(cal["raw_json"])
    assert raw["description"] == "[redacted]"
    assert raw["attendees"][0]["email"] == "h***@corp.example"
    assert raw["attendees"][1] == "c***@example.com"
    # untouched sibling key inside raw_json survives
    assert raw["status"] == "confirmed"
    # untouched columns survive
    assert cal["title"] == "Panel interview"

    # no raw PII strings anywhere in the redacted blobs we own
    blob = json.dumps([profile, ev, cal])
    for leaked in (PROFILE_EMAIL, EMAIL_BODY, CAL_DESCRIPTION,
                   "hr@corp.example", "candidate@example.com"):
        assert leaked not in blob


# ----------------------- default (false) -----------------------

def test_default_export_is_unredacted() -> None:
    email_id, cal_id = _seed()
    bundle = _export_bundle(redact=False)

    assert bundle["redacted"] is False

    profile = _row_by_id(bundle, "user_profile", 1)
    assert profile["email"] == PROFILE_EMAIL
    assert profile["phone"] == PROFILE_PHONE
    assert profile["name"] == PROFILE_NAME

    ev = _row_by_id(bundle, "email_event", email_id)
    assert ev["body_text"] == EMAIL_BODY
    assert ev["sender"] == EMAIL_SENDER
    assert ev["subject"] == EMAIL_SUBJECT

    cal = _row_by_id(bundle, "calendar_event", cal_id)
    raw = json.loads(cal["raw_json"])
    assert raw["description"] == CAL_DESCRIPTION
    assert raw["attendees"] == CAL_ATTENDEES


def test_build_export_bundle_default_matches_snapshot_path() -> None:
    """Snapshot creation calls _build_export_bundle() with no args — it
    must stay unredacted so pre-wipe rollback restores the real data."""
    _seed()
    bundle, counts = _build_export_bundle()
    assert bundle["redacted"] is False
    profile = bundle["tables"]["user_profile"][0]
    assert profile["email"] == PROFILE_EMAIL
    assert profile["phone"] == PROFILE_PHONE
    assert counts["user_profile"] == 1


# ----------------------- import round-trip -----------------------

def test_redacted_bundle_imports_cleanly() -> None:
    email_id, cal_id = _seed()
    bundle = _export_bundle(redact=True)

    body = json.dumps(bundle, default=str).encode("utf-8")
    r = client.post(
        "/api/data/import",
        files={"file": ("jhh-export-redacted.json", body, "application/json")},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    data = payload["data"]
    assert data["error_count"] == 0, data["errors"]
    assert data["imported_counts"]["user_profile"] == 1
    assert data["imported_counts"]["email_event"] >= 1
    assert data["imported_counts"]["calendar_event"] >= 1

    # The masked values landed in the DB — structural validity proven.
    conn = get_conn()
    prof = conn.execute(
        "SELECT name, email, phone FROM user_profile WHERE id = 1"
    ).fetchone()
    assert prof["email"] == "j***@example.com"
    assert prof["phone"] == "***34"
    assert prof["name"] == "Jane M."
    ev = conn.execute(
        "SELECT sender, subject, body_text FROM email_event WHERE id = ?",
        (email_id,),
    ).fetchone()
    assert ev["body_text"] == "[redacted]"
    assert ev["subject"] == EMAIL_SUBJECT
    assert ev["sender"] == "r***@corp.example"
    cal = conn.execute(
        "SELECT raw_json FROM calendar_event WHERE id = ?", (cal_id,)
    ).fetchone()
    raw = json.loads(cal["raw_json"])
    assert raw["description"] == "[redacted]"
