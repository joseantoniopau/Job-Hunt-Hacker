"""Tests for onboarding demo mode (G6).

POST /api/vault/demo-seed   — seed fictional vault (409 unless empty)
DELETE /api/vault/demo-seed — wipe exactly the demo rows
GET /api/vault/demo-status  — {active: bool}

Tests in this file are stateful in order (single temp DB per session) but
each test re-establishes its own preconditions, so they also survive
partial reruns.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from backend.app.db import get_conn, tx
from backend.app.main import app
from backend.app.services import career_vault, demo_seed

client = TestClient(app)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _count(sql: str, *params) -> int:
    row = get_conn().execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def _clear_profile_name() -> None:
    with tx() as c:
        c.execute("UPDATE user_profile SET name = NULL, email = NULL WHERE id = 1")


def _wipe_all_evidence() -> None:
    for src in career_vault.list_sources():
        career_vault.delete_source(int(src["id"]))


def _ensure_empty_vault() -> None:
    """Drive the DB to the 'effectively empty' state demo seeding requires."""
    demo_seed.delete_demo()
    _wipe_all_evidence()
    _clear_profile_name()


def _ensure_seeded() -> None:
    if not demo_seed.demo_status()["active"]:
        _ensure_empty_vault()
        r = client.post("/api/vault/demo-seed", json={"confirm": True})
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# status + confirm guard
# ---------------------------------------------------------------------------

def test_demo_status_initially_inactive():
    _ensure_empty_vault()
    r = client.get("/api/vault/demo-status")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["active"] is False
    assert body["data"]["active"] is False
    assert body["data"]["sources"] == 0
    assert body["data"]["jobs"] == 0


def test_seed_requires_confirm_flag():
    _ensure_empty_vault()
    assert client.post("/api/vault/demo-seed", json={}).status_code == 400
    assert client.post("/api/vault/demo-seed",
                       json={"confirm": False}).status_code == 400
    # Nothing got seeded by the rejected calls.
    assert demo_seed.demo_status()["active"] is False


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------

def test_seed_populates_vault_and_scores_jobs():
    _ensure_empty_vault()
    r = client.post("/api/vault/demo-seed", json={"confirm": True})
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    # --- profile ---
    prof = dict(get_conn().execute(
        "SELECT * FROM user_profile WHERE id = 1").fetchone())
    assert prof["name"] == "Alex Rivera"
    assert prof["email"] == "demo@example.invalid"
    assert "Senior Product Manager" in (prof["target_titles"] or "")
    assert "name" in data["profile_fields_set"]
    assert "email" in data["profile_fields_set"]

    # --- evidence sources: exactly 2, both tagged demo ---
    assert len(data["source_ids"]) == 2
    assert _count("SELECT COUNT(*) FROM evidence_source "
                  "WHERE source_type = 'demo'") == 2
    titles = [r[0] for r in get_conn().execute(
        "SELECT title FROM evidence_source WHERE source_type = 'demo'")]
    assert any("Resume" in t for t in titles)
    assert any("LinkedIn" in t for t in titles)

    # --- claims: ~15 (we ship 20), all with verbatim provenance ---
    claims = get_conn().execute(
        "SELECT c.claim_text, c.claim_type, s.raw_text "
        "FROM career_claim c JOIN evidence_source s ON s.id = c.source_id "
        "WHERE s.source_type = 'demo'").fetchall()
    assert data["claims_inserted"] == len(claims)
    assert 14 <= len(claims) <= 25
    for claim_text, _ctype, raw_text in claims:
        assert claim_text in raw_text, (
            f"claim lacks verbatim provenance: {claim_text!r}")
    claim_types = {c[1] for c in claims}
    assert {"role", "accomplishment", "skill"} <= claim_types

    # --- jobs: 6 tagged demo, with descriptions ---
    assert len(data["job_ids"]) == 6
    jobs = get_conn().execute(
        "SELECT id, description, source FROM job_posting "
        "WHERE source = 'demo'").fetchall()
    assert len(jobs) == 6
    assert all((j[1] or "").strip() for j in jobs)

    # --- scoring: every demo job has a job_match row with a real score ---
    assert data["jobs_scored"] == 6
    assert data["score_errors"] == []
    for jid in data["job_ids"]:
        m = get_conn().execute(
            "SELECT overall_score FROM job_match WHERE job_id = ?",
            (jid,)).fetchone()
        assert m is not None, f"demo job {jid} was not scored"
        assert 0.0 <= float(m[0]) <= 1.0

    # --- applications: 2, in different pipeline stages ---
    assert len(data["application_ids"]) == 2
    statuses = {r[0] for r in get_conn().execute(
        "SELECT status FROM application WHERE job_id IN "
        "(SELECT id FROM job_posting WHERE source = 'demo')")}
    assert statuses == {"applied", "interview"}

    # --- status flips active, op is audited ---
    body = client.get("/api/vault/demo-status").json()
    assert body["active"] is True
    assert body["data"]["claims"] == len(claims)
    assert _count("SELECT COUNT(*) FROM audit_log WHERE action = 'demo_seed'") >= 1


def test_seed_conflicts_when_demo_already_present():
    _ensure_seeded()
    r = client.post("/api/vault/demo-seed", json={"confirm": True})
    assert r.status_code == 409
    # Still exactly one demo set — no duplicates.
    assert _count("SELECT COUNT(*) FROM evidence_source "
                  "WHERE source_type = 'demo'") == 2
    assert _count("SELECT COUNT(*) FROM job_posting WHERE source = 'demo'") == 6


def test_seed_conflicts_when_profile_name_set():
    _ensure_empty_vault()
    with tx() as c:
        c.execute("UPDATE user_profile SET name = 'Real Human' WHERE id = 1")
    try:
        r = client.post("/api/vault/demo-seed", json={"confirm": True})
        assert r.status_code == 409
        assert demo_seed.demo_status()["active"] is False
    finally:
        _clear_profile_name()


def test_seed_conflicts_when_evidence_exists():
    _ensure_empty_vault()
    sid = career_vault.add_source(
        "text", title="My real notes",
        raw_text="I shipped a real thing once and have evidence of it.")
    try:
        r = client.post("/api/vault/demo-seed", json={"confirm": True})
        assert r.status_code == 409
    finally:
        career_vault.delete_source(int(sid))


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def test_delete_removes_demo_rows_and_nothing_else():
    _ensure_seeded()

    # Plant NON-demo rows that must survive the wipe.
    real_sid = career_vault.add_source(
        "text", title="Keep me — user evidence",
        raw_text="Built a genuinely real internal tool used by 5 teammates.")
    with tx() as c:
        cur = c.execute(
            "INSERT INTO job_posting (source, title, company, description, "
            "discovered_at, hash, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("greenhouse", "Real Job", "RealCo", "A non-demo job posting.",
             time.time(), f"realhash-{time.time()}", "new"))
        real_jid = int(cur.lastrowid)
        c.execute("INSERT INTO application (job_id, status) VALUES (?, 'saved')",
                  (real_jid,))

    demo_source_ids = [int(r[0]) for r in get_conn().execute(
        "SELECT id FROM evidence_source WHERE source_type = 'demo'")]
    demo_claim_ids = [int(r[0]) for r in get_conn().execute(
        "SELECT id FROM career_claim WHERE source_id IN "
        "(SELECT id FROM evidence_source WHERE source_type = 'demo')")]
    demo_job_ids = [int(r[0]) for r in get_conn().execute(
        "SELECT id FROM job_posting WHERE source = 'demo'")]
    assert demo_source_ids and demo_claim_ids and demo_job_ids

    r = client.delete("/api/vault/demo-seed")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["sources_deleted"] == 2
    assert data["claims_deleted"] == len(demo_claim_ids)
    assert data["jobs_deleted"] == 6
    assert data["applications_deleted"] == 2
    assert "name" in data["profile_fields_reset"]

    conn = get_conn()
    # Demo rows fully gone, including FK-cascaded children + embeddings.
    assert _count("SELECT COUNT(*) FROM evidence_source "
                  "WHERE source_type = 'demo'") == 0
    qmarks = ",".join("?" * len(demo_claim_ids))
    assert _count(f"SELECT COUNT(*) FROM career_claim WHERE id IN ({qmarks})",
                  *demo_claim_ids) == 0
    jmarks = ",".join("?" * len(demo_job_ids))
    assert _count("SELECT COUNT(*) FROM job_posting WHERE source = 'demo'") == 0
    assert _count(f"SELECT COUNT(*) FROM job_match WHERE job_id IN ({jmarks})",
                  *demo_job_ids) == 0
    assert _count(f"SELECT COUNT(*) FROM application WHERE job_id IN ({jmarks})",
                  *demo_job_ids) == 0
    for sid in demo_source_ids:
        assert _count("SELECT COUNT(*) FROM embedding "
                      "WHERE owner_type = 'evidence' AND owner_id = ?", sid) == 0
    for cid in demo_claim_ids:
        assert _count("SELECT COUNT(*) FROM embedding "
                      "WHERE owner_type = 'claim' AND owner_id = ?", cid) == 0

    # Profile fields we set are reset (name back to empty).
    prof = dict(conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone())
    assert not (prof["name"] or "").strip()
    assert not (prof["email"] or "").strip()

    # Non-demo rows untouched.
    assert _count("SELECT COUNT(*) FROM evidence_source WHERE id = ?",
                  int(real_sid)) == 1
    assert _count("SELECT COUNT(*) FROM job_posting WHERE id = ?", real_jid) == 1
    assert _count("SELECT COUNT(*) FROM application WHERE job_id = ?",
                  real_jid) == 1

    # Status reflects the wipe; op is audited.
    assert client.get("/api/vault/demo-status").json()["active"] is False
    assert _count("SELECT COUNT(*) FROM audit_log "
                  "WHERE action = 'demo_delete'") >= 1

    # Cleanup the planted real rows so later tests see an empty-ish DB.
    career_vault.delete_source(int(real_sid))
    with tx() as c:
        c.execute("DELETE FROM job_posting WHERE id = ?", (real_jid,))


def test_delete_preserves_user_edited_profile_fields():
    """If the user renamed the demo profile, delete must NOT clobber it."""
    _ensure_seeded()
    with tx() as c:
        c.execute("UPDATE user_profile SET name = 'Maria Real' WHERE id = 1")
    r = client.delete("/api/vault/demo-seed")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "name" not in data["profile_fields_reset"]
    assert "email" in data["profile_fields_reset"]  # still the demo value
    prof = dict(get_conn().execute(
        "SELECT name FROM user_profile WHERE id = 1").fetchone())
    assert prof["name"] == "Maria Real"
    _clear_profile_name()


def test_delete_is_idempotent():
    _ensure_empty_vault()
    r = client.delete("/api/vault/demo-seed")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["sources_deleted"] == 0
    assert data["claims_deleted"] == 0
    assert data["jobs_deleted"] == 0
    assert data["applications_deleted"] == 0


def test_reseed_after_delete_works():
    _ensure_empty_vault()
    r1 = client.post("/api/vault/demo-seed", json={"confirm": True})
    assert r1.status_code == 200, r1.text
    assert client.get("/api/vault/demo-status").json()["active"] is True
    assert client.delete("/api/vault/demo-seed").status_code == 200
    r2 = client.post("/api/vault/demo-seed", json={"confirm": True})
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["claims_inserted"] >= 14
    # leave the DB clean for any test that runs after this file
    client.delete("/api/vault/demo-seed")
