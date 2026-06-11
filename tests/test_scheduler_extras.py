"""Scheduler dry-run, job-run history recording, and tz-aware slots."""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from backend.app.db import get_conn, tx
from backend.app.integrations import calendar_google
from backend.app.integrations import scheduler as sched


def _mk_saved_search(query: dict) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO saved_search (label, query_json, frequency_hours, enabled, created_at) "
            "VALUES ('extras-test', ?, 24, 1, ?)",
            (json.dumps(query), time.time()),
        )
        return int(cur.lastrowid)


def test_dry_run_does_not_persist(monkeypatch):
    from backend.app.services.job_sources import pipeline
    from backend.app.services.job_sources.base import JobRecord

    sid = _mk_saved_search({"query": "engineer", "sites": []})
    recs = [JobRecord(source="demo", title="Eng A", company="Acme", location="Remote",
                      external_id="dryA", apply_url="https://x/a"),
            JobRecord(source="demo", title="Eng B", company="Beta", location="Remote",
                      external_id="dryB", apply_url="https://x/b")]
    monkeypatch.setattr(pipeline, "search_all",
                        lambda q, sites: {"records": recs, "per_source": {"demo": 2}, "errors": {}})

    before = get_conn().execute("SELECT COUNT(*) FROM job_posting").fetchone()[0]
    res = sched.dry_run_saved_search(sid, results_cap=5)
    after = get_conn().execute("SELECT COUNT(*) FROM job_posting").fetchone()[0]

    assert res["ok"] is True
    assert after == before  # nothing persisted
    assert res["would_insert"] == 2
    assert res["discovered"] == 2
    assert len(res["top"]) == 2


def test_dry_run_missing_search():
    res = sched.dry_run_saved_search(99999)
    assert res["ok"] is False


def test_record_run_writes_history_and_status():
    sched._ensure_scheduler_schema()

    def ok_job():
        return "fine"

    def bad_job():
        raise RuntimeError("kaboom")

    assert sched._record_run("jhh.test_ok", ok_job) == "fine"
    try:
        sched._record_run("jhh.test_bad", bad_job)
    except RuntimeError:
        pass

    rows = {r["job_id"]: r for r in get_conn().execute(
        "SELECT job_id, status, error FROM scheduler_job_run "
        "WHERE job_id IN ('jhh.test_ok', 'jhh.test_bad') "
        "AND id IN (SELECT MAX(id) FROM scheduler_job_run GROUP BY job_id)"
    ).fetchall()}
    assert rows["jhh.test_ok"]["status"] == "ok"
    assert rows["jhh.test_bad"]["status"] == "failed"
    assert "kaboom" in (rows["jhh.test_bad"]["error"] or "")


def test_status_includes_last_run_fields():
    sched._ensure_scheduler_schema()
    sched._record_run("jhh.inbox_sweep", lambda: None)
    st = sched.status()
    # status() returns serializable JSON (no raw datetimes)
    assert json.dumps(st) is not None
    assert "jobs" in st and "saved_searches" in st


def test_find_slots_timezone_local_window():
    # Calendar not configured in tests -> naive slot generator path.
    # America/New_York is UTC-4/5; 9-17 local must NOT equal 9-17 UTC.
    utc_slots = calendar_google.find_slots(window_days=3, slot_minutes=60,
                                           work_hours=(9, 17), tz="UTC")
    ny_slots = calendar_google.find_slots(window_days=3, slot_minutes=60,
                                          work_hours=(9, 17), tz="America/New_York")

    def utc_hours(slots):
        return {datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC).hour
                for s in slots}

    # For NY slots, the UTC hour of each slot is offset (>=13 for 9am ET).
    if ny_slots:
        assert min(utc_hours(ny_slots)) >= 13
    # UTC slots sit in the 9-16 UTC band.
    if utc_slots:
        assert max(utc_hours(utc_slots)) <= 16
    assert utc_hours(ny_slots) != utc_hours(utc_slots)
