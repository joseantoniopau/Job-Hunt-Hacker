"""Data export / import / wipe.

Three operations under /api/data:

  * GET  /api/data/export
        Stream a single JSON dump containing every user-owned row from the
        SQLite vault (profile + evidence + jobs + resumes + applications +
        events + saved searches + recent audit log). The browser is asked
        to save it via Content-Disposition.

  * POST /api/data/import      (multipart: file=<jhh-export-*.json>)
        Transactionally restore an export bundle into the live database.
        Rows are upserted by primary key when possible so re-importing the
        same bundle on top of an existing database is safe. Refuses when
        the bundle version does not match the running app version.

  * DELETE /api/data?i_understand=ENABLE
        Wipe ALL user data. Profile is reset to defaults instead of
        deleted (the singleton row is required by the rest of the app).
        Requires the literal `?i_understand=ENABLE` query string — every
        other value returns HTTP 400 with no destructive side effect.

Every action is audited.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import Response

from ..config import settings
from ..db import audit, get_conn, row_to_dict, tx

log = logging.getLogger("jhh.data")

router = APIRouter(prefix="/api/data", tags=["data"])

EXPORT_VERSION = "1"

# Tables exported and importable. Order matters for import (parents first).
# Each entry is (table_name, primary_key_col).
TABLES: list[tuple[str, str]] = [
    ("user_profile", "id"),
    ("evidence_source", "id"),
    ("career_claim", "id"),
    ("career_fact", "id"),
    ("resume_document", "id"),
    ("job_posting", "id"),
    ("job_match", "id"),
    ("tailored_resume", "id"),
    ("cover_letter", "id"),
    ("application", "id"),
    ("email_event", "id"),
    ("calendar_event", "id"),
    ("saved_search", "id"),
    ("source_state", "source"),
]

# Tables wiped on DELETE (everything except user_profile, which is reset).
WIPE_TABLES: list[str] = [
    "email_event",
    "calendar_event",
    "application",
    "cover_letter",
    "tailored_resume",
    "job_match",
    "job_posting",
    "career_claim",
    "career_fact",
    "embedding",
    "evidence_source",
    "resume_document",
    "saved_search",
    "source_state",
    "audit_log",
]


# ---------- EXPORT ----------

@router.get("/export")
def export_all() -> Response:
    conn = get_conn()
    bundle: dict[str, Any] = {
        "version": EXPORT_VERSION,
        "app_version": "0.2.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tables": {},
    }
    counts: dict[str, int] = {}
    for table, _pk in TABLES:
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        except Exception as exc:  # noqa: BLE001
            log.warning("export: skip table %s (%s)", table, exc)
            bundle["tables"][table] = []
            counts[table] = 0
            continue
        dicts = [_row_for_export(r) for r in rows]
        bundle["tables"][table] = dicts
        counts[table] = len(dicts)
    # last 500 audit-log entries (full history can be huge; keep cap)
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 500"
        ).fetchall()
        bundle["audit_log_recent"] = [_row_for_export(r) for r in rows]
        counts["audit_log_recent"] = len(bundle["audit_log_recent"])
    except Exception:
        bundle["audit_log_recent"] = []
        counts["audit_log_recent"] = 0

    audit("data_export", "data", None, counts=counts)
    body = json.dumps(bundle, default=str, indent=2)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="jhh-export-{ts}.json"',
            "X-JHH-Export-Counts": json.dumps(counts),
        },
    )


def _row_for_export(row) -> dict:
    """Convert a sqlite Row to a plain dict WITHOUT auto-decoding *_json
    columns — we want the raw stored values so import can write them back
    byte-for-byte."""
    d = dict(row)
    return d


# ---------- IMPORT ----------

@router.post("/import")
async def import_all(file: UploadFile = File(...)) -> dict:
    try:
        raw = await file.read()
    except Exception as exc:
        raise HTTPException(400, f"could not read upload: {exc}")
    if not raw:
        raise HTTPException(400, "empty upload")
    try:
        bundle = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(400, f"invalid JSON: {exc}")

    if not isinstance(bundle, dict):
        raise HTTPException(400, "bundle must be a JSON object")
    version = str(bundle.get("version") or "")
    if version != EXPORT_VERSION:
        raise HTTPException(
            400,
            f"version mismatch: bundle={version!r} expected={EXPORT_VERSION!r}",
        )
    tables = bundle.get("tables") or {}
    if not isinstance(tables, dict):
        raise HTTPException(400, "bundle.tables must be an object")

    imported_counts: dict[str, int] = {}
    skipped_counts: dict[str, int] = {}
    errors: list[str] = []

    with tx() as conn:
        for table, pk in TABLES:
            rows = tables.get(table) or []
            if not isinstance(rows, list):
                errors.append(f"{table}: not a list")
                continue
            # discover real columns to filter unknown keys (schema drift safe)
            try:
                cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            except Exception as exc:
                errors.append(f"{table}: pragma failed {exc}")
                continue
            valid_cols = {c[1] for c in cols_info}
            inserted = 0
            skipped = 0
            for rec in rows:
                if not isinstance(rec, dict):
                    skipped += 1
                    continue
                clean = {k: v for k, v in rec.items() if k in valid_cols}
                if not clean:
                    skipped += 1
                    continue
                # Special-case user_profile: always upsert into the singleton row.
                if table == "user_profile":
                    clean["id"] = 1
                col_list = ", ".join(clean.keys())
                placeholders = ", ".join("?" for _ in clean)
                update_list = ", ".join(
                    f"{c}=excluded.{c}" for c in clean.keys() if c != pk
                )
                sql = (
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT({pk}) DO UPDATE SET {update_list}"
                    if update_list
                    else f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
                )
                try:
                    conn.execute(sql, list(clean.values()))
                    inserted += 1
                except Exception as exc:  # noqa: BLE001
                    skipped += 1
                    errors.append(f"{table}#{rec.get(pk)}: {exc}")
            imported_counts[table] = inserted
            skipped_counts[table] = skipped

    audit("data_import", "data", None,
          imported=imported_counts, skipped=skipped_counts,
          error_count=len(errors))
    return {
        "ok": True,
        "data": {
            "imported_counts": imported_counts,
            "skipped_counts": skipped_counts,
            "errors": errors[:50],
            "error_count": len(errors),
            "version": version,
        },
    }


# ---------- WIPE ----------

@router.delete("")
def delete_all(
    i_understand: str = Query(
        default="",
        description="Must be exactly the string ENABLE to confirm.",
    ),
) -> dict:
    if i_understand != "ENABLE":
        raise HTTPException(
            400,
            "destructive op requires ?i_understand=ENABLE (exact string)",
        )
    counts: dict[str, int] = {}
    now = time.time()
    with tx() as conn:
        for table in WIPE_TABLES:
            try:
                cur = conn.execute(f"DELETE FROM {table}")
                counts[table] = int(cur.rowcount or 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("wipe: failed to clear %s: %s", table, exc)
                counts[table] = -1
        # reset user_profile singleton to defaults
        try:
            conn.execute("DELETE FROM user_profile WHERE id = 1")
            conn.execute(
                "INSERT INTO user_profile (id, currency, mode, created_at, updated_at) "
                "VALUES (1, 'USD', 'assisted', ?, ?)",
                (now, now),
            )
            counts["user_profile"] = 1   # reset, not deleted
        except Exception as exc:  # noqa: BLE001
            log.warning("wipe: failed to reset user_profile: %s", exc)
            counts["user_profile"] = -1

    # All saved_search rows were just deleted, so unregister their APScheduler
    # jobs too — otherwise dangling cron jobs fire against missing DB rows.
    try:
        from ..integrations import scheduler as _sched
        if hasattr(_sched, "unregister_all_saved_search_jobs"):
            _sched.unregister_all_saved_search_jobs()
        elif hasattr(_sched, "register_saved_searches"):
            # Re-register (no rows → no jobs); APScheduler replace_existing
            # semantics handle the diff.
            _sched.register_saved_searches()
    except Exception as exc:  # noqa: BLE001
        log.warning("wipe: failed to clean scheduler jobs: %s", exc)

    # audit AFTER wipe so the record survives (audit_log was just cleared)
    audit("data_wipe", "data", None, counts=counts)
    return {"ok": True, "data": {"counts": counts}}
