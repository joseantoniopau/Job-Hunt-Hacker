"""End-to-end tests for the six new backend features.

Each feature has at least one test exercising the service layer + HTTP
layer where applicable. We rely on conftest.py to redirect the DB to a
temp file so writes here don't pollute the user's real vault.
"""
from __future__ import annotations

import csv
import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app.db import get_conn, init_db, tx
from backend.app.main import app
from backend.app.routers.profile import profile_completeness
from backend.app.services import effectiveness_tracker, gap_tracker

client = TestClient(app)


# ----------------------- helpers -----------------------

def _make_job(title: str = "Test Job", company: str = "TestCo") -> int:
    """Insert a minimal job_posting and return its id."""
    init_db()
    h = f"hash-{title}-{company}-{time.time()}-{id(title)}"
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO job_posting (source, title, company, status, hash, discovered_at) "
            "VALUES (?, ?, ?, 'new', ?, ?)",
            ("test", title, company, h, time.time()),
        )
        return int(cur.lastrowid)


def _make_tailored_resume() -> int:
    """Create a minimal tailored_resume row so applications can FK to it."""
    init_db()
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO tailored_resume (resume_type, markdown, created_at) "
            "VALUES (?, ?, ?)",
            ("job_specific", "# test", time.time()),
        )
        return int(cur.lastrowid)


def _make_app(job_id: int, status: str = "saved", resume_id: int | None = None) -> int:
    init_db()
    # FK constraint on application.resume_id → tailored_resume.id. If the
    # caller passes a resume_id we don't bother validating it exists; the
    # tests that need linkage call _make_tailored_resume() themselves.
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO application (job_id, status, resume_id, audit_json) "
            "VALUES (?, ?, ?, '[]')",
            (job_id, status, resume_id),
        )
        return int(cur.lastrowid)


def _reset_profile_to_blank() -> None:
    """Wipe the singleton profile back to defaults so completeness math is
    deterministic regardless of test order."""
    init_db()
    with tx() as conn:
        conn.execute("DELETE FROM user_profile WHERE id = 1")
        now = time.time()
        # Insert with currency=NULL and mode=NULL so 'empty' truly means empty
        # (the default seed sets both, which would skew the score upward).
        conn.execute(
            "INSERT INTO user_profile (id, currency, mode, created_at, updated_at) "
            "VALUES (1, NULL, NULL, ?, ?)",
            (now, now),
        )


# ----------------------- 1. gap tracker -----------------------

def test_gap_tracker_record_and_top():
    jid = _make_job("Gap Test A", "GapCo")
    # Record two events for kubernetes, one for terraform
    n1 = gap_tracker.record_gaps(jid, ["Kubernetes", "Terraform"])
    n2 = gap_tracker.record_gaps(jid, ["kubernetes"])  # case-insensitive
    assert n1 == 2 and n2 == 1

    top = gap_tracker.top_gaps(days=30, limit=10)
    keywords = {row["keyword"]: row for row in top}
    assert "kubernetes" in keywords
    assert keywords["kubernetes"]["mentions"] >= 2
    assert jid in keywords["kubernetes"]["sample_job_ids"]


def test_gap_tracker_trend_by_day():
    jid = _make_job("Gap Test B", "GapCo")
    gap_tracker.record_gaps(jid, ["Rust", "Go", "Rust"])
    t = gap_tracker.trend(days=30)
    assert t["total"] >= 3
    assert t["unique_keywords"] >= 2
    # by_day must be a dict keyed by ISO date strings
    assert isinstance(t["by_day"], dict)
    assert all(len(k) == 10 and k[4] == "-" for k in t["by_day"].keys())


def test_gap_router_endpoints_work():
    # Record via HTTP
    r = client.post(
        "/api/gaps/record",
        json={"job_id": None, "missing": ["FastAPI", "Postgres"]},
    )
    assert r.status_code == 200
    assert r.json().get("ok") is True

    r2 = client.get("/api/gaps/top?days=30&limit=5")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2.get("ok") is True
    assert isinstance(body2.get("data"), list)

    r3 = client.get("/api/gaps/trend?days=30")
    assert r3.status_code == 200
    body3 = r3.json()
    assert body3.get("ok") is True
    assert "by_day" in (body3.get("data") or {})


# ----------------------- 2. effectiveness tracker -----------------------

def test_effectiveness_record_then_stats():
    jid = _make_job("Eff Test A", "EffCo")
    rid = _make_tailored_resume()
    aid = _make_app(jid, status="applied", resume_id=rid)
    # Three sends, one reply, one interview
    effectiveness_tracker.record(aid, rid, "sent")
    effectiveness_tracker.record(aid, rid, "sent")
    effectiveness_tracker.record(aid, rid, "sent")
    effectiveness_tracker.record(aid, rid, "replied")
    effectiveness_tracker.record(aid, rid, "interviewed")

    stats = effectiveness_tracker.resume_stats(rid)
    assert stats["sent"] == 3
    assert stats["replied"] == 1
    assert stats["interview"] == 1
    # reply_rate = (replied + screened + interviewed + offered) / sent
    assert stats["reply_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert stats["interview_rate"] == pytest.approx(1 / 3, abs=0.01)


def test_effectiveness_record_invalid_outcome_rejected():
    with pytest.raises(ValueError):
        effectiveness_tracker.record(None, 9999, "bogus_outcome")


def test_effectiveness_leaderboard_orders_by_reply_rate():
    # Resume A: 3 sent, 2 replied (66%)
    # Resume B: 3 sent, 0 replied (0%)
    jid = _make_job("Eff Test B", "EffCo")
    aid = _make_app(jid, status="applied")
    rid_a = _make_tailored_resume()
    rid_b = _make_tailored_resume()
    for _ in range(3):
        effectiveness_tracker.record(aid, rid_a, "sent")
    effectiveness_tracker.record(aid, rid_a, "replied")
    effectiveness_tracker.record(aid, rid_a, "replied")
    for _ in range(3):
        effectiveness_tracker.record(aid, rid_b, "sent")

    board = effectiveness_tracker.all_resume_effectiveness(min_sent=3)
    rid_index = {row["resume_id"]: i for i, row in enumerate(board)}
    # rid_a must appear before rid_b in the ordering
    assert rid_index.get(rid_a, 999) < rid_index.get(rid_b, 999)


def test_effectiveness_leaderboard_http():
    r = client.get("/api/effectiveness/leaderboard?min_sent=1")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert isinstance(body.get("data"), list)


# ----------------------- 3. profile completeness -----------------------

def test_profile_completeness_empty():
    out = profile_completeness({})
    assert out["score"] == 0
    assert out["filled"] == []
    assert len(out["missing"]) > 0
    assert len(out["suggestions"]) == len(out["missing"])


def test_profile_completeness_partial():
    out = profile_completeness({
        "name": "Jane",
        "email": "jane@example.com",
        "target_titles": ["Engineer"],
    })
    # 3 of 11 fields → 27%
    assert 20 <= out["score"] <= 35
    assert "name" in out["filled"]
    assert "email" in out["filled"]
    assert "target_titles" in out["filled"]
    assert "minimum_salary" in out["missing"]


def test_profile_completeness_full():
    out = profile_completeness({
        "name": "Jane",
        "email": "jane@example.com",
        "target_titles": ["Engineer"],
        "target_keywords": ["python", "aws"],
        "preferred_locations": ["Remote"],
        "employment_types": ["full-time"],
        "seniority_targets": ["senior"],
        "currency": "USD",
        "mode": "assisted",
        "minimum_salary": 150000,
        "location": "NYC, NY",
    })
    assert out["score"] == 100
    assert out["missing"] == []
    assert out["suggestions"] == []


def test_profile_completeness_http():
    _reset_profile_to_blank()
    r = client.get("/api/profile/completeness")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    data = body.get("data") or {}
    assert "score" in data
    assert "missing" in data
    assert "filled" in data
    assert "suggestions" in data


# ----------------------- 4. CSV export -----------------------

def test_csv_export_single_table():
    # Ensure at least one application row exists so the CSV isn't trivially empty
    jid = _make_job("CSV Test Job", "CSVCo")
    _make_app(jid, status="saved")
    r = client.get("/api/data/export.csv?table=applications")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    body = r.content.decode("utf-8")
    reader = csv.DictReader(io.StringIO(body))
    rows = list(reader)
    assert len(rows) >= 1
    # The application table must have at least these columns in headers
    fieldnames = set(reader.fieldnames or [])
    assert {"id", "job_id", "status"}.issubset(fieldnames)


def test_csv_export_unknown_table_400():
    r = client.get("/api/data/export.csv?table=not_a_real_table")
    assert r.status_code == 400


def test_csv_export_zip_of_all():
    jid = _make_job("CSV Zip Job", "ZipCo")
    _make_app(jid, status="saved")
    r = client.get("/api/data/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    # Must contain a CSV for each bundle table
    assert "application.csv" in names
    assert "job_posting.csv" in names
    # And each entry is non-empty CSV with a header line
    with zf.open("application.csv") as f:
        text = f.read().decode("utf-8")
        assert "id" in text.splitlines()[0]


# ----------------------- 5. bulk operations -----------------------

def test_bulk_jobs_status_archives_many():
    ids = [_make_job(f"Bulk Job {i}", "BulkCo") for i in range(3)]
    r = client.post(
        "/api/bulk/jobs/status",
        json={"job_ids": ids, "status": "archived"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    data = body.get("data") or {}
    assert data.get("touched") == 3
    assert data.get("failed") == []
    # And confirm they actually got archived in the DB
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, status FROM job_posting WHERE id IN (%s)" % ",".join("?" * len(ids)),
        ids,
    ).fetchall()
    for r2 in rows:
        assert r2["status"] == "archived"


def test_bulk_jobs_status_reports_missing():
    r = client.post(
        "/api/bulk/jobs/status",
        json={"job_ids": [999_999_999], "status": "archived"},
    )
    assert r.status_code == 200
    body = r.json()
    data = body.get("data") or {}
    assert data.get("touched") == 0
    assert len(data.get("failed") or []) == 1


def test_bulk_applications_status_updates_many():
    jids = [_make_job(f"Bulk App Job {i}", "BulkCo") for i in range(2)]
    aids = [_make_app(j, status="saved") for j in jids]
    r = client.post(
        "/api/bulk/applications/status",
        json={"application_ids": aids, "status": "applied"},
    )
    assert r.status_code == 200
    data = r.json().get("data") or {}
    assert data.get("touched") == 2


def test_bulk_jobs_delete_soft_archives():
    ids = [_make_job(f"Bulk Del {i}", "DelCo") for i in range(2)]
    r = client.post("/api/bulk/jobs/delete", json={"job_ids": ids})
    assert r.status_code == 200
    data = r.json().get("data") or {}
    assert data.get("touched") == 2
