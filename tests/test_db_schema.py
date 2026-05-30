"""Verify the SQLite schema initializes and all expected tables exist."""
from backend.app.db import init_db, get_conn


EXPECTED_TABLES = {
    "user_profile",
    "evidence_source",
    "career_claim",
    "career_fact",
    "embedding",
    "resume_document",
    "job_posting",
    "job_match",
    "tailored_resume",
    "cover_letter",
    "application",
    "email_event",
    "calendar_event",
    "audit_log",
    "saved_search",
    "source_state",
}


def test_schema_has_all_tables():
    init_db()
    conn = get_conn()
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {r[0] for r in rows}
    missing = EXPECTED_TABLES - names
    assert not missing, f"missing tables: {missing}"


def test_user_profile_singleton():
    init_db()
    conn = get_conn()
    row = conn.execute("SELECT id, mode, currency FROM user_profile").fetchone()
    assert row is not None
    assert row[0] == 1
