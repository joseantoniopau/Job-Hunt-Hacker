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
