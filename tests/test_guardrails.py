"""Tests for the LLM output guardrails (provenance + keyword safety + fabrication)."""
from __future__ import annotations

from backend.app.llm.guardrails import (
    assert_no_fabrication,
    enforce_keyword_safety,
    validate_provenance,
)


def test_validate_provenance_drops_unsupported_segments():
    """Exactly two of three items must be dropped: the one with an empty
    evidence_ids list and the one citing an unknown ID 999."""
    out = {
        "sections": [{
            "title": "Experience",
            "items": [
                {"text": "Real bullet", "evidence_ids": [1]},
                {"text": "Fabricated", "evidence_ids": []},
                {"text": "Wrong ID", "evidence_ids": [999]},
            ],
        }],
    }
    cleaned = validate_provenance(out, {1})
    items = cleaned["sections"][0]["items"]
    # Only the supported one survives
    assert len(items) == 1
    assert items[0]["text"] == "Real bullet"
    assert items[0]["evidence_ids"] == [1]

    dropped = (cleaned.get("honesty_report") or {}).get("dropped_segments") or []
    assert len(dropped) == 2
    reasons = {d.get("reason") for d in dropped}
    assert "no_valid_evidence_ids" in reasons


def test_validate_provenance_keeps_only_tagged_boilerplate_paragraphs():
    """Only kind='boilerplate' paragraphs may ship without evidence_ids —
    greeting/closing survive, an untagged unsupported body paragraph dies."""
    out = {
        "paragraphs": [
            {"text": "Dear Hiring Manager,", "evidence_ids": [], "kind": "boilerplate"},
            {"text": "Led the payments migration.", "evidence_ids": [1], "kind": "body"},
            {"text": "I single-handedly invented Kubernetes.", "evidence_ids": [], "kind": "body"},
            {"text": "Thank you,\nJane", "evidence_ids": [], "kind": "boilerplate"},
        ],
    }
    cleaned = validate_provenance(out, {1})
    texts = [p["text"] for p in cleaned["paragraphs"]]
    assert texts == [
        "Dear Hiring Manager,",
        "Led the payments migration.",
        "Thank you,\nJane",
    ]
    dropped = (cleaned.get("honesty_report") or {}).get("dropped_segments") or []
    assert len(dropped) == 1
    assert dropped[0]["where"] == "paragraphs[2]"


def test_validate_provenance_no_positional_exemption_for_edge_paragraphs():
    """First/last paragraphs get no free pass: without a boilerplate tag and
    without valid evidence_ids they are dropped like any other segment."""
    out = {
        "paragraphs": [
            {"text": "I built the original ARPANET.", "evidence_ids": []},
            {"text": "Backed claim.", "evidence_ids": [7]},
            {"text": "I also run NASA on weekends.", "evidence_ids": [999]},
        ],
    }
    cleaned = validate_provenance(out, {7})
    assert [p["text"] for p in cleaned["paragraphs"]] == ["Backed claim."]
    dropped = (cleaned.get("honesty_report") or {}).get("dropped_segments") or []
    assert {d["where"] for d in dropped} == {"paragraphs[0]", "paragraphs[2]"}


def test_cover_letter_deterministic_fallback_survives_guardrails():
    """The deterministic cover-letter fallback tags greeting/closing as
    boilerplate, so the full flow keeps them even with the strict (no
    positional exemption) provenance rules."""
    from backend.app.db import init_db, tx
    from backend.app.tailoring import cover_letter
    import time

    init_db()
    tag = f"cl_{int(time.time() * 1000)}"
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO job_posting (source, title, company, description, "
            "discovered_at, hash) VALUES (?, ?, ?, ?, ?, ?)",
            ("test_src", f"Test Role {tag}", "TestCo",
             "Python AWS engineer needed", time.time(), f"hash_{tag}"),
        )
        job_id = int(cur.lastrowid)

    result = cover_letter.generate(job_id)
    paragraphs = result.get("paragraphs") or []
    kinds = [p.get("kind") for p in paragraphs if isinstance(p, dict)]
    # Greeting + closing boilerplate must survive the guardrails pass
    assert kinds.count("boilerplate") == 2
    assert paragraphs[0].get("kind") == "boilerplate"
    assert paragraphs[-1].get("kind") == "boilerplate"
    # Any surviving body paragraph must carry evidence
    for p in paragraphs:
        if p.get("kind") != "boilerplate":
            assert p.get("evidence_ids"), f"unsupported paragraph survived: {p!r}"


def test_enforce_keyword_safety_splits_safe_unsafe():
    matrix = [
        {"keyword": "python", "resume_safe": True},
        {"keyword": "kubernetes", "resume_safe": False},
        {"keyword": "go", "resume_safe": True},
        # missing 'rust' → not in matrix → unsafe
    ]
    safe, unsafe = enforce_keyword_safety(
        ["Python", "Kubernetes", "Rust", "Go"], matrix
    )
    # Order is preserved
    assert safe == ["Python", "Go"]
    assert unsafe == ["Kubernetes", "Rust"]


def test_enforce_keyword_safety_handles_empty():
    safe, unsafe = enforce_keyword_safety([], [])
    assert safe == []
    assert unsafe == []


def test_assert_no_fabrication_flags_unsupported_ngrams():
    text = (
        "I shipped Python services and led a team of fifty Mars colonists."
    )
    evidence = ["I shipped Python services for our company."]
    suspect = assert_no_fabrication(text, evidence)
    # The fabricated "Mars colonists" phrase has to show up somewhere
    joined = " | ".join(suspect)
    assert "mars" in joined.lower() or "colonists" in joined.lower(), (
        f"expected fabricated phrase about Mars/colonists in: {suspect}"
    )


def test_assert_no_fabrication_clean_text_returns_empty():
    text = "I shipped Python services for our company."
    evidence = ["I shipped Python services for our company."]
    suspect = assert_no_fabrication(text, evidence)
    assert suspect == []
