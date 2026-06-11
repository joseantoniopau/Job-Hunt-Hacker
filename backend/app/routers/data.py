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

from fastapi import APIRouter, Form, HTTPException, Query, UploadFile, File
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

# Substituted for free-text PII fields when redaction is requested.
_REDACTED = "[redacted]"
# Loose email matcher used to find addresses inside composite strings like
# "Jane Recruiter <jane@corp.com>".
_EMAIL_ADDR_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _mask_email(value: Any) -> Any:
    """Mask an email address: first char of the local part + *** + @domain.
    Composite strings ("Name <addr@x.com>") collapse to just the masked
    address so the display name is not leaked either. Non-strings and
    empties pass through unchanged."""
    if not value or not isinstance(value, str):
        return value
    m = _EMAIL_ADDR_RE.search(value)
    if m:
        local, _, domain = m.group(0).partition("@")
        return f"{local[0]}***@{domain}"
    s = value.strip()
    return f"{s[0]}***" if s else value


def _mask_phone(value: Any) -> Any:
    """Mask a phone number, keeping only the last 2 digits."""
    if not value or not isinstance(value, str):
        return value
    digits = re.sub(r"\D", "", value)
    if not digits:
        return "***"
    return "***" + digits[-2:]


def _mask_name(value: Any) -> Any:
    """Mask a person name: first name + last-name initial ("Jane D.")."""
    if not value or not isinstance(value, str):
        return value
    parts = value.split()
    if not parts:
        return value
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[1][0]}."


def _mask_attendees(attendees: Any) -> Any:
    """Mask attendee emails. Attendees may be plain email strings or
    Google-style dicts ({"email": ...}). Anything else passes through."""
    if not isinstance(attendees, list):
        return attendees
    out: list[Any] = []
    for a in attendees:
        if isinstance(a, str):
            out.append(_mask_email(a))
        elif isinstance(a, dict):
            b = dict(a)
            if b.get("email"):
                b["email"] = _mask_email(b["email"])
            out.append(b)
        else:
            out.append(a)
    return out


def _redact_row(table: str, row: dict) -> dict:
    """Return a redacted copy of an exported row for the given table.
    Tables without PII rules pass through untouched (same object)."""
    if table == "user_profile":
        d = dict(row)
        if d.get("email"):
            d["email"] = _mask_email(d["email"])
        if d.get("phone"):
            d["phone"] = _mask_phone(d["phone"])
        if d.get("name"):
            d["name"] = _mask_name(d["name"])
        return d
    if table == "email_event":
        d = dict(row)
        if d.get("body_text"):
            d["body_text"] = _REDACTED
        if d.get("sender"):
            d["sender"] = _mask_email(d["sender"])
        return d
    if table == "calendar_event":
        d = dict(row)
        # description / attendees live inside raw_json (Google Calendar
        # response payload); the columns themselves carry no body text.
        raw = d.get("raw_json")
        if isinstance(raw, str) and raw.strip():
            try:
                obj = json.loads(raw)
            except Exception:  # noqa: BLE001
                obj = None
            if isinstance(obj, dict):
                if obj.get("description"):
                    obj["description"] = _REDACTED
                if "attendees" in obj:
                    obj["attendees"] = _mask_attendees(obj["attendees"])
                d["raw_json"] = json.dumps(obj, default=str)
        # Schema-drift safety: redact column-level fields if they exist.
        if d.get("description"):
            d["description"] = _REDACTED
        return d
    return row


def _build_export_bundle(redact_pii: bool = False) -> tuple[dict[str, Any], dict[str, int]]:
    """Materialize the full export bundle + per-table row counts. Pulled
    out of `export_all` so snapshot creation can reuse it. When
    `redact_pii` is true, PII fields (profile contact info, email bodies /
    senders, calendar descriptions / attendees) are masked; the bundle
    stays structurally import-compatible either way."""
    conn = get_conn()
    bundle: dict[str, Any] = {
        "version": EXPORT_VERSION,
        "app_version": APP_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "redacted": bool(redact_pii),
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
        if redact_pii:
            dicts = [_redact_row(table, d) for d in dicts]
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
def export_all(
    redact_pii: bool = Query(
        default=False,
        description="Mask PII (profile contact info, email bodies/senders, "
                    "calendar descriptions/attendees) in the exported bundle.",
    ),
) -> Response:
    bundle, counts = _build_export_bundle(redact_pii=redact_pii)
    audit("data_export", "data", None, counts=counts, redacted=bool(redact_pii))
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


# ---------- TRACKER IMPORT (Huntr / Teal / generic CSV) ----------

_TRACKER_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_TRACKER_ERROR_LIMIT = 20
# Accept "generic" as a friendly alias for "csv".
_TRACKER_FORMAT_ALIASES = {"huntr": "huntr", "teal": "teal", "csv": "csv",
                           "generic": "csv"}

# Canonical row fields every format funnels into:
#   title, company, url, status, applied_date, saved_date, location,
#   salary (free text), salary_min, salary_max, notes
#
# Header aliases are matched on a normalized form (lowercase, BOM stripped,
# runs of whitespace/underscores collapsed to a single space) so e.g.
# "Applied_Date", "applied date" and "Applied Date" all hit the same key.

# Generic CSV contract: title,company,url,status,applied_date,location,salary,notes
_GENERIC_ALIASES: dict[str, str] = {
    "title": "title",
    "company": "company",
    "url": "url",
    "status": "status",
    "applied date": "applied_date",
    "location": "location",
    "salary": "salary",
    "notes": "notes",
}

# Huntr board export. Their CSV typically carries Title / Company / Location /
# Salary / Url / List (the kanban column the card sits in) / Date Added /
# Description; older and newer exports vary, so aliases are generous and any
# missing column simply yields an empty field.
_HUNTR_ALIASES: dict[str, str] = {
    "title": "title", "job title": "title", "position": "title",
    "company": "company", "company name": "company",
    "url": "url", "job post url": "url", "post url": "url",
    "job url": "url", "link": "url",
    "list": "status", "list name": "status", "status": "status",
    "stage": "status",
    "date added": "saved_date", "created at": "saved_date",
    "applied date": "applied_date", "date applied": "applied_date",
    "applied at": "applied_date", "application date": "applied_date",
    "location": "location",
    "salary": "salary",
    "description": "notes", "notes": "notes",
}

# Teal job tracker export. Typically Company / Role (or Position) / Status /
# URL / Location / Salary (or Min Salary + Max Salary) / Date Saved /
# Date Applied / Notes / Excitement.
_TEAL_ALIASES: dict[str, str] = {
    "role": "title", "position": "title", "job title": "title",
    "title": "title",
    "company": "company", "company name": "company",
    "url": "url", "job post url": "url", "job posting url": "url",
    "job url": "url", "link": "url",
    "status": "status", "stage": "status",
    "date applied": "applied_date", "applied date": "applied_date",
    "date saved": "saved_date",
    "location": "location",
    "salary": "salary", "compensation": "salary", "pay": "salary",
    "min salary": "salary_min", "salary min": "salary_min",
    "max salary": "salary_max", "salary max": "salary_max",
    "notes": "notes",
}

_TRACKER_ALIAS_MAPS: dict[str, dict[str, str]] = {
    "huntr": {**_GENERIC_ALIASES, **_HUNTR_ALIASES},
    "teal": {**_GENERIC_ALIASES, **_TEAL_ALIASES},
    "csv": dict(_GENERIC_ALIASES),
}

# Foreign stage/list names -> this app's pipeline statuses
# (saved/prepared/applied/replied/interview/offer/rejected). Lookup is on the
# normalized form; unknown stages fall back to a substring heuristic and
# finally to "saved" so an exotic kanban column never kills the row.
_STAGE_MAP: dict[str, str] = {
    # saved
    "wishlist": "saved", "bookmarked": "saved", "saved": "saved",
    "interested": "saved", "to apply": "saved", "backlog": "saved",
    # prepared
    "applying": "prepared", "preparing": "prepared", "prepared": "prepared",
    "drafting": "prepared", "in progress": "prepared",
    # applied
    "applied": "applied", "application submitted": "applied",
    "submitted": "applied", "pending": "applied",
    # replied
    "replied": "replied", "contacted": "replied", "in contact": "replied",
    "follow up": "replied", "followed up": "replied", "responded": "replied",
    # interview
    "phone screen": "interview", "screen": "interview",
    "screening": "interview", "interview": "interview",
    "interviewing": "interview", "interviews": "interview",
    "on site": "interview", "onsite": "interview",
    "technical interview": "interview", "final round": "interview",
    # offer
    "offer": "offer", "offers": "offer", "offer received": "offer",
    "negotiating": "offer", "negotiation": "offer", "accepted": "offer",
    "hired": "offer",
    # rejected
    "rejected": "rejected", "rejection": "rejected",
    "not selected": "rejected", "declined": "rejected",
    "no response": "rejected", "closed": "rejected",
    "withdrawn": "rejected", "archived": "rejected", "ghosted": "rejected",
}

# A row whose mapped status is in this set represents a job the user actually
# applied to -> it gets an application row, not just a job_posting.
_APPLIED_STATUSES = {"applied", "replied", "interview", "offer", "rejected"}

_TRACKER_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y",
    "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
    "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M",
)

_SALARY_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kK])?")


def _norm_header(h: str) -> str:
    """Normalize a CSV header/stage token: strip BOM + whitespace, lowercase,
    collapse runs of whitespace/underscores/dashes to single spaces."""
    return re.sub(r"[\s_\-]+", " ", (h or "").replace("\ufeff", "").strip().lower())


def _map_stage(raw_stage: str) -> str:
    """Translate a foreign tracker stage into one of this app's pipeline
    statuses. Unknown stages degrade to 'saved' (job imported, no
    application) rather than erroring."""
    s = _norm_header(raw_stage)
    if not s:
        return "saved"
    if s in _STAGE_MAP:
        return _STAGE_MAP[s]
    # substring heuristics for custom kanban column names
    if "reject" in s or "declin" in s or "no longer" in s:
        return "rejected"
    if "offer" in s or "negotiat" in s or "accept" in s or "hired" in s:
        return "offer"
    if "interview" in s or "screen" in s or "onsite" in s or "on site" in s:
        return "interview"
    if "repl" in s or "contact" in s or "follow" in s or "respon" in s:
        return "replied"
    if "applied" in s or "submit" in s:
        return "applied"
    if "applying" in s or "prepar" in s or "draft" in s or "progress" in s:
        return "prepared"
    return "saved"


def _parse_tracker_date(value: str) -> float | None:
    """Parse a tracker export date into an epoch float. Tries ISO-8601 first
    (with Z tolerance), then a battery of common US/word formats. Returns
    None when unparseable — callers fall back to import time."""
    s = (value or "").strip()
    if not s:
        return None
    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        pass
    for fmt in _TRACKER_DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _parse_salary_text(value: str) -> tuple[int | None, int | None]:
    """Pull (salary_min, salary_max) out of free text like
    "$120,000 - $150,000/yr" or "120k–150k". Figures under 1000 after
    k-expansion are ignored (hourly rates / noise). Single figure ->
    (n, n); nothing usable -> (None, None)."""
    if not value:
        return None, None
    nums: list[int] = []
    for digits, k in _SALARY_NUM_RE.findall(str(value)):
        try:
            n = float(digits.replace(",", ""))
        except ValueError:
            continue
        if k:
            n *= 1000
        if n >= 1000:
            nums.append(int(n))
    if not nums:
        return None, None
    return min(nums), max(nums)


def _parse_salary_single(value: str) -> int | None:
    lo, _hi = _parse_salary_text(value)
    return lo


def _extract_row_fields(row: dict, header_map: dict[str, str]) -> dict[str, str]:
    """Project a raw DictReader row onto the canonical field names. When two
    source columns alias the same field, the first non-empty value wins."""
    fields: dict[str, str] = {}
    for raw_header, field in header_map.items():
        if fields.get(field):
            continue
        v = row.get(raw_header)
        if v is None:
            continue
        v = str(v).strip()
        if v:
            fields[field] = v
    return fields


def _local_dedup_insert_job(source: str, f: dict[str, str],
                            salary_min: int | None, salary_max: int | None,
                            posted_at: str) -> tuple[bool, int | None]:
    """Fallback persistence when services.job_sources.pipeline is not
    importable: dedup by lower(title)+lower(company) across all sources,
    then plain INSERT. Returns (inserted, job_id)."""
    title = f.get("title", "")
    company = f.get("company", "")
    with tx() as conn:
        dup = conn.execute(
            "SELECT id FROM job_posting "
            "WHERE lower(trim(title)) = ? AND lower(trim(company)) = ?",
            (title.strip().lower(), company.strip().lower()),
        ).fetchone()
        if dup:
            return False, None
        cur = conn.execute(
            "INSERT INTO job_posting (external_id, source, title, company, "
            "location, salary_min, salary_max, apply_url, posted_at, "
            "discovered_at, raw_json, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')",
            (f.get("url") or None, source, title, company,
             f.get("location", ""), salary_min, salary_max,
             f.get("url", ""), posted_at, time.time(),
             json.dumps({"imported_fields": f}, default=str)),
        )
        return True, int(cur.lastrowid)


def _persist_imported_job(fmt: str, f: dict[str, str]) -> tuple[bool, int | None]:
    """Insert one imported row as a job_posting under source='import:<fmt>'.

    Reuses services.job_sources.pipeline.persist when importable so imports
    get the exact same dedup the live adapters get: UNIQUE(source,
    external_id), UNIQUE(hash) and the 14-day cross-source
    title+company+(url-or-location) probe. Falls back to a local
    title+company dedup if the pipeline module is unavailable.

    Returns (inserted, job_id); (False, None) means duplicate-skipped.
    """
    source = f"import:{fmt}"
    salary_min = _parse_salary_single(f.get("salary_min", ""))
    salary_max = _parse_salary_single(f.get("salary_max", ""))
    if salary_min is None and salary_max is None:
        salary_min, salary_max = _parse_salary_text(f.get("salary", ""))
    # posted_at: the best date we have for "when this job entered the funnel"
    posted_ts = (_parse_tracker_date(f.get("applied_date", ""))
                 or _parse_tracker_date(f.get("saved_date", "")))
    posted_at = (datetime.fromtimestamp(posted_ts, tz=timezone.utc)
                 .strftime("%Y-%m-%d") if posted_ts else "")
    try:
        from ..services.job_sources import pipeline as job_pipeline
        from ..services.job_sources.base import JobRecord
    except ImportError:
        return _local_dedup_insert_job(source, f, salary_min, salary_max,
                                       posted_at)
    rec = JobRecord(
        source=source,
        title=f.get("title", ""),
        company=f.get("company", ""),
        location=f.get("location", ""),
        salary_min=salary_min,
        salary_max=salary_max,
        description=f.get("notes", ""),
        apply_url=f.get("url", ""),
        posted_at=posted_at,
        # URL doubles as a stable external id so re-importing the same
        # export hits UNIQUE(source, external_id) even if other fields drift.
        external_id=f.get("url", ""),
        raw={"import_format": fmt, "imported_fields": f},
    )
    res = job_pipeline.persist([rec])
    ids = res.get("ids") or []
    if ids:
        return True, int(ids[0])
    return False, None


def _create_imported_application(job_id: int, status: str, f: dict[str, str],
                                 fmt: str) -> int:
    """Insert the application row for an imported job. applied_at comes from
    the export's applied_date when parseable, else import time."""
    now = time.time()
    applied_at = _parse_tracker_date(f.get("applied_date", "")) or now
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO application (job_id, status, mode, notes, applied_at, "
            "application_url, audit_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(job_id), status, "import", f.get("notes", ""), applied_at,
             f.get("url") or None,
             json.dumps([{"ts": now, "action": "imported",
                          "import_format": fmt,
                          "original_status": f.get("status", ""),
                          "status": status}])),
        )
        return int(cur.lastrowid)


@router.post("/import-tracker")
async def import_tracker(
    file: UploadFile = File(...),
    format: str = Form(...),
) -> dict:
    """Import jobs + applications from another tracker's CSV export.

    Request (multipart/form-data):
      * file   — the CSV export (<5 MB, utf-8 / utf-8-sig)
      * format — "huntr" | "teal" | "csv" ("generic" accepted as alias of csv)

    Generic CSV columns: title,company,url,status,applied_date,location,
    salary,notes (title required per row; everything else optional). Huntr
    and Teal headers are mapped via per-format aliases and missing columns
    are tolerated.

    Per row: a job_posting is inserted under source='import:<format>'
    (deduped via the shared job_sources persist pipeline — UNIQUE(source,
    external_id), content hash and cross-source title+company probe). When
    the row's stage maps to a status that implies the user applied
    (applied/replied/interview/offer/rejected) an application row is created
    too, with applied_at taken from the export when present.

    Response: {"ok": true, "data": {"format", "total_rows", "imported_jobs",
    "imported_applications", "skipped_duplicates", "errors" (first 20),
    "error_count"}}.
    """
    import csv as _csv
    import io as _io

    fmt = _TRACKER_FORMAT_ALIASES.get((format or "").strip().lower())
    if not fmt:
        raise HTTPException(
            400,
            f"unknown format: {format!r}; valid: ['csv', 'huntr', 'teal']",
        )
    try:
        raw = await file.read()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not read upload: {exc}")
    if not raw:
        raise HTTPException(400, "empty upload")
    if len(raw) > _TRACKER_MAX_BYTES:
        raise HTTPException(
            413,
            f"file too large: {len(raw)} bytes (max {_TRACKER_MAX_BYTES})",
        )
    # utf-8-sig strips a BOM if present; errors=replace keeps a stray
    # non-utf8 byte from killing the whole import.
    text = raw.decode("utf-8-sig", errors="replace")

    reader = _csv.DictReader(_io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "CSV has no header row")

    aliases = _TRACKER_ALIAS_MAPS[fmt]
    # raw header -> canonical field, preserving CSV column order
    header_map: dict[str, str] = {}
    for h in reader.fieldnames:
        if h is None:
            continue
        field = aliases.get(_norm_header(h))
        if field:
            header_map[h] = field
    if "title" not in header_map.values():
        raise HTTPException(
            400,
            f"no job-title column found for format={fmt}; "
            f"headers seen: {[h for h in reader.fieldnames if h]}",
        )

    imported_jobs = 0
    imported_applications = 0
    skipped_duplicates = 0
    errors: list[str] = []
    error_count = 0
    total_rows = 0

    def _record_error(msg: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < _TRACKER_ERROR_LIMIT:
            errors.append(msg)

    rows_iter = enumerate(reader, start=2)  # data starts on line 2
    while True:
        try:
            line_no, row = next(rows_iter)
        except StopIteration:
            break
        except _csv.Error as exc:
            _record_error(f"csv parse error: {exc}")
            continue
        total_rows += 1
        try:
            f = _extract_row_fields(row, header_map)
            if not f.get("title"):
                _record_error(f"row {line_no}: missing title")
                continue
            status = _map_stage(f.get("status", ""))
            inserted, job_id = _persist_imported_job(fmt, f)
            if not inserted:
                skipped_duplicates += 1
                continue
            imported_jobs += 1
            if status in _APPLIED_STATUSES and job_id is not None:
                _create_imported_application(job_id, status, f, fmt)
                imported_applications += 1
        except Exception as exc:  # noqa: BLE001
            _record_error(f"row {line_no}: {exc}")

    audit("data_import_tracker", "data", None,
          format=fmt, total_rows=total_rows, imported_jobs=imported_jobs,
          imported_applications=imported_applications,
          skipped_duplicates=skipped_duplicates, error_count=error_count)
    return {
        "ok": True,
        "data": {
            "format": fmt,
            "total_rows": total_rows,
            "imported_jobs": imported_jobs,
            "imported_applications": imported_applications,
            "skipped_duplicates": skipped_duplicates,
            "errors": errors,
            "error_count": error_count,
        },
    }


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
