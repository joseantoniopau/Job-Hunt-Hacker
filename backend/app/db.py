"""SQLite vault. Single connection per request via dependency.

Schema is created idempotently on init. All FK enforcement on.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import settings

# Reentrant lock + per-thread "are we already in a tx?" flag so that a
# tx() inside another tx() degrades to a no-op (yields the same connection
# without nested BEGIN/COMMIT — SQLite forbids that).
_lock = threading.RLock()
_in_tx = threading.local()


def _cleanup_stale_wal_files() -> None:
    """If the main DB file is missing but WAL/SHM files exist, SQLite refuses
    to open the connection with `disk I/O error`. This happens whenever a
    user manually deletes data/jhh.db to "start over" but leaves the
    journal sidecars behind. Detect and clean up so the user doesn't have
    to know about SQLite internals.
    """
    main = settings.db_path
    if main.exists():
        return  # nothing to do — normal restart
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = main.with_name(main.name + suffix)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except Exception:
                pass


def _connect() -> sqlite3.Connection:
    _cleanup_stale_wal_files()
    conn = sqlite3.connect(str(settings.db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = _connect()
                _init_schema(_conn)
    return _conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Acquire the write lock + BEGIN/COMMIT around the block.

    Reentrancy-safe: if we're already inside a tx() on this thread,
    just yield the live connection without nested BEGIN (SQLite would
    error: "cannot start a transaction within a transaction").
    """
    conn = get_conn()
    # If we're already in a transaction on this thread, just yield the conn.
    if getattr(_in_tx, "depth", 0) > 0:
        _in_tx.depth += 1
        try:
            yield conn
        finally:
            _in_tx.depth -= 1
        return

    with _lock:
        _in_tx.depth = 1
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            _in_tx.depth = 0


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS user_profile (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        name TEXT,
        email TEXT,
        phone TEXT,
        location TEXT,
        linkedin_url TEXT,
        github_url TEXT,
        portfolio_url TEXT,
        target_titles TEXT,
        target_keywords TEXT,
        excluded_keywords TEXT,
        preferred_locations TEXT,
        remote_preference TEXT,
        employment_types TEXT,
        minimum_salary INTEGER,
        preferred_salary INTEGER,
        currency TEXT DEFAULT 'USD',
        seniority_targets TEXT,
        industries TEXT,
        excluded_industries TEXT,
        preferred_companies TEXT,
        excluded_companies TEXT,
        visa_preferences TEXT,
        interview_availability_json TEXT,
        scoring_weights_json TEXT,
        mode TEXT DEFAULT 'assisted',
        created_at REAL,
        updated_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS evidence_source (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type TEXT NOT NULL,
        title TEXT,
        filename TEXT,
        url TEXT,
        raw_text TEXT,
        parsed_json TEXT,
        content_hash TEXT,
        ingestion_status TEXT DEFAULT 'parsed',
        created_at REAL,
        updated_at REAL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence_source(content_hash)""",
    """CREATE INDEX IF NOT EXISTS idx_evidence_type ON evidence_source(source_type)""",

    """CREATE TABLE IF NOT EXISTS career_claim (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        claim_type TEXT NOT NULL,
        claim_text TEXT NOT NULL,
        normalized_claim TEXT,
        date_start TEXT,
        date_end TEXT,
        employer TEXT,
        project TEXT,
        skill TEXT,
        tool TEXT,
        confidence REAL DEFAULT 0.5,
        evidence_strength TEXT DEFAULT 'medium',
        user_verified INTEGER DEFAULT 0,
        allowed_for_resume INTEGER DEFAULT 1,
        contradiction_status TEXT DEFAULT 'none',
        created_at REAL,
        FOREIGN KEY (source_id) REFERENCES evidence_source(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS idx_claim_source ON career_claim(source_id)""",
    """CREATE INDEX IF NOT EXISTS idx_claim_type ON career_claim(claim_type)""",
    """CREATE INDEX IF NOT EXISTS idx_claim_skill ON career_claim(skill)""",

    """CREATE TABLE IF NOT EXISTS career_fact (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact_type TEXT NOT NULL,
        text TEXT NOT NULL,
        normalized_text TEXT,
        related_claim_ids TEXT,
        confidence REAL DEFAULT 0.5,
        user_verified INTEGER DEFAULT 0,
        allowed_for_resume INTEGER DEFAULT 1,
        created_at REAL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_fact_type ON career_fact(fact_type)""",

    """CREATE TABLE IF NOT EXISTS embedding (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_type TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        vector BLOB NOT NULL,
        dim INTEGER NOT NULL,
        model TEXT,
        created_at REAL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_embed_owner ON embedding(owner_type, owner_id)""",

    """CREATE TABLE IF NOT EXISTS resume_document (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT,
        file_type TEXT,
        raw_text TEXT,
        parsed_json TEXT,
        is_master INTEGER DEFAULT 0,
        created_at REAL
    )""",

    """CREATE TABLE IF NOT EXISTS job_posting (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        external_id TEXT,
        source TEXT NOT NULL,
        title TEXT NOT NULL,
        company TEXT,
        location TEXT,
        remote_type TEXT,
        employment_type TEXT,
        salary_min INTEGER,
        salary_max INTEGER,
        currency TEXT,
        bonus_equity_text TEXT,
        description TEXT,
        requirements TEXT,
        benefits TEXT,
        apply_url TEXT,
        company_url TEXT,
        posted_at TEXT,
        discovered_at REAL,
        raw_json TEXT,
        hash TEXT UNIQUE,
        status TEXT DEFAULT 'new'
    )""",
    """CREATE INDEX IF NOT EXISTS idx_job_source ON job_posting(source)""",
    """CREATE INDEX IF NOT EXISTS idx_job_company ON job_posting(company)""",
    """CREATE INDEX IF NOT EXISTS idx_job_status ON job_posting(status)""",

    """CREATE TABLE IF NOT EXISTS job_match (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        overall_score REAL NOT NULL,
        skills_score REAL,
        experience_score REAL,
        salary_score REAL,
        location_score REAL,
        seniority_score REAL,
        keyword_score REAL,
        evidence_score REAL,
        explanation TEXT,
        matched_keywords TEXT,
        transferable_keywords TEXT,
        missing_keywords TEXT,
        unsupported_keywords TEXT,
        red_flags TEXT,
        recommended_resume_strategy TEXT,
        created_at REAL,
        FOREIGN KEY (job_id) REFERENCES job_posting(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS idx_match_job ON job_match(job_id)""",
    """CREATE INDEX IF NOT EXISTS idx_match_score ON job_match(overall_score)""",

    """CREATE TABLE IF NOT EXISTS tailored_resume (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER,
        base_resume_id INTEGER,
        resume_type TEXT,
        markdown TEXT,
        plain_text TEXT,
        docx_path TEXT,
        pdf_path TEXT,
        provenance_json TEXT,
        honesty_report_json TEXT,
        ats_report_json TEXT,
        keyword_report_json TEXT,
        created_at REAL,
        FOREIGN KEY (job_id) REFERENCES job_posting(id) ON DELETE SET NULL,
        FOREIGN KEY (base_resume_id) REFERENCES resume_document(id) ON DELETE SET NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_tailored_job ON tailored_resume(job_id)""",

    """CREATE TABLE IF NOT EXISTS cover_letter (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        text TEXT NOT NULL,
        provenance_json TEXT,
        created_at REAL,
        FOREIGN KEY (job_id) REFERENCES job_posting(id) ON DELETE CASCADE
    )""",

    """CREATE TABLE IF NOT EXISTS application (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        status TEXT DEFAULT 'saved',
        mode TEXT,
        applied_at REAL,
        application_url TEXT,
        resume_id INTEGER,
        cover_letter_id INTEGER,
        notes TEXT,
        last_contact_at REAL,
        next_followup_at REAL,
        audit_json TEXT,
        FOREIGN KEY (job_id) REFERENCES job_posting(id) ON DELETE CASCADE,
        FOREIGN KEY (resume_id) REFERENCES tailored_resume(id) ON DELETE SET NULL,
        FOREIGN KEY (cover_letter_id) REFERENCES cover_letter(id) ON DELETE SET NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_app_status ON application(status)""",
    """CREATE INDEX IF NOT EXISTS idx_app_job ON application(job_id)""",

    """CREATE TABLE IF NOT EXISTS email_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER,
        sender TEXT,
        subject TEXT,
        body_text TEXT,
        detected_type TEXT,
        received_at REAL,
        raw_json TEXT,
        status TEXT,
        status_updated_at REAL,
        FOREIGN KEY (application_id) REFERENCES application(id) ON DELETE SET NULL
    )""",

    """CREATE TABLE IF NOT EXISTS calendar_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER,
        title TEXT,
        start_time REAL,
        end_time REAL,
        location TEXT,
        meeting_link TEXT,
        status TEXT,
        raw_json TEXT,
        FOREIGN KEY (application_id) REFERENCES application(id) ON DELETE SET NULL
    )""",

    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        actor TEXT,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id INTEGER,
        detail_json TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)""",

    """CREATE TABLE IF NOT EXISTS saved_search (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        query_json TEXT NOT NULL,
        frequency_hours INTEGER DEFAULT 24,
        last_run_at REAL,
        enabled INTEGER DEFAULT 1,
        created_at REAL
    )""",

    """CREATE TABLE IF NOT EXISTS source_state (
        source TEXT PRIMARY KEY,
        enabled INTEGER DEFAULT 1,
        last_run_at REAL,
        last_error TEXT,
        config_json TEXT
    )""",
]


def _init_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    # ----- lightweight migrations for existing DBs -----
    _ensure_column(conn, "email_event", "status", "TEXT")
    _ensure_column(conn, "email_event", "status_updated_at", "REAL")
    # seed singleton profile row
    cur = conn.execute("SELECT id FROM user_profile WHERE id = 1")
    if cur.fetchone() is None:
        now = time.time()
        conn.execute(
            "INSERT INTO user_profile (id, currency, mode, created_at, updated_at) VALUES (1, 'USD', 'assisted', ?, ?)",
            (now, now),
        )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """ALTER TABLE … ADD COLUMN if the column doesn't already exist.

    SQLite's ALTER is forgiving (no DROP needed) and tolerates this from
    multiple workers — but we still check first because the error message
    on "duplicate column" is noisy in logs.
    """
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return
    if any(r[1] == column for r in rows):
        return
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    except sqlite3.Error:
        # Race or pre-existing — silently fine.
        pass


def init_db() -> None:
    """Public entrypoint used by install.sh and tests."""
    get_conn()


# ---- helpers ----

def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    # auto-decode any *_json columns
    for k, v in list(d.items()):
        if k.endswith("_json") and isinstance(v, str) and v.strip():
            try:
                d[k] = json.loads(v)
            except Exception:
                pass
        elif k in ("target_titles", "target_keywords", "excluded_keywords",
                   "preferred_locations", "employment_types", "seniority_targets",
                   "industries", "excluded_industries", "preferred_companies",
                   "excluded_companies", "visa_preferences", "matched_keywords",
                   "transferable_keywords", "missing_keywords", "unsupported_keywords",
                   "red_flags", "related_claim_ids", "requirements", "benefits"):
            if isinstance(v, str) and v.strip():
                try:
                    d[k] = json.loads(v)
                except Exception:
                    d[k] = [s.strip() for s in v.split(",") if s.strip()]
    return d


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [row_to_dict(r) for r in rows]


def audit(action: str, target_type: str = "", target_id: int | None = None, **detail: Any) -> None:
    """Single-statement insert. Safe to call from inside a tx() too —
    `tx()` is reentrancy-aware, but even without that the connection is
    in autocommit mode so a bare INSERT outside a transaction commits
    immediately.
    """
    conn = get_conn()
    # Acquire the lock briefly so the INSERT serializes with concurrent
    # transactions; using the existing tx() context manager so its
    # reentrancy logic kicks in if we're already inside another tx.
    try:
        with tx() as c:
            c.execute(
                "INSERT INTO audit_log (ts, actor, action, target_type, target_id, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), "system", action, target_type, target_id,
                 json.dumps(detail, default=str)),
            )
    except Exception:
        # Last-ditch fallback: write outside any transaction so an audit failure
        # never bubbles up to break a caller's actual operation.
        try:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, target_type, target_id, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), "system", action, target_type, target_id,
                 json.dumps(detail, default=str)),
            )
        except Exception:
            pass
