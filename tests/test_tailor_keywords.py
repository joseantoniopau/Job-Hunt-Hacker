"""Tests for the unsupported-keyword wiring in resume tailoring.

analyze_job() never returned a `missing_keywords` field, so the honesty
report's unsupported_job_requirements / missing_evidence were silently empty
on the main path. They are now derived from the keyword matrix itself
(support_status from ats_analyzer, resume_safe from the fallback matrix).
"""
from __future__ import annotations

import time

from backend.app.db import init_db, tx
from backend.app.tailoring.resume_tailor import _unsupported_keywords, tailor_resume


def test_unsupported_keywords_from_analyzer_matrix():
    matrix = [
        {"keyword": "python", "support_status": "supported",
         "importance": "required", "resume_safe": True},
        {"keyword": "kubernetes", "support_status": "unsupported",
         "importance": "required", "resume_safe": False},
        {"keyword": "terraform", "support_status": "weak_evidence",
         "importance": "preferred", "resume_safe": False},
        {"keyword": "graphql", "support_status": "transferable",
         "importance": "nice", "resume_safe": True},
        {"keyword": "kafka", "support_status": "unsupported",
         "importance": "nice", "resume_safe": False},
    ]
    required, everything = _unsupported_keywords(matrix)
    # Only required + unbacked keywords land in the requirements list
    assert required == ["kubernetes"]
    # Every unbacked keyword (unsupported or weak_evidence) is missing evidence
    assert everything == ["kubernetes", "terraform", "kafka"]


def test_unsupported_keywords_from_fallback_matrix_shape():
    """The lightweight fallback matrix has no support_status/importance —
    resume_safe decides, and everything unbacked counts as required."""
    matrix = [
        {"keyword": "python", "resume_safe": True, "evidence_ids": []},
        {"keyword": "kubernetes", "resume_safe": False, "evidence_ids": []},
        {"keyword": "kubernetes", "resume_safe": False, "evidence_ids": []},  # dupe
    ]
    required, everything = _unsupported_keywords(matrix)
    assert required == ["kubernetes"]
    assert everything == ["kubernetes"]


def test_unsupported_keywords_handles_garbage():
    required, everything = _unsupported_keywords([])
    assert required == [] and everything == []
    required, everything = _unsupported_keywords([None, "x", {"keyword": ""}])  # type: ignore[list-item]
    assert required == [] and everything == []


def test_tailor_resume_wires_unsupported_keywords_into_honesty_report():
    """End to end: the honesty report's unsupported_job_requirements,
    missing_evidence and gaps_flagged stay consistent with the keyword
    matrix that the tailoring run actually produced."""
    init_db()
    tag = f"kw_{int(time.time() * 1000)}"
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO job_posting (source, title, company, description, "
            "discovered_at, hash) VALUES (?, ?, ?, ?, ?, ?)",
            ("test_src", f"Platform Engineer {tag}", "TestCo",
             "Required: Kubernetes, Terraform. Preferred: GraphQL.",
             time.time(), f"hash_{tag}"),
        )
        job_id = int(cur.lastrowid)

    result = tailor_resume(job_id)
    honesty = result.get("honesty_report") or {}
    matrix = (result.get("keyword_report") or {}).get("matrix") or []

    expected_required, expected_all = _unsupported_keywords(matrix)
    assert honesty.get("unsupported_job_requirements") == expected_required
    assert honesty.get("missing_evidence") == expected_all

    # Unbacked required keywords also surface as gaps (capped at 8)
    gaps = honesty.get("gaps_flagged") or []
    for kw in expected_required[:8]:
        assert f"No vault evidence for required job keyword: {kw}" in gaps

    # The job explicitly requires keywords; with no fabrication allowed the
    # matrix must mention them, and any the vault can't back must be reported.
    matrix_kws = {(m.get("keyword") or "").lower() for m in matrix if isinstance(m, dict)}
    assert {"kubernetes", "terraform"} <= matrix_kws
    unbacked_lower = {k.lower() for k in expected_all}
    for m in matrix:
        if not isinstance(m, dict):
            continue
        status = m.get("support_status")
        unbacked = (status in ("unsupported", "weak_evidence")) if status is not None \
            else not m.get("resume_safe")
        assert ((m.get("keyword") or "").lower() in unbacked_lower) == unbacked
