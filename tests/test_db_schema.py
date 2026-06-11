"""Verify the SQLite schema initializes and all expected tables exist."""
import time

from backend.app.db import init_db, get_conn, _init_schema
from backend.app.services.job_sources.base import JobRecord
from backend.app.services.job_sources.pipeline import persist


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


# ---------- job_posting indexes + bootstrap dedup ----------

def _index_sql(conn, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone()
    return row[0] if row else None


def test_job_posting_indexes_exist():
    init_db()
    conn = get_conn()
    sql = _index_sql(conn, "idx_job_source_ext")
    assert sql is not None
    assert "UNIQUE" in sql.upper()
    assert "external_id IS NOT NULL" in sql  # partial index
    assert _index_sql(conn, "idx_job_posted_at") is not None
    assert _index_sql(conn, "idx_effect_resume") is not None


def test_bootstrap_dedupes_duplicate_external_ids():
    """Simulate a pre-constraint DB: drop the unique index, insert dup rows,
    re-run bootstrap. The lowest id survives, children of the losers cascade
    away, and empty-string external ids are normalized to NULL (NOT deduped)."""
    init_db()
    conn = get_conn()
    conn.execute("DROP INDEX IF EXISTS idx_job_source_ext")
    now = time.time()
    keep_id = conn.execute(
        "INSERT INTO job_posting (external_id, source, title, hash, discovered_at) "
        "VALUES ('dup-1', 'dedup_src', 'Engineer A', 'h-dedup-1', ?)",
        (now,),
    ).lastrowid
    lose_id = conn.execute(
        "INSERT INTO job_posting (external_id, source, title, hash, discovered_at) "
        "VALUES ('dup-1', 'dedup_src', 'Engineer B', 'h-dedup-2', ?)",
        (now,),
    ).lastrowid
    conn.execute(
        "INSERT INTO job_match (job_id, overall_score, created_at) VALUES (?, 50.0, ?)",
        (lose_id, now),
    )
    conn.execute(
        "INSERT INTO job_posting (external_id, source, title, hash) "
        "VALUES ('', 'dedup_src', 'No id A', 'h-dedup-3')"
    )
    conn.execute(
        "INSERT INTO job_posting (external_id, source, title, hash) "
        "VALUES ('', 'dedup_src', 'No id B', 'h-dedup-4')"
    )

    _init_schema(conn)  # what a restart runs

    rows = conn.execute(
        "SELECT id FROM job_posting WHERE source = 'dedup_src' AND external_id = 'dup-1'"
    ).fetchall()
    assert [r[0] for r in rows] == [keep_id]
    # FK cascade removed the deleted duplicate's child row
    assert conn.execute(
        "SELECT COUNT(*) FROM job_match WHERE job_id = ?", (lose_id,)
    ).fetchone()[0] == 0
    # '' ids both survive, normalized to NULL (partial index ignores them)
    null_rows = conn.execute(
        "SELECT external_id FROM job_posting WHERE source = 'dedup_src' AND title LIKE 'No id %'"
    ).fetchall()
    assert len(null_rows) == 2
    assert all(r[0] is None for r in null_rows)
    assert _index_sql(conn, "idx_job_source_ext") is not None


def test_unique_index_blocks_same_source_external_id():
    init_db()
    conn = get_conn()
    conn.execute(
        "INSERT INTO job_posting (external_id, source, title, hash) "
        "VALUES ('uniq-1', 'uniq_src', 'First', 'h-uniq-1')"
    )
    cur = conn.execute(
        "INSERT OR IGNORE INTO job_posting (external_id, source, title, hash) "
        "VALUES ('uniq-1', 'uniq_src', 'Second', 'h-uniq-2')"
    )
    assert cur.rowcount == 0
    n = conn.execute(
        "SELECT COUNT(*) FROM job_posting WHERE source = 'uniq_src' AND external_id = 'uniq-1'"
    ).fetchone()[0]
    assert n == 1


# ---------- persist(): cross-source dedup ----------

def _rec(**kw) -> JobRecord:
    base = dict(
        source="remotive",
        title="Staff Engineer",
        company="AcmeCo",
        location="Remote - US",
        apply_url="https://www.acme.com/jobs/123?utm_source=feed",
        posted_at="2026-06-01",
    )
    base.update(kw)
    return JobRecord(**base)


def test_persist_cross_source_dedup_by_url():
    init_db()
    assert persist([_rec(company="UrlDedupCo")])["inserted"] == 1
    # same job on another board: www/query/trailing-slash differences only
    out = persist([_rec(source="weworkremotely", company="UrlDedupCo",
                        location="Anywhere", apply_url="https://acme.com/jobs/123/")])
    assert out["inserted"] == 0
    assert out["duplicates"] == 1


def test_persist_cross_source_dedup_by_location():
    init_db()
    assert persist([_rec(company="LocDedupCo",
                         apply_url="https://a.example/jobs/1")])["inserted"] == 1
    out = persist([_rec(source="lever", company="LocDedupCo",
                        apply_url="https://b.example/other/2")])  # same location
    assert out["inserted"] == 0
    assert out["duplicates"] == 1


def test_persist_cross_source_distinct_job_inserts():
    init_db()
    assert persist([_rec(company="DistinctCo", location="Berlin",
                         apply_url="https://a.example/jobs/1")])["inserted"] == 1
    out = persist([_rec(source="lever", company="DistinctCo", location="NYC",
                        apply_url="https://b.example/jobs/2")])
    assert out["inserted"] == 1  # different url AND location -> not a dup


def test_persist_cross_source_dedup_window_expires():
    init_db()
    conn = get_conn()
    out1 = persist([_rec(company="WindowCo", location="Lisbon",
                         apply_url="https://w.example/jobs/9")])
    assert out1["inserted"] == 1
    conn.execute(
        "UPDATE job_posting SET discovered_at = ? WHERE id = ?",
        (time.time() - 15 * 86400, out1["ids"][0]),
    )
    out2 = persist([_rec(source="lever", company="WindowCo", location="Lisbon",
                         apply_url="https://w.example/jobs/9")])
    assert out2["inserted"] == 1  # outside the 14-day window -> new row


def test_persist_normalizes_empty_external_id():
    init_db()
    out = persist([
        _rec(company="NullExtCo", title="Role A", external_id="",
             location="Madrid", apply_url="https://n.example/a"),
        _rec(company="NullExtCo", title="Role B", external_id="",
             location="Porto", apply_url="https://n.example/b"),
    ])
    assert out["inserted"] == 2  # '' ids must not collide under the unique index
    rows = get_conn().execute(
        "SELECT external_id FROM job_posting WHERE company = 'NullExtCo'"
    ).fetchall()
    assert len(rows) == 2
    assert all(r[0] is None for r in rows)
