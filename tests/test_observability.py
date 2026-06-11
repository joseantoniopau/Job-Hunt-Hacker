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


# ---- observed LLM runs: negotiation script + followup emails -------------

def _seed_application(status: str = "offer") -> int:
    """Insert a job_posting + application pair and return the application id."""
    now = time.time()
    with tx() as c:
        cur = c.execute(
            "INSERT INTO job_posting (source, title, company, location, description, hash, discovered_at) "
            "VALUES ('test', 'Senior Python Engineer', 'ObsCo', 'Remote', "
            "'Build reliable Python systems and data pipelines.', ?, ?)",
            (f"obs-test-{uuid.uuid4()}", now),
        )
        job_id = int(cur.lastrowid)
        cur = c.execute(
            "INSERT INTO application (job_id, status) VALUES (?, ?)",
            (job_id, status),
        )
        app_id = int(cur.lastrowid)
    return app_id


def _llm_run_row(run_id: int) -> dict:
    row = get_conn().execute(
        "SELECT * FROM llm_run WHERE id = ?", (int(run_id),)
    ).fetchone()
    assert row is not None, f"llm_run id={run_id} not recorded"
    return dict(row)


def test_negotiation_script_records_observed_run() -> None:
    """negotiation.generate() must route its LLM call through
    observed_complete (stage 'negotiation_script') and link the run id
    into the returned payload — even on the template provider, where the
    deterministic fallback supplies the script."""
    from backend.app.tailoring import negotiation

    app_id = _seed_application()
    out = negotiation.generate(app_id, offer_base=100_000, offer_total=120_000,
                               currency="USD")

    # Response shape unchanged.
    script = out["script"]
    for key in ("opening", "market_anchor", "counter_ask",
                "fallback_position", "walkaway", "talking_points"):
        assert key in script, f"missing script key {key}"
    assert out["application_id"] == app_id
    # Template provider echoes the input JSON (no 'opening' key) so the
    # deterministic fallback must have kicked in.
    assert out["provenance"]["provider"] == "template"
    assert script["opening"]

    # The run is recorded and linked into the payload.
    rid = out.get("llm_run_id")
    assert rid and rid > 0
    row = _llm_run_row(rid)
    assert row["stage"] == "negotiation_script"
    assert row["provider"] == "template"
    assert row["status"] == "ok"
    assert row["target_type"] == "application"
    assert row["target_id"] == app_id


def test_followup_draft_records_observed_run() -> None:
    """followup_emails.draft() must record an llm_run (stage
    'followup_email') and link the run id into the returned payload."""
    from backend.app.tailoring import followup_emails

    app_id = _seed_application(status="applied")
    out = followup_emails.draft(app_id, "applied")

    # Response shape unchanged: template fallback still produces the email.
    assert out["stage"] == "applied"
    assert out["subject"] and out["body"]
    assert out["provenance"]["provider"] == "template"
    assert "honesty_report" in out

    rid = out.get("llm_run_id")
    assert rid and rid > 0
    row = _llm_run_row(rid)
    assert row["stage"] == "followup_email"
    assert row["provider"] == "template"
    assert row["status"] == "ok"
    assert row["target_type"] == "application"
    assert row["target_id"] == app_id


# ---- interview prep evidence budget ---------------------------------------

def _fake_claim(i: int, text_repeats: int) -> dict:
    return {
        "id": 10_000 + i,
        "claim_type": "experience",
        "claim_text": f"Python systems work {i}: "
                      + ("shipped reliable data pipelines " * text_repeats),
        "employer": "ObsCo",
        "skill": "Python",
        "tool": None,
        "confidence": round(0.99 - i * 0.01, 4),
        "user_verified": 1,
        "source_id": None,
        "allowed_for_resume": 1,
    }


def test_evidence_budget_truncates_lowest_confidence_stable_order() -> None:
    from backend.app.routers import interview_prep as iv

    rows = [_fake_claim(i, text_repeats=30) for i in range(20)]
    pack = [{"id": r["id"], "claim_type": r["claim_type"],
             "claim_text": r["claim_text"], "employer": r["employer"],
             "skill": r["skill"], "tool": r["tool"],
             "user_verified": True, "provenance": {}} for r in rows]
    conf = {r["id"]: r["confidence"] for r in rows}

    assert len(iv._serialize_evidence(pack)) > 11_000  # the test is meaningful

    kept, meta = iv._apply_evidence_budget(pack, conf, budget=11_000)
    assert meta["evidence_truncated"] is True
    assert meta["claims_total"] == 20
    assert meta["claims_kept"] == len(kept)
    assert 0 < len(kept) < 20
    assert len(iv._serialize_evidence(kept)) <= 11_000
    assert meta["serialized_chars"] <= 11_000

    # Highest-confidence claims (lowest i) survive, in stable original order.
    kept_ids = [c["id"] for c in kept]
    assert kept_ids == [10_000 + i for i in range(len(kept_ids))]


def test_evidence_budget_no_truncation_under_budget() -> None:
    from backend.app.routers import interview_prep as iv

    rows = [_fake_claim(i, text_repeats=1) for i in range(3)]
    pack = [{"id": r["id"], "claim_text": r["claim_text"]} for r in rows]
    kept, meta = iv._apply_evidence_budget(
        pack, {r["id"]: r["confidence"] for r in rows}, budget=11_000)
    assert kept == pack
    assert meta == {
        "evidence_truncated": False,
        "claims_total": 3,
        "claims_kept": 3,
        "budget_chars": 11_000,
        "serialized_chars": len(iv._serialize_evidence(pack)),
    }


def test_interview_prep_packet_flags_evidence_truncation(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """An oversized evidence pack must be truncated at the 11k-char budget
    and the packet payload must carry evidence_truncated + counts."""
    from backend.app.services import career_vault

    app_id = _seed_application(status="interview")
    long_rows = [_fake_claim(i, text_repeats=30) for i in range(20)]
    monkeypatch.setattr(career_vault, "retrieve_for_job",
                        lambda text, top=20: long_rows[:top])

    resp = client.post(f"/api/interview/prep/{app_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["evidence_truncated"] is True
    assert data["evidence_claims_total"] == 20
    assert 0 < data["evidence_claims_kept"] < 20
    meta = data["evidence_meta_json"]
    assert meta["evidence_truncated"] is True
    assert meta["claims_kept"] == data["evidence_claims_kept"]
    assert meta["serialized_chars"] <= meta["budget_chars"] == 11_000

    # GET surfaces the same flags (persisted, not just on create).
    resp2 = client.get(f"/api/interview/prep/{app_id}")
    assert resp2.status_code == 200
    data2 = resp2.json()["data"]
    assert data2["evidence_truncated"] is True
    assert data2["evidence_claims_kept"] == data["evidence_claims_kept"]


def test_interview_prep_packet_not_truncated_when_small(
        client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.services import career_vault

    app_id = _seed_application(status="interview")
    small_rows = [_fake_claim(i, text_repeats=1) for i in range(3)]
    monkeypatch.setattr(career_vault, "retrieve_for_job",
                        lambda text, top=20: small_rows[:top])

    resp = client.post(f"/api/interview/prep/{app_id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["evidence_truncated"] is False
    assert data["evidence_claims_total"] == 3
    assert data["evidence_claims_kept"] == 3
