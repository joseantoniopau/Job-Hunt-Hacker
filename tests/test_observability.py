"""Observability tests: request IDs, /metrics endpoint, audit retention + listing."""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.db import get_conn, tx
from backend.app.integrations import scheduler as sched


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


# ---- request ID middleware ----------------------------------------------

def test_health_emits_request_id_header(client: TestClient) -> None:
    """Every response should carry an `X-Request-ID` header that's a UUID."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    rid = resp.headers.get("X-Request-ID") or resp.headers.get("x-request-id")
    assert rid, "missing X-Request-ID header"
    # If we didn't supply one, the middleware should mint a fresh UUID4.
    parsed = uuid.UUID(rid)
    assert parsed.version == 4


def test_request_id_is_propagated_when_supplied(client: TestClient) -> None:
    """Supplying X-Request-ID should round-trip it back unchanged."""
    supplied = "trace-from-load-balancer-42"
    resp = client.get("/api/health", headers={"X-Request-ID": supplied})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == supplied


# ---- /metrics endpoint --------------------------------------------------

def test_metrics_endpoint_returns_prometheus_text(client: TestClient) -> None:
    # Make a request first so the http_requests_total counter has data.
    client.get("/api/health")

    resp = client.get("/metrics")
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    assert "text/plain" in ctype
    assert "version=0.0.4" in ctype, f"unexpected content-type: {ctype}"

    body = resp.text
    # At minimum the http_requests_total counter should be present after
    # we hit /api/health above.
    assert "jhh_http_requests_total" in body


# ---- audit retention ----------------------------------------------------

def test_audit_retention_deletes_old_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Insert old + new rows, run retention, verify only fresh rows survive."""
    monkeypatch.setenv("JHH_AUDIT_RETENTION_DAYS", "7")
    conn = get_conn()

    # Clean slate so the test is deterministic.
    with tx() as c:
        c.execute("DELETE FROM audit_log")

    now = time.time()
    old_ts = now - 30 * 86400  # 30 days old → must be deleted
    fresh_ts = now - 1 * 3600  # 1 hour old → must survive

    with tx() as c:
        # 200 rows total: 150 old, 50 fresh.
        for i in range(150):
            c.execute(
                "INSERT INTO audit_log (ts, actor, action, target_type, target_id, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (old_ts, "test", f"old_event_{i}", "system", None, "{}"),
            )
        for i in range(50):
            c.execute(
                "INSERT INTO audit_log (ts, actor, action, target_type, target_id, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (fresh_ts, "test", f"fresh_event_{i}", "system", None, "{}"),
            )

    pre = int(conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])
    assert pre >= 200

    result = sched.run_audit_retention()
    assert result.get("ok") is True
    assert result["days"] == 7
    # At least the 150 old rows we just inserted must have been swept.
    assert result["deleted"] >= 150

    remaining_old = int(
        conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'old_event_%'").fetchone()[0]
    )
    remaining_fresh = int(
        conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'fresh_event_%'").fetchone()[0]
    )
    assert remaining_old == 0
    assert remaining_fresh == 50


# ---- /api/audit listing -------------------------------------------------

def test_api_audit_returns_reverse_chronological(client: TestClient) -> None:
    """Insert known rows with monotonic timestamps; assert newest first."""
    with tx() as c:
        c.execute("DELETE FROM audit_log")
        base = time.time()
        for i in range(5):
            c.execute(
                "INSERT INTO audit_log (ts, actor, action, target_type, target_id, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (base + i, "test", f"chrono_event_{i}", "system", None, "{}"),
            )

    resp = client.get("/api/audit?limit=10")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    rows = payload["data"]
    # Filter to just the rows we inserted (the /api/audit call itself may
    # have created a server_start row earlier — though we deleted them above,
    # other tests in this file may have triggered events between requests).
    ours = [r for r in rows if str(r["action"]).startswith("chrono_event_")]
    assert len(ours) == 5
    # Reverse chrono: timestamps must be non-increasing.
    timestamps = [float(r["ts"]) for r in ours]
    assert timestamps == sorted(timestamps, reverse=True)
    assert ours[0]["action"] == "chrono_event_4"
    assert ours[-1]["action"] == "chrono_event_0"


def test_api_audit_stats_envelope(client: TestClient) -> None:
    resp = client.get("/api/audit/stats")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    data = payload["data"]
    assert "total" in data
    assert "by_action" in data
    assert "by_day_last_30" in data
    assert isinstance(data["by_action"], dict)
    assert isinstance(data["by_day_last_30"], dict)
