"""End-to-end smoke test for resume tailoring against an evidence-empty vault.

The deterministic TemplateProvider returns "" for JSON, so tailor_resume
falls back to the claim-stitching path. We verify the returned bundle
contains an honesty_report and does NOT contain fabricated bullets.
"""
from __future__ import annotations

import time

import pytest

from backend.app.db import init_db, tx

try:
    from backend.app.tailoring.resume_tailor import tailor_resume
    _TAILOR_OK = True
except Exception:
    _TAILOR_OK = False


@pytest.mark.skipif(not _TAILOR_OK, reason="tailoring module not available")
def test_tailor_with_no_evidence_produces_empty_or_gap_report():
    init_db()
    # Insert a unique fake job; use timestamp suffix to avoid colliding with
    # anything already in the DB.
    tag = f"e2e_{int(time.time() * 1000)}"
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO job_posting (source, title, company, description, "
            "discovered_at, hash) VALUES (?, ?, ?, ?, ?, ?)",
            ("test_src", f"Test Role {tag}", "TestCo",
             "Python AWS engineer needed", time.time(), f"hash_{tag}"),
        )
        job_id = int(cur.lastrowid)

    result = tailor_resume(job_id)
    assert isinstance(result, dict)
    # The bundle must include the honesty report — it's the whole point
    assert "honesty_report" in result
    assert result.get("job_id") == job_id

    structured = result.get("structured") or {}
    sections = structured.get("sections") or []
    # Every surviving bullet must have at least one evidence_id (the
    # guardrails layer drops anything else). Verify that invariant holds.
    for sec in sections:
        for item in (sec.get("items") or []):
            ev = item.get("evidence_ids") or []
            assert ev, f"unsupported bullet survived: {item!r}"

    # Provenance + ATS reports both present
    assert "provenance" in result
    assert "ats_report" in result
