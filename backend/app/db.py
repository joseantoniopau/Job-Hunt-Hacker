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
    """CREATE INDEX IF NOT EXISTS idx_job_posted_at ON job_posting(posted_at DESC)""",

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
        last_error TEXT,
        last_error_ts REAL,
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

    """CREATE TABLE IF NOT EXISTS gap_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        job_id INTEGER,
        missing_keyword TEXT NOT NULL,
        FOREIGN KEY (job_id) REFERENCES job_posting(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS idx_gap_event_ts ON gap_event(ts)""",
    """CREATE INDEX IF NOT EXISTS idx_gap_event_keyword ON gap_event(missing_keyword)""",
    """CREATE INDEX IF NOT EXISTS idx_gap_event_job ON gap_event(job_id)""",

    # NOTE: the resume_id FK only applies to FRESH databases — `IF NOT
    # EXISTS` leaves existing tables untouched (no rebuild), so DBs created
    # before this FK keep working with the plain resume_id column.
    """CREATE TABLE IF NOT EXISTS effectiveness_event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        application_id INTEGER,
        resume_id INTEGER,
        outcome TEXT NOT NULL,
        notes TEXT,
        FOREIGN KEY (application_id) REFERENCES application(id) ON DELETE CASCADE,
        FOREIGN KEY (resume_id) REFERENCES tailored_resume(id) ON DELETE SET NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_eff_event_ts ON effectiveness_event(ts)""",
    """CREATE INDEX IF NOT EXISTS idx_eff_event_resume ON effectiveness_event(resume_id)""",
    """CREATE INDEX IF NOT EXISTS idx_effect_resume ON effectiveness_event(resume_id)""",
    """CREATE INDEX IF NOT EXISTS idx_eff_event_outcome ON effectiveness_event(outcome)""",

    # ---- Headhunter mode tables (additive) ----
    """CREATE TABLE IF NOT EXISTS connection (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        relationship TEXT,
        company TEXT,
        role TEXT,
        contact TEXT,
        notes TEXT,
        last_contacted_at REAL,
        created_at REAL,
        updated_at REAL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_connection_company ON connection(company)""",
    """CREATE INDEX IF NOT EXISTS idx_connection_name ON connection(name)""",

    """CREATE TABLE IF NOT EXISTS connection_company (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        connection_id INTEGER NOT NULL,
        company TEXT NOT NULL,
        role TEXT,
        created_at REAL,
        FOREIGN KEY (connection_id) REFERENCES connection(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS idx_connection_company_conn ON connection_company(connection_id)""",
    """CREATE INDEX IF NOT EXISTS idx_connection_company_co ON connection_company(company)""",

    # ---- LLM observability ----
    """CREATE TABLE IF NOT EXISTS llm_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        finished_ts REAL,
        provider TEXT,
        model TEXT,
        stage TEXT NOT NULL,
        target_type TEXT,
        target_id INTEGER,
        system_text TEXT,
        user_text TEXT,
        output_text TEXT,
        status TEXT NOT NULL,
        error TEXT,
        prompt_chars INTEGER,
        output_chars INTEGER,
        elapsed_ms INTEGER
    )""",
    """CREATE INDEX IF NOT EXISTS idx_llm_run_ts ON llm_run(ts)""",
    """CREATE INDEX IF NOT EXISTS idx_llm_run_stage ON llm_run(stage)""",
    """CREATE INDEX IF NOT EXISTS idx_llm_run_status ON llm_run(status)""",

    # ---- LLM-enhanced profile inference: store proposal for human gate ----
    """CREATE TABLE IF NOT EXISTS profile_proposal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL NOT NULL,
        source TEXT NOT NULL,
        deterministic_json TEXT,
        llm_json TEXT,
        llm_run_id INTEGER,
        status TEXT DEFAULT 'pending',
        accepted_fields_json TEXT,
        applied_at REAL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_profile_proposal_status ON profile_proposal(status)""",

    # ---- LLM rerank scores (kept separate from deterministic job_match) ----
    """CREATE TABLE IF NOT EXISTS llm_job_score (
        job_id INTEGER PRIMARY KEY,
        semantic_score REAL,
        fit_summary TEXT,
        strengths_json TEXT,
        gaps_json TEXT,
        red_flags_json TEXT,
        recommended_action TEXT,
        llm_run_id INTEGER,
        created_at REAL,
        FOREIGN KEY (job_id) REFERENCES job_posting(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS idx_llm_job_score_score ON llm_job_score(semantic_score)""",

    # ---- Interview prep packets + practice sessions ----
    """CREATE TABLE IF NOT EXISTS interview_prep_packet (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER,
        job_id INTEGER,
        created_at REAL NOT NULL,
        company_brief TEXT,
        behavioral_questions_json TEXT,
        technical_questions_json TEXT,
        scenario_questions_json TEXT,
        star_skeletons_json TEXT,
        llm_run_id INTEGER,
        FOREIGN KEY (application_id) REFERENCES application(id) ON DELETE CASCADE,
        FOREIGN KEY (job_id) REFERENCES job_posting(id) ON DELETE SET NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_iv_prep_app ON interview_prep_packet(application_id)""",

    """CREATE TABLE IF NOT EXISTS interview_practice_session (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER,
        prep_packet_id INTEGER,
        started_at REAL NOT NULL,
        finished_at REAL,
        status TEXT DEFAULT 'active',
        question_count INTEGER DEFAULT 0,
        avg_score REAL,
        FOREIGN KEY (application_id) REFERENCES application(id) ON DELETE CASCADE,
        FOREIGN KEY (prep_packet_id) REFERENCES interview_prep_packet(id) ON DELETE SET NULL
    )""",
    """CREATE INDEX IF NOT EXISTS idx_iv_session_app ON interview_practice_session(application_id)""",

    """CREATE TABLE IF NOT EXISTS interview_practice_turn (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        turn_index INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        question_type TEXT,
        user_answer TEXT,
        feedback_text TEXT,
        score REAL,
        evidence_used_json TEXT,
        llm_run_id INTEGER,
        created_at REAL NOT NULL,
        FOREIGN KEY (session_id) REFERENCES interview_practice_session(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS idx_iv_turn_session ON interview_practice_turn(session_id)""",

    # ---- Offer analysis ----
    """CREATE TABLE IF NOT EXISTS offer_analysis (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        application_id INTEGER,
        created_at REAL NOT NULL,
        offer_text TEXT,
        components_json TEXT,
        market_comparison_json TEXT,
        counter_script_json TEXT,
        red_flags_json TEXT,
        equity_analysis_json TEXT,
        total_score REAL,
        recommendation TEXT,
        llm_run_id INTEGER,
        FOREIGN KEY (application_id) REFERENCES application(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS idx_offer_analysis_app ON offer_analysis(application_id)""",

    # ---- Career snapshot — LLM-generated narrative of who the user is + where to go ----
    """CREATE TABLE IF NOT EXISTS career_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL NOT NULL,
        basic_info_json TEXT,
        what_they_do TEXT,
        career_stage TEXT,
        career_stage_reasoning TEXT,
        strengths_json TEXT,
        next_steps_json TEXT,
        job_recommendations_json TEXT,
        narrative TEXT,
        llm_run_id INTEGER,
        is_latest INTEGER DEFAULT 1
    )""",
    """CREATE INDEX IF NOT EXISTS idx_career_snapshot_latest ON career_snapshot(is_latest, created_at)""",

    # ---- JD change tracking: point-in-time copies of a job's description.
    # change_summary holds JSON diff stats vs the previous snapshot
    # ({"chars_added":..,"chars_removed":..,"added_keywords":[..],...});
    # the very first snapshot per job is the baseline ({"initial": true}).
    """CREATE TABLE IF NOT EXISTS job_posting_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER NOT NULL,
        content_hash TEXT,
        description TEXT,
        captured_at REAL,
        change_summary TEXT,
        FOREIGN KEY (job_id) REFERENCES job_posting(id) ON DELETE CASCADE
    )""",
    """CREATE INDEX IF NOT EXISTS idx_jps_job ON job_posting_snapshot(job_id)""",

    # ---- In-app notifications (deadline reminders, etc.) ----
    """CREATE TABLE IF NOT EXISTS notification (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        kind TEXT NOT NULL,
        title TEXT,
        body TEXT,
        read INTEGER DEFAULT 0,
        target_type TEXT,
        target_id INTEGER
    )""",
    """CREATE INDEX IF NOT EXISTS idx_notification_read ON notification(read, ts)""",
    """CREATE INDEX IF NOT EXISTS idx_notification_kind ON notification(kind)""",
]


def _init_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    # ----- lightweight migrations for existing DBs -----
    _ensure_column(conn, "email_event", "status", "TEXT")
    _ensure_column(conn, "email_event", "status_updated_at", "REAL")
    _ensure_column(conn, "saved_search", "last_error", "TEXT")
    _ensure_column(conn, "saved_search", "last_error_ts", "REAL")
    # per-adapter circuit breaker (services/job_sources/pipeline.py)
    _ensure_column(conn, "source_state", "consecutive_failures", "INTEGER DEFAULT 0")
    _ensure_column(conn, "source_state", "disabled_until", "REAL")
    # user timezone for local-time interview-slot suggestions (calendar)
    _ensure_column(conn, "user_profile", "timezone", "TEXT")
    # application deadlines (PATCH /api/applications/{id}) + one-shot reminder stamp
    _ensure_column(conn, "application", "deadline_at", "REAL")
    _ensure_column(conn, "application", "deadline_source", "TEXT")
    _ensure_column(conn, "application", "reminder_sent_at", "REAL")
    # job_posting (source, external_id) uniqueness: dedupe rows that predate
    # the constraint, THEN create the partial unique index (creation would
    # fail if duplicates were still present).
    _dedupe_job_external_ids(conn)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_source_ext "
        "ON job_posting(source, external_id) WHERE external_id IS NOT NULL"
    )
    # seed singleton profile row
    cur = conn.execute("SELECT id FROM user_profile WHERE id = 1")
    if cur.fetchone() is None:
        now = time.time()
        conn.execute(
            "INSERT INTO user_profile (id, currency, mode, created_at, updated_at) VALUES (1, 'USD', 'assisted', ?, ?)",
            (now, now),
        )


def _dedupe_job_external_ids(conn: sqlite3.Connection) -> None:
    """Collapse job_posting rows sharing (source, external_id) so the partial
    unique index idx_job_source_ext can be created on DBs that predate it.

    Empty-string external ids are normalized to NULL first — adapters that
    don't supply one default to "" and those rows are NOT duplicates of each
    other (the partial index ignores NULLs). For real duplicates the lowest
    id is kept; FK cascades clean up children (job_match, application,
    cover_letter, gap_event, llm_job_score, ...).
    """
    conn.execute("UPDATE job_posting SET external_id = NULL WHERE external_id = ''")
    conn.execute(
        "DELETE FROM job_posting WHERE external_id IS NOT NULL AND id NOT IN ("
        " SELECT MIN(id) FROM job_posting"
        " WHERE external_id IS NOT NULL"
        " GROUP BY source, external_id)"
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
