"""Tests for the second wave of Job Hunt Hacker features.

Covers:
  * interview question library (load + role-aware selection)
  * saved-search daily digest (empty + populated cases)
  * Slack webhook (unconfigured + configured shape)
  * resume "rewrite this bullet" iteration flow (endpoint + guardrails)

Tests rely on ``tests/conftest.py`` redirecting the SQLite DB to a temp
file so seed inserts don't leak into the user's vault.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.app.db import get_conn, init_db, tx


# --------------------------------------------------------------------------
# Shared TestClient — built lazily so module-level imports don't crash if
# a router happens to be unavailable in a particular env.
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client() -> TestClient:
    init_db()
    from backend.app.main import app
    return TestClient(app)


# --------------------------------------------------------------------------
# 1. Interview library
# --------------------------------------------------------------------------

EXPECTED_FAMILIES = {
    "general", "engineering", "data", "design", "product", "marketing",
    "sales", "writing", "operations", "support", "security", "exec",
}


def test_interview_library_loads_with_all_families():
    from backend.app.tailoring import interview_library

    # Force a clean load so this test isn't sensitive to prior test order.
    qs = interview_library.reload_questions()
    assert isinstance(qs, dict)
    missing = EXPECTED_FAMILIES - set(qs.keys())
    assert not missing, f"missing families: {sorted(missing)}"
    # Every family must have at least 5 real questions (no placeholders).
    for fam, lst in qs.items():
        assert isinstance(lst, list), f"family {fam} is not a list"
        assert len(lst) >= 5, f"family {fam} has too few questions ({len(lst)})"
        for q in lst:
            assert isinstance(q, str) and q.strip(), f"bad question in {fam}: {q!r}"
            # cheap placeholder sniff
            qlc = q.lower()
            assert "todo" not in qlc and "placeholder" not in qlc, (
                f"placeholder leaked into {fam}: {q!r}"
            )


def test_interview_library_returns_role_specific_plus_general():
    from backend.app.tailoring import interview_library

    interview_library.reload_questions()
    out = interview_library.questions_for_role("Senior Backend Engineer", n=6)
    assert isinstance(out, list)
    assert 1 <= len(out) <= 6

    general = set(interview_library.load_questions()["general"])
    engineering = set(interview_library.load_questions()["engineering"])

    overlap_general = [q for q in out if q in general]
    overlap_eng = [q for q in out if q in engineering]
    # Spec: always blend in 2 from "general" + role-specific from family bucket
    assert len(overlap_general) >= 1, "expected at least 1 general question"
    assert len(overlap_eng) >= 1, "expected at least 1 engineering question"

    # Unclassifiable titles should still return *something* (fallback to general)
    unknown = interview_library.questions_for_role("Mystery Title XYZ", n=4)
    assert isinstance(unknown, list)
    assert unknown, "unknown title should fall back to general questions"


# --------------------------------------------------------------------------
# 2. Saved-search digest
# --------------------------------------------------------------------------

def _wipe_recent_jobs() -> None:
    """Push existing job_posting rows back in time so they're outside the
    24h digest window. We can't DROP rows because other tests in the
    session may have created them via cascading inserts.
    """
    far_past = time.time() - 365 * 86400
    with tx() as conn:
        conn.execute(
            "UPDATE job_posting SET discovered_at = ? WHERE discovered_at > ?",
            (far_past, far_past),
        )


def test_digest_assembles_when_no_jobs():
    init_db()
    _wipe_recent_jobs()

    from backend.app.integrations import digest

    out = digest.assemble_digest(since_hours=24)
    assert isinstance(out, dict)
    assert out.get("total_new_jobs") == 0
    assert isinstance(out.get("by_search"), dict)
    assert isinstance(out.get("top_10_overall"), list)
    assert out.get("top_10_overall") == []

    text = digest.render_digest_text(out)
    assert "0" in text
    assert "Nothing new" in text or "0" in text

    html_body = digest.render_digest_html(out)
    assert "<html" in html_body.lower()


def test_digest_assembles_with_real_jobs():
    init_db()
    _wipe_recent_jobs()

    tag = f"digest_{int(time.time() * 1000)}"

    # Seed a saved search whose query keywords match our seeded job titles.
    with tx() as conn:
        conn.execute(
            "INSERT INTO saved_search (label, query_json, frequency_hours, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                f"Backend Python {tag}",
                json.dumps({"query": "python backend engineer", "location": "Remote"}),
                24,
                1,
                time.time(),
            ),
        )

    now = time.time()
    job_ids: list[int] = []
    with tx() as conn:
        for i in range(3):
            cur = conn.execute(
                "INSERT INTO job_posting (source, title, company, location, description, "
                "discovered_at, hash, apply_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "test_src",
                    f"Senior Python Backend Engineer {tag}-{i}",
                    f"AcmeCo-{i}",
                    "Remote",
                    "Python + Postgres role, focus on backend services.",
                    now - i * 60,
                    f"hash_{tag}_{i}",
                    f"https://example.com/job/{tag}/{i}",
                ),
            )
            jid = int(cur.lastrowid)
            job_ids.append(jid)
            # Attach a score to two of them so top_10 ordering is observable.
            if i < 2:
                conn.execute(
                    "INSERT INTO job_match (job_id, overall_score, created_at) VALUES (?, ?, ?)",
                    (jid, 80.0 - i * 5, now),
                )

    from backend.app.integrations import digest

    out = digest.assemble_digest(since_hours=24)
    assert out.get("total_new_jobs") == 3

    # Saved-search bucket must contain our jobs
    by_search = out.get("by_search") or {}
    matched = any(
        any(item["job_id"] in job_ids for item in items)
        for items in by_search.values()
    )
    assert matched, f"no saved-search bucket caught our jobs: {by_search!r}"

    top = out.get("top_10_overall") or []
    assert top, "top_10_overall should contain at least the scored jobs"
    # Top entry should be the highest-scored one we seeded (80.0).
    assert top[0]["score"] >= 70.0

    text = digest.render_digest_text(out)
    assert "TOP 10 OVERALL" in text
    html_body = digest.render_digest_html(out)
    assert "Top 10" in html_body


# --------------------------------------------------------------------------
# 3. Slack webhook
# --------------------------------------------------------------------------

def test_slack_not_configured_returns_false(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    from backend.app.integrations import slack

    assert slack.is_configured() is False
    # post() never raises and returns False when unconfigured
    assert slack.post("hello") is False


def test_slack_configured_format_check(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")

    from backend.app.integrations import slack

    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 200
        text = "ok"

    def _fake_post(url, json=None, timeout=None):  # noqa: A002 — matches httpx sig
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResp()

    # httpx is imported inside slack._post_httpx; patch the module surface.
    import httpx

    monkeypatch.setattr(httpx, "post", _fake_post)

    assert slack.is_configured() is True
    ok = slack.post("hello world", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}])
    assert ok is True
    assert captured["url"] == "https://hooks.slack.test/abc"
    assert captured["json"]["text"] == "hello world"
    assert isinstance(captured["json"].get("blocks"), list)
    assert captured["json"]["blocks"][0]["type"] == "section"


# --------------------------------------------------------------------------
# 4. Resume iteration endpoint
# --------------------------------------------------------------------------

def _seed_tailored_resume_with_evidence(tag: str) -> tuple[int, int]:
    """Insert a career_claim + a tailored_resume row referencing it via
    provenance_json + markdown. Returns ``(resume_id, claim_id)``.
    """
    now = time.time()
    with tx() as conn:
        # Need an evidence_source row to satisfy the FK
        cur = conn.execute(
            "INSERT INTO evidence_source (source_type, title, raw_text, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("manual_paste", f"src_{tag}", f"src text for {tag}", f"hash_src_{tag}", now),
        )
        src_id = int(cur.lastrowid)

        cur = conn.execute(
            "INSERT INTO career_claim (source_id, claim_type, claim_text, normalized_claim, "
            "employer, confidence, evidence_strength, user_verified, allowed_for_resume, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                src_id,
                "accomplishment",
                f"Shipped service X handling 10k rps for {tag}.",
                f"Shipped service X handling 10k rps for {tag}.",
                "AcmeCo",
                0.9,
                "strong",
                1,
                1,
                now,
            ),
        )
        claim_id = int(cur.lastrowid)

        # A minimal job_posting so the FK on tailored_resume passes when set.
        cur = conn.execute(
            "INSERT INTO job_posting (source, title, company, description, discovered_at, hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test_src", f"Test Role {tag}", "TestCo", "desc", now, f"hash_job_{tag}"),
        )
        job_id = int(cur.lastrowid)

        markdown = (
            "# Test User\n\n"
            "## Experience\n"
            f"- [AcmeCo] Shipped service X handling 10k rps for {tag}.\n"
        )
        provenance = {
            "segments": {
                "sections[0].items[0]": [claim_id],
            },
            "distinct_evidence_ids": [claim_id],
            "coverage": {"n_segments": 1, "n_with_evidence": 1, "n_without": 0},
        }
        cur = conn.execute(
            "INSERT INTO tailored_resume (job_id, resume_type, markdown, plain_text, "
            "provenance_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, "job_specific", markdown, markdown,
             json.dumps(provenance), now),
        )
        resume_id = int(cur.lastrowid)
    return resume_id, claim_id


def test_resume_iterate_endpoint_returns_original_and_rewritten(client):
    tag = f"iter_{int(time.time() * 1000)}"
    resume_id, claim_id = _seed_tailored_resume_with_evidence(tag)

    resp = client.post(
        f"/api/resume/{resume_id}/iterate",
        json={
            "section_index": 0,
            "item_index": 0,
            "instruction": "Make the bullet tighter, lead with a strong verb.",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    data = body["data"]
    assert data["accepted"] is False
    assert data["original"]["text"]
    assert tag in data["original"]["text"]
    assert claim_id in data["original"]["evidence_ids"]
    # Rewritten must be present + its evidence_ids must be a subset of original
    assert data["rewritten"]["text"]
    rew_ids = set(data["rewritten"]["evidence_ids"])
    orig_ids = set(data["original"]["evidence_ids"])
    assert rew_ids.issubset(orig_ids), (
        f"rewrite introduced new evidence_ids: {rew_ids - orig_ids}"
    )
    assert rew_ids, "rewrite must keep at least one original evidence_id"


def test_resume_iterate_drops_unsupported_bullets():
    """The accept-iteration path must reject evidence_ids that aren't a
    subset of the original bullet's allowed set — that's how a rewrite
    is prevented from smuggling in new (unverified) claims.
    """
    from backend.app.tailoring import resume_iteration

    tag = f"drop_{int(time.time() * 1000)}"
    resume_id, claim_id = _seed_tailored_resume_with_evidence(tag)

    # Try to accept a rewrite that cites an evidence_id we never linked.
    result = resume_iteration.accept_iteration(
        resume_id=resume_id,
        section_index=0,
        item_index=0,
        new_text="Bogus rewrite citing a fabricated source.",
        new_evidence_ids=[claim_id + 999_999],   # nonexistent / not in allowed set
    )
    assert result["ok"] is False, f"expected accept to fail: {result!r}"
    assert "subset" in (result.get("detail") or "").lower()

    # Likewise a valid rewrite (citing the existing claim_id) should succeed.
    result_ok = resume_iteration.accept_iteration(
        resume_id=resume_id,
        section_index=0,
        item_index=0,
        new_text=f"Tightened bullet for {tag}.",
        new_evidence_ids=[claim_id],
    )
    assert result_ok["ok"] is True, f"expected accept to succeed: {result_ok!r}"
    assert result_ok["accepted"]["evidence_ids"] == [claim_id]
    assert tag in result_ok["accepted"]["text"]

    # And the bullet should now be persisted into the markdown
    conn = get_conn()
    row = conn.execute("SELECT markdown FROM tailored_resume WHERE id = ?", (resume_id,)).fetchone()
    assert row is not None
    assert tag in (row["markdown"] or "")
