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
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel

from ..config import APP_VERSION, settings
from ..db import audit, get_conn, row_to_dict, tx

log = logging.getLogger("jhh.data")

router = APIRouter(prefix="/api/data", tags=["data"])

EXPORT_VERSION = "1"

# Snapshots are pre-wipe insurance: we always take a fresh export right
# before destructive ops so the user can roll back. Keep the last 5.
_SNAPSHOT_KEEP = 5
_SNAPSHOT_PREFIX = "jhh-pre-wipe-"
_SNAPSHOT_SUFFIX = ".json"
# Loose ISO8601-ish: digits / T / Z / dashes / colons / dots. No path
# separators, no parent refs, no spaces -- defense-in-depth on filename
# acceptance for the restore/delete endpoints.
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\-:T]+$")


def _snapshots_dir() -> Path:
    p = settings.data_dir / "snapshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _validate_filename(filename: str) -> str:
    """Reject anything that could escape the snapshots dir. Returns the
    cleaned filename or raises HTTPException(400)."""
    if not filename or not isinstance(filename, str):
        raise HTTPException(400, "filename is required")
    name = filename.strip()
    # Reject path separators / traversal explicitly even though the regex
    # would catch them -- this makes the error message specific.
    if "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise HTTPException(400, "invalid filename (path traversal)")
    if not _SAFE_FILENAME_RE.match(name):
        raise HTTPException(400, "invalid filename (disallowed characters)")
    if os.path.basename(name) != name:
        raise HTTPException(400, "invalid filename")
    return name

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
    # `embedding` MUST be exported — otherwise vector retrieval is lost on
    # round-trip (the WIPE step nukes it, but if it's not in the bundle
    # we have nothing to restore).
    ("embedding", "id"),
    ("llm_run", "id"),
    ("profile_proposal", "id"),
    ("career_snapshot", "id"),
    ("connection", "id"),
    ("connection_company", "id"),
    ("gap_event", "id"),
    ("effectiveness_event", "id"),
    ("llm_job_score", "job_id"),
    ("interview_prep_packet", "id"),
    ("interview_practice_session", "id"),
    ("interview_practice_turn", "id"),
    ("offer_analysis", "id"),
]

# Tables wiped on DELETE (everything except user_profile, which is reset).
# Children before parents so explicit deletes never trip FK constraints.
WIPE_TABLES: list[str] = [
    "interview_practice_turn",
    "interview_practice_session",
    "interview_prep_packet",
    "offer_analysis",
    "gap_event",
    "effectiveness_event",
    "llm_job_score",
    "connection_company",
    "connection",
    "profile_proposal",
    "career_snapshot",
    "llm_run",
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

def _build_export_bundle() -> tuple[dict[str, Any], dict[str, int]]:
    """Materialize the full export bundle + per-table row counts. Pulled
    out of `export_all` so snapshot creation can reuse it."""
    conn = get_conn()
    bundle: dict[str, Any] = {
        "version": EXPORT_VERSION,
        "app_version": APP_VERSION,
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
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 500"
        ).fetchall()
        bundle["audit_log_recent"] = [_row_for_export(r) for r in rows]
        counts["audit_log_recent"] = len(bundle["audit_log_recent"])
    except Exception:
        bundle["audit_log_recent"] = []
        counts["audit_log_recent"] = 0
    return bundle, counts


@router.get("/export")
def export_all() -> Response:
    bundle, counts = _build_export_bundle()
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


# ---------- CSV EXPORT ----------

# Friendly aliases the UI uses → real SQL table names.
_CSV_TABLE_ALIASES: dict[str, str] = {
    "applications": "application",
    "application": "application",
    "jobs": "job_posting",
    "job_posting": "job_posting",
    "claims": "career_claim",
    "career_claim": "career_claim",
    "tailored_resumes": "tailored_resume",
    "tailored_resume": "tailored_resume",
    "cover_letters": "cover_letter",
    "cover_letter": "cover_letter",
}

# Which tables are eligible for the whole-export ZIP bundle.
_CSV_BUNDLE_TABLES: list[str] = [
    "application",
    "job_posting",
    "career_claim",
    "tailored_resume",
    "cover_letter",
]


def _csv_rows_for_table(table: str) -> tuple[list[str], list[dict]]:
    """Return (headers, rows) for the requested table. Headers are the
    table's column names in PRAGMA order. Rows are plain dicts with raw
    JSON columns stringified (so the CSV is import-symmetric)."""
    conn = get_conn()
    cols_info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    headers = [c[1] for c in cols_info]
    raw_rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    out: list[dict] = []
    for r in raw_rows:
        d = dict(r)
        # Keep JSON columns as their original stored string — CSV must be
        # a single flat string per cell. If the column is None, leave it as "".
        for k, v in list(d.items()):
            if v is None:
                d[k] = ""
            elif isinstance(v, (dict, list)):
                d[k] = json.dumps(v, default=str)
        out.append(d)
    return headers, out


def _csv_text(headers: list[str], rows: list[dict]) -> str:
    import csv
    import io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


@router.get("/export.csv")
def export_csv(table: str | None = Query(default=None)) -> Response:
    """Single-table CSV when `?table=...` is given; whole-export ZIP otherwise."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    if table:
        sql_table = _CSV_TABLE_ALIASES.get(table.strip().lower())
        if not sql_table:
            raise HTTPException(
                400,
                f"unknown table: {table!r}; valid: {sorted(set(_CSV_TABLE_ALIASES.keys()))}",
            )
        try:
            headers, rows = _csv_rows_for_table(sql_table)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"csv export failed for {sql_table}: {exc}")
        body = _csv_text(headers, rows)
        audit("data_export_csv", "data", None, table=sql_table, rows=len(rows))
        filename = f"jhh-{table}-{ts}.csv"
        return Response(
            content=body,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-JHH-Export-Rows": str(len(rows)),
            },
        )

    # No table param → ZIP of every bundle table's CSV.
    import io
    import zipfile
    buf = io.BytesIO()
    counts: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sql_table in _CSV_BUNDLE_TABLES:
            try:
                headers, rows = _csv_rows_for_table(sql_table)
            except Exception as exc:  # noqa: BLE001
                log.warning("csv-zip: skip %s (%s)", sql_table, exc)
                continue
            counts[sql_table] = len(rows)
            zf.writestr(f"{sql_table}.csv", _csv_text(headers, rows))
    audit("data_export_csv_zip", "data", None, counts=counts)
    filename = f"jhh-export-{ts}.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-JHH-Export-Counts": json.dumps(counts),
        },
    )


# ---------- IMPORT ----------

def _import_bundle(bundle: Any) -> dict[str, Any]:
    """Apply a parsed export bundle. Raises HTTPException on validation
    failures so handlers can let it bubble. Returns the same payload that
    the legacy /import endpoint emits."""
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

        # Restore audit_log_recent rows from the bundle so the user keeps
        # their history across export/wipe/import cycles.
        audit_rows = bundle.get("audit_log_recent") or []
        if isinstance(audit_rows, list) and audit_rows:
            try:
                cols_info = conn.execute("PRAGMA table_info(audit_log)").fetchall()
                valid_cols = {c[1] for c in cols_info}
                inserted_audit = 0
                for rec in audit_rows:
                    if not isinstance(rec, dict):
                        continue
                    clean = {k: v for k, v in rec.items() if k in valid_cols}
                    if not clean:
                        continue
                    col_list = ", ".join(clean.keys())
                    placeholders = ", ".join("?" for _ in clean)
                    try:
                        conn.execute(
                            f"INSERT OR REPLACE INTO audit_log ({col_list}) "
                            f"VALUES ({placeholders})",
                            list(clean.values()),
                        )
                        inserted_audit += 1
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"audit_log#{rec.get('id')}: {exc}")
                imported_counts["audit_log"] = inserted_audit
            except Exception as exc:  # noqa: BLE001
                errors.append(f"audit_log restore: {exc}")

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
    return _import_bundle(bundle)


# ---------- SNAPSHOTS ----------

def _write_pre_wipe_snapshot() -> dict[str, Any]:
    """Capture a full export into data/snapshots/ and rotate older ones.
    Returns metadata about the snapshot just written. Best-effort: a
    failure here MUST NOT block the wipe (it just means no rollback)."""
    try:
        bundle, counts = _build_export_bundle()
        # ISO timestamp with `:` and `.` for human readability. Pre-wipe
        # snapshots are read by humans more than by code.
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        filename = f"{_SNAPSHOT_PREFIX}{ts}{_SNAPSHOT_SUFFIX}"
        path = _snapshots_dir() / filename
        body = json.dumps(bundle, default=str, indent=2)
        path.write_text(body, encoding="utf-8")
        _rotate_snapshots()
        return {
            "filename": filename,
            "size_bytes": path.stat().st_size,
            "counts": counts,
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("pre-wipe snapshot failed: %s", exc)
        return {"filename": None, "size_bytes": 0, "counts": {}, "error": str(exc)}


def _list_snapshot_files() -> list[Path]:
    d = _snapshots_dir()
    return sorted(
        [p for p in d.iterdir()
         if p.is_file() and p.name.startswith(_SNAPSHOT_PREFIX) and p.name.endswith(_SNAPSHOT_SUFFIX)],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _rotate_snapshots() -> int:
    """Keep newest _SNAPSHOT_KEEP files; delete the rest. Returns count deleted."""
    files = _list_snapshot_files()
    if len(files) <= _SNAPSHOT_KEEP:
        return 0
    removed = 0
    for stale in files[_SNAPSHOT_KEEP:]:
        try:
            stale.unlink()
            removed += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("snapshot rotate: failed to delete %s: %s", stale.name, exc)
    return removed


def _snapshot_meta(path: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
        tables = (bundle or {}).get("tables") or {}
        if isinstance(tables, dict):
            for tname, rows in tables.items():
                if isinstance(rows, list):
                    counts[tname] = len(rows)
        audit_rows = (bundle or {}).get("audit_log_recent") or []
        if isinstance(audit_rows, list):
            counts["audit_log_recent"] = len(audit_rows)
    except Exception:
        counts = {}
    stat = path.stat()
    return {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "counts": counts,
    }


@router.get("/snapshots")
def list_snapshots() -> dict:
    files = _list_snapshot_files()
    items = [_snapshot_meta(p) for p in files]
    return {"ok": True, "data": {"snapshots": items, "count": len(items), "keep": _SNAPSHOT_KEEP}}


class _RestoreReq(BaseModel):
    filename: str


@router.post("/snapshots/restore")
def restore_snapshot(req: _RestoreReq) -> dict:
    name = _validate_filename(req.filename)
    path = _snapshots_dir() / name
    if not path.is_file():
        raise HTTPException(404, f"snapshot not found: {name}")
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(400, f"snapshot is not valid JSON: {exc}")
    result = _import_bundle(bundle)
    audit("snapshot_restore", "data", None, filename=name)
    if isinstance(result, dict):
        data = result.get("data") or {}
        data["restored_from"] = name
        result["data"] = data
    return result


@router.delete("/snapshots/{filename}")
def delete_snapshot(filename: str) -> dict:
    name = _validate_filename(filename)
    path = _snapshots_dir() / name
    if not path.is_file():
        raise HTTPException(404, f"snapshot not found: {name}")
    try:
        path.unlink()
    except Exception as exc:
        raise HTTPException(500, f"could not delete snapshot: {exc}")
    audit("snapshot_delete", "data", None, filename=name)
    return {"ok": True, "data": {"filename": name, "deleted": True}}


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
    # Pre-wipe snapshot: capture current state to disk BEFORE the
    # transaction starts so a wipe-then-realize-you-needed-something
    # recovery is one POST /api/data/snapshots/restore away.
    snapshot_info = _write_pre_wipe_snapshot()
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

    # Also clear the filesystem artifacts: tailored resume files + built
    # application packets + uploaded evidence files. Otherwise the user
    # "deletes their data" but stale packets / resumes from prior jobs
    # remain on disk.
    import shutil
    fs_counts: dict[str, int] = {"resumes": 0, "packets": 0, "uploads": 0, "calendar_ics": 0}
    try:
        for f in settings.resumes_dir.iterdir():
            if f.name == ".gitkeep": continue
            try:
                if f.is_dir(): shutil.rmtree(f)
                else: f.unlink()
                fs_counts["resumes"] += 1
            except Exception:
                pass
        for d in settings.packets_dir.iterdir():
            if d.name == ".gitkeep": continue
            try:
                if d.is_dir(): shutil.rmtree(d)
                else: d.unlink()
                fs_counts["packets"] += 1
            except Exception:
                pass
        for f in settings.uploads_dir.iterdir():
            if f.name == ".gitkeep": continue
            try:
                if f.is_dir(): shutil.rmtree(f)
                else: f.unlink()
                fs_counts["uploads"] += 1
            except Exception:
                pass
        ics_dir = settings.data_dir / "calendar_ics"
        if ics_dir.exists():
            for f in ics_dir.iterdir():
                try:
                    f.unlink()
                    fs_counts["calendar_ics"] += 1
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        log.warning("wipe: filesystem cleanup partial failure: %s", exc)

    # audit AFTER wipe so the record survives (audit_log was just cleared)
    audit("data_wipe", "data", None, counts=counts, fs_counts=fs_counts,
          snapshot=snapshot_info)
    return {
        "ok": True,
        "data": {
            "counts": counts,
            "fs_counts": fs_counts,
            "snapshot": snapshot_info,
        },
    }
