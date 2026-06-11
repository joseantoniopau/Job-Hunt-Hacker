"""Scheduler reliability: saved-search failure tracking (last_error),
email/calendar retention, nightly DB maintenance, and status() serialization.
"""
from __future__ import annotations

import json
import time

import pytest

from backend.app.db import get_conn, init_db, tx
from backend.app.integrations import scheduler as sched


def _audit_rows(action: str) -> list:
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM audit_log WHERE action = ? ORDER BY id DESC", (action,)
    ).fetchall()


# ---------- saved-search failure tracking ----------

def test_saved_search_failure_writes_last_error(monkeypatch):
    init_db()
    sid = sched.create_saved_search("fail-case", {"query": "python", "sites": ["remotive"]})
    import backend.app.services.job_sources.pipeline as pipeline

    def boom(q, sites):
        raise RuntimeError("boom: upstream 503")

    monkeypatch.setattr(pipeline, "search_all", boom)
    out = sched.run_saved_search_now(sid)
    assert out["ok"] is False
    assert "boom" in out["detail"]

    row = get_conn().execute(
        "SELECT last_error, last_error_ts FROM saved_search WHERE id = ?", (sid,)
    ).fetchone()
    assert row["last_error"] is not None and "boom" in row["last_error"]
    assert row["last_error_ts"] is not None
    fails = [r for r in _audit_rows("saved_search_failed") if r["target_id"] == sid]
    assert fails, "expected a saved_search_failed audit row"


def test_saved_search_success_clears_last_error(monkeypatch):
    init_db()
    sid = sched.create_saved_search("ok-case", {"query": "python", "sites": ["remotive"]})
    with tx() as c:
        c.execute(
            "UPDATE saved_search SET last_error = 'old failure', last_error_ts = ? WHERE id = ?",
            (time.time(), sid),
        )
    import backend.app.services.job_sources.pipeline as pipeline
    monkeypatch.setattr(
        pipeline, "search_all",
        lambda q, sites: {"records": [], "per_source": {}, "errors": {}},
    )
    out = sched.run_saved_search_now(sid)
    assert out["ok"] is True

    row = get_conn().execute(
        "SELECT last_error, last_error_ts, last_run_at FROM saved_search WHERE id = ?", (sid,)
    ).fetchone()
    assert row["last_error"] is None
    assert row["last_error_ts"] is None
    assert row["last_run_at"] is not None


# ---------- email / calendar retention ----------

def test_email_calendar_retention_deletes_only_old_rows():
    init_db()
    now = time.time()
    with tx() as c:
        c.execute("INSERT INTO email_event (sender, subject, received_at) VALUES ('old@x', 'ret-old', ?)",
                  (now - 200 * 86400,))
        c.execute("INSERT INTO email_event (sender, subject, received_at) VALUES ('new@x', 'ret-new', ?)",
                  (now - 86400,))
        c.execute("INSERT INTO email_event (sender, subject, received_at) VALUES ('nots@x', 'ret-no-ts', NULL)")
        c.execute("INSERT INTO calendar_event (title, start_time) VALUES ('ret-old-cal', ?)",
                  (now - 400 * 86400,))
        c.execute("INSERT INTO calendar_event (title, start_time) VALUES ('ret-new-cal', ?)",
                  (now - 86400,))

    out = sched.run_email_calendar_retention()
    assert out["ok"] is True
    assert out["email_retention_days"] == 180
    assert out["calendar_retention_days"] == 365
    assert out["email_deleted"] >= 1
    assert out["calendar_deleted"] >= 1

    conn = get_conn()
    subjects = {r[0] for r in conn.execute("SELECT subject FROM email_event").fetchall()}
    assert "ret-old" not in subjects
    assert {"ret-new", "ret-no-ts"} <= subjects  # recent + untimestamped survive
    titles = {r[0] for r in conn.execute("SELECT title FROM calendar_event").fetchall()}
    assert "ret-old-cal" not in titles
    assert "ret-new-cal" in titles
    assert _audit_rows("email_calendar_retention_purged")


def test_email_calendar_retention_env_override(monkeypatch):
    init_db()
    monkeypatch.setenv("JHH_EMAIL_RETENTION_DAYS", "10")
    monkeypatch.setenv("JHH_CALENDAR_RETENTION_DAYS", "20")
    now = time.time()
    with tx() as c:
        c.execute("INSERT INTO email_event (sender, subject, received_at) VALUES ('mid@x', 'ret-mid', ?)",
                  (now - 30 * 86400,))
        c.execute("INSERT INTO calendar_event (title, start_time) VALUES ('ret-mid-cal', ?)",
                  (now - 30 * 86400,))

    out = sched.run_email_calendar_retention()
    assert out["email_retention_days"] == 10
    assert out["calendar_retention_days"] == 20
    conn = get_conn()
    assert conn.execute(
        "SELECT COUNT(*) FROM email_event WHERE subject = 'ret-mid'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM calendar_event WHERE title = 'ret-mid-cal'"
    ).fetchone()[0] == 0


# ---------- nightly DB maintenance ----------

def test_db_maintenance_runs_and_audits():
    init_db()
    out = sched.run_db_maintenance()
    assert out["ok"] is True
    assert out["optimized"] is True
    assert out["wal_checkpoint"] is not None  # WAL mode -> (busy, log, ckpt)
    assert _audit_rows("db_maintenance_run")


# ---------- status() ----------

def test_status_includes_saved_search_state_and_serializes():
    init_db()
    sid = sched.create_saved_search("status-case", {"query": "go"})
    with tx() as c:
        c.execute(
            "UPDATE saved_search SET last_error = 'bad fetch', last_error_ts = ?, last_run_at = ? "
            "WHERE id = ?",
            (time.time(), time.time(), sid),
        )
    st = sched.status()
    assert isinstance(st["jobs"], list)
    by_id = {s["id"]: s for s in st["saved_searches"]}
    assert sid in by_id
    assert by_id[sid]["last_error"] == "bad fetch"
    assert by_id[sid]["last_run_at"] is not None
    for j in st["jobs"]:
        assert j["next_run_time"] is None or isinstance(j["next_run_time"], str)
    json.dumps(st)  # whole payload must be JSON-serializable


def test_status_with_running_scheduler():
    init_db()
    sid = sched.create_saved_search("running-case", {"query": "rust"})
    sched.start()
    if not sched.is_running():
        pytest.skip("apscheduler unavailable in this environment")
    try:
        st = sched.status()
        ids = {j["id"] for j in st["jobs"]}
        assert "jhh.email_calendar_retention" in ids
        assert "jhh.db_maintenance" in ids
        assert "jhh.audit_retention" in ids
        assert f"saved_search_{sid}" in ids
        for j in st["jobs"]:
            if j["next_run_time"] is not None:
                assert isinstance(j["next_run_time"], str)
                assert "T" in j["next_run_time"]  # ISO 8601
            if j["id"] == f"saved_search_{sid}":
                assert "last_error" in j
                assert "last_run_at" in j
        json.dumps(st)
    finally:
        sched.shutdown()
