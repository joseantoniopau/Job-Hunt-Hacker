"""Resilience tests: adapter cache, retry passthrough, snapshot lifecycle.

Each test isolates state through:
  - the conftest-provided JHH_DB_PATH temp DB (kept clean by `_clear_cache`)
  - pytest's monkeypatch for tenacity removal
  - the snapshots/ dir under settings.data_dir, cleaned around the snapshot tests
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import settings
from backend.app.db import get_conn, tx
from backend.app.main import app
from backend.app.services.job_sources import cache as adapter_cache
from backend.app.services.job_sources.base import JobRecord, JobSearchQuery


client = TestClient(app)


# ----- helpers ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_cache():
    """Wipe the adapter_cache table between tests so cache keys don't bleed."""
    try:
        adapter_cache.clear_all()
    except Exception:
        pass
    yield
    try:
        adapter_cache.clear_all()
    except Exception:
        pass


def _sample_records(n: int = 2) -> list[JobRecord]:
    out = []
    for i in range(n):
        out.append(JobRecord(
            source="remotive",
            title=f"Engineer {i}",
            company=f"Acme {i}",
            location="Remote",
            description="short",
            apply_url=f"https://example.com/{i}",
            external_id=str(i),
        ))
    return out


def _snapshots_dir() -> Path:
    p = settings.data_dir / "snapshots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _clear_snapshots_dir() -> None:
    d = _snapshots_dir()
    for f in d.iterdir():
        if f.is_file():
            try:
                f.unlink()
            except Exception:
                pass


# ----- cache tests -----------------------------------------------------------

def test_cache_get_returns_none_on_miss():
    q = JobSearchQuery(query="never-cached-needle-xyz", results_per_site=3)
    assert adapter_cache.get("remotive", q) is None


def test_cache_get_returns_cached_on_hit():
    q = JobSearchQuery(query="python engineer", location="remote", results_per_site=5)
    recs = _sample_records(3)
    adapter_cache.set("remotive", q, recs, ttl=60)
    got = adapter_cache.get("remotive", q)
    assert got is not None
    assert len(got) == 3
    assert got[0].title == recs[0].title
    assert got[0].company == recs[0].company


def test_cache_expires_after_ttl():
    q = JobSearchQuery(query="rust engineer", results_per_site=2)
    recs = _sample_records(1)
    # ttl=1 second; sleep just past it
    adapter_cache.set("remotive", q, recs, ttl=1)
    assert adapter_cache.get("remotive", q) is not None  # still fresh
    time.sleep(1.2)
    assert adapter_cache.get("remotive", q) is None


# ----- retry passthrough -----------------------------------------------------

def test_retry_passes_through_when_tenacity_missing(monkeypatch):
    """When tenacity isn't importable, wrap_with_retry returns the same callable."""
    # Force the lazy import inside retry.py to fail by stubbing sys.modules
    monkeypatch.setitem(sys.modules, "tenacity", None)
    # Re-import the retry module to ensure it picks up the stubbed state
    from importlib import reload
    from backend.app.services.job_sources import retry as retry_mod
    reload(retry_mod)
    try:
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        wrapped = retry_mod.wrap_with_retry(fn)
        # When tenacity is missing, wrap_with_retry returns the same function object.
        assert wrapped is fn
        assert wrapped() == "ok"
        assert calls["n"] == 1
    finally:
        # Restore tenacity for the rest of the test session.
        monkeypatch.delitem(sys.modules, "tenacity", raising=False)
        reload(retry_mod)


# ----- snapshot tests --------------------------------------------------------

def test_pre_wipe_snapshot_created():
    """DELETE /api/data?i_understand=ENABLE writes a snapshot file."""
    _clear_snapshots_dir()
    r = client.delete("/api/data?i_understand=ENABLE")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    snap = (body.get("data") or {}).get("snapshot")
    assert snap is not None
    assert snap.get("filename")
    # File exists on disk
    files = [p for p in _snapshots_dir().iterdir() if p.name.startswith("jhh-pre-wipe-")]
    assert files, f"no snapshot file written to {_snapshots_dir()}"
    assert any(f.name == snap["filename"] for f in files)


def test_snapshot_list_endpoint():
    """GET /api/data/snapshots returns a list including the snapshot we just made."""
    # First ensure at least one snapshot exists.
    client.delete("/api/data?i_understand=ENABLE")
    r = client.get("/api/data/snapshots")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    data = body.get("data") or {}
    snapshots = data.get("snapshots")
    assert isinstance(snapshots, list)
    assert data.get("count") == len(snapshots)
    assert len(snapshots) >= 1
    item = snapshots[0]
    assert "filename" in item
    assert "size_bytes" in item
    assert "created_at" in item
    assert "counts" in item


def test_snapshot_restore_endpoint():
    """Restoring a snapshot brings rows back even after a wipe."""
    _clear_snapshots_dir()
    # 1. Seed a job row so we have something to lose.
    with tx() as conn:
        conn.execute(
            "INSERT INTO job_posting (source, title, company, location, hash, status, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, 'new', ?)",
            ("remotive", "Snapshot Test Engineer", "AcmeSnap", "Remote",
             f"snapshot-test-{time.time()}", time.time()),
        )
    seeded_count = get_conn().execute(
        "SELECT COUNT(*) FROM job_posting WHERE company = 'AcmeSnap'"
    ).fetchone()[0]
    assert seeded_count >= 1

    # 2. Wipe (which creates the snapshot AND removes the row).
    wipe_resp = client.delete("/api/data?i_understand=ENABLE")
    assert wipe_resp.status_code == 200
    snap_filename = ((wipe_resp.json().get("data") or {}).get("snapshot") or {}).get("filename")
    assert snap_filename, "wipe response missing snapshot.filename"

    after_wipe = get_conn().execute(
        "SELECT COUNT(*) FROM job_posting WHERE company = 'AcmeSnap'"
    ).fetchone()[0]
    assert after_wipe == 0, "wipe didn't actually delete the row"

    # 3. Restore from the snapshot.
    restore_resp = client.post(
        "/api/data/snapshots/restore",
        json={"filename": snap_filename},
    )
    assert restore_resp.status_code == 200, restore_resp.text
    body = restore_resp.json()
    assert body["ok"] is True
    assert (body.get("data") or {}).get("restored_from") == snap_filename

    # 4. Verify the row came back.
    restored = get_conn().execute(
        "SELECT COUNT(*) FROM job_posting WHERE company = 'AcmeSnap'"
    ).fetchone()[0]
    assert restored >= 1


def test_snapshot_path_traversal_rejected():
    """DELETE /api/data/snapshots/{filename} refuses obvious traversal attempts."""
    bad_names = [
        "..%2Fetc%2Fpasswd",     # URL-encoded ..
        "..-etc-passwd",          # contains '..'
    ]
    for name in bad_names:
        r = client.delete(f"/api/data/snapshots/{name}")
        # Either the URL routing rejects it (404) or our validator does (400).
        # ".." encoded should hit the handler -> 400; raw "/" would be eaten
        # by the router. We accept either rejection.
        assert r.status_code in (400, 404), f"{name}: expected reject, got {r.status_code} {r.text}"

    # The restore endpoint takes the filename in the JSON body, so we can
    # send the traversal string verbatim and it MUST be rejected with 400.
    r = client.post("/api/data/snapshots/restore", json={"filename": "../etc/passwd"})
    assert r.status_code == 400, r.text
    r = client.post("/api/data/snapshots/restore", json={"filename": "foo/bar.json"})
    assert r.status_code == 400, r.text
