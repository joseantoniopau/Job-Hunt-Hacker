"""Application CRUD + status flow. Joined views for the UI pipeline."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..db import audit, get_conn, row_to_dict, tx

log = logging.getLogger("jhh.applications.pipeline")

VALID_STATUSES = {
    "saved",
    "prepared",
    "applied",
    "replied",
    "screened",
    "interview",
    "offer",
    "rejected",
    "archived",
    "auto_packet_ready",
}


def _validate_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}; must be one of {sorted(VALID_STATUSES)}")
    return s


def create_application(
    job_id: int,
    status: str = "saved",
    notes: str = "",
    mode: str | None = None,
    resume_id: int | None = None,
    cover_letter_id: int | None = None,
    application_url: str | None = None,
) -> int:
    status = _validate_status(status)
    now = time.time()
    with tx() as conn:
        # Reuse existing application for the same job if already present? We
        # actually allow multiple applications per job (e.g. reapplied), but
        # for "saved"/"prepared" we'll dedupe by reusing the latest non-final.
        existing = conn.execute(
            "SELECT id, status FROM application WHERE job_id = ? "
            "AND status NOT IN ('archived','rejected') "
            "ORDER BY id DESC LIMIT 1",
            (int(job_id),),
        ).fetchone()
        if existing and status in ("saved", "prepared", "auto_packet_ready"):
            app_id = int(existing["id"])
            conn.execute(
                "UPDATE application SET status = ?, mode = COALESCE(?, mode), "
                "notes = COALESCE(NULLIF(?, ''), notes), "
                "resume_id = COALESCE(?, resume_id), "
                "cover_letter_id = COALESCE(?, cover_letter_id), "
                "application_url = COALESCE(?, application_url) "
                "WHERE id = ?",
                (status, mode, notes, resume_id, cover_letter_id, application_url, app_id),
            )
        else:
            cur = conn.execute(
                "INSERT INTO application (job_id, status, mode, notes, applied_at, "
                "application_url, resume_id, cover_letter_id, audit_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(job_id),
                    status,
                    mode,
                    notes or "",
                    now if status == "applied" else None,
                    application_url,
                    resume_id,
                    cover_letter_id,
                    json.dumps([{"ts": now, "action": "created", "status": status}]),
                ),
            )
            app_id = int(cur.lastrowid)
    try:
        audit("application_create", "application", app_id, job_id=int(job_id), status=status)
    except Exception:
        pass
    return app_id


def update_application(application_id: int, fields: dict) -> dict:
    if not fields:
        return get_application(application_id) or {}
    allowed = {"status", "mode", "notes", "next_followup_at", "last_contact_at",
               "application_url", "resume_id", "cover_letter_id", "applied_at"}
    sets: list[str] = []
    vals: list[Any] = []
    transition_to_applied = False
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "status":
            v = _validate_status(v)
            if v == "applied":
                transition_to_applied = True
        sets.append(f"{k} = ?")
        vals.append(v)
    # Auto-set applied_at when transitioning to status=applied, if not already set
    if transition_to_applied and "applied_at" not in fields:
        conn0 = get_conn()
        existing = conn0.execute(
            "SELECT applied_at FROM application WHERE id = ?",
            (int(application_id),),
        ).fetchone()
        if existing and existing["applied_at"] is None:
            sets.append("applied_at = ?")
            vals.append(time.time())
    if not sets:
        return get_application(application_id) or {}
    vals.append(int(application_id))
    with tx() as conn:
        cur = conn.execute(
            f"UPDATE application SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        if cur.rowcount == 0:
            raise LookupError(f"application {application_id} not found")
        # append audit history into audit_json
        row = conn.execute(
            "SELECT audit_json FROM application WHERE id = ?",
            (int(application_id),),
        ).fetchone()
        try:
            hist = json.loads(row["audit_json"]) if row and row["audit_json"] else []
        except Exception:
            hist = []
        hist.append({"ts": time.time(), "action": "update", "fields": fields})
        conn.execute(
            "UPDATE application SET audit_json = ? WHERE id = ?",
            (json.dumps(hist, default=str), int(application_id)),
        )
    try:
        audit("application_update", "application", int(application_id), fields=fields)
    except Exception:
        pass
    return get_application(application_id) or {}


def _alias_app(d: dict | None) -> dict | None:
    """Surface UI-friendly aliases (title/company/score/packet_path/url)
    on top of the joined columns so the frontend doesn't have to know about
    the underlying schema.
    """
    if not d:
        return d
    out = dict(d)
    if "job_title" in out:
        out["title"] = out["job_title"]
    if "job_company" in out:
        out["company"] = out["job_company"]
    if "job_location" in out and "location" not in out:
        out["location"] = out["job_location"]
    if "job_apply_url" in out:
        out["url"] = out["job_apply_url"]
    if "overall_score" in out:
        # Scorer stores 0-1; UI consumes 0-100 (match _alias_job in jobs router)
        v = out["overall_score"]
        out["score"] = int(round(float(v) * 100)) if v is not None else None
    # packet_path: read from audit_json history if recorded; else empty
    try:
        hist = out.get("audit_json")
        if isinstance(hist, list):
            for entry in reversed(hist):
                f = (entry or {}).get("fields") or {}
                if "packet_path" in f:
                    out["packet_path"] = f["packet_path"]
                    break
    except Exception:
        pass
    return out


def get_application(application_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT a.*, j.title AS job_title, j.company AS job_company, "
        "j.location AS job_location, j.apply_url AS job_apply_url, j.source AS job_source, "
        "m.overall_score "
        "FROM application a "
        "LEFT JOIN job_posting j ON j.id = a.job_id "
        "LEFT JOIN job_match m ON m.job_id = a.job_id "
        "WHERE a.id = ?",
        (int(application_id),),
    ).fetchone()
    return _alias_app(row_to_dict(row)) if row else None


def list_applications(status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    conn = get_conn()
    sql = (
        "SELECT a.*, j.title AS job_title, j.company AS job_company, "
        "j.location AS job_location, j.apply_url AS job_apply_url, j.source AS job_source, "
        "m.overall_score, "
        "tr.id AS tailored_resume_id, tr.markdown AS tailored_resume_markdown "
        "FROM application a "
        "LEFT JOIN job_posting j ON j.id = a.job_id "
        "LEFT JOIN job_match m ON m.job_id = a.job_id "
        "LEFT JOIN tailored_resume tr ON tr.id = a.resume_id "
    )
    params: list[Any] = []
    if status:
        sql += "WHERE a.status = ? "
        params.append(_validate_status(status))
    sql += "ORDER BY a.id DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    rows = conn.execute(sql, params).fetchall()
    return [_alias_app(row_to_dict(r)) for r in rows]


def pipeline_board() -> dict:
    """Return {status_name: [application_dicts]} for kanban UI.

    Includes every valid status as a key, even if empty.
    """
    rows = list_applications(limit=1000)
    board: dict[str, list[dict]] = {s: [] for s in VALID_STATUSES}
    for r in rows:
        s = r.get("status") or "saved"
        if s not in board:
            board[s] = []
        board[s].append(r)
    return board


def set_followup(application_id: int, days: int, status: str | None = None) -> dict:
    next_at = time.time() + int(days) * 86400
    fields: dict = {"next_followup_at": next_at}
    if status:
        fields["status"] = status
    out = update_application(application_id, fields)
    try:
        audit("application_followup_set", "application", int(application_id), days=days, status=status)
    except Exception:
        pass
    return out


def delete_application(application_id: int, hard: bool = False) -> bool:
    if hard:
        with tx() as conn:
            cur = conn.execute("DELETE FROM application WHERE id = ?", (int(application_id),))
            return cur.rowcount > 0
    # soft: status=archived
    try:
        update_application(application_id, {"status": "archived"})
        return True
    except LookupError:
        return False


def find_followups_due() -> list[dict]:
    conn = get_conn()
    now = time.time()
    rows = conn.execute(
        "SELECT a.*, j.title AS job_title, j.company AS job_company "
        "FROM application a "
        "LEFT JOIN job_posting j ON j.id = a.job_id "
        "WHERE a.next_followup_at IS NOT NULL AND a.next_followup_at < ? "
        "AND a.status NOT IN ('archived','rejected') "
        "ORDER BY a.next_followup_at ASC",
        (now,),
    ).fetchall()
    return [row_to_dict(r) for r in rows]
