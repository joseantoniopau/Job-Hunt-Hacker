"""Tests for the provenance + guardrails layer — the no-fabrication core."""
import pytest

try:
    from backend.app.tailoring.provenance import ProvenanceMap
    _OK = True
except Exception:
    _OK = False


@pytest.mark.skipif(not _OK, reason="tailoring module not available")
def test_provenance_coverage_basic():
    p = ProvenanceMap()
    p.link("s1", [1, 2])
    p.link("s2", [3])
    p.link("s3", [])
    c = p.coverage()
    assert c["n_segments"] == 3
    assert c["n_with_evidence"] == 2
    assert c["n_without"] == 1


@pytest.mark.skipif(not _OK, reason="tailoring module not available")
def test_provenance_to_dict_roundtrip():
    p = ProvenanceMap()
    p.link("s1", [1, 2])
    d = p.to_dict()
    assert isinstance(d, dict)


try:
    from backend.app.llm.guardrails import validate_provenance
    _G_OK = True
except Exception:
    _G_OK = False


@pytest.mark.skipif(not _G_OK, reason="guardrails module not available")
def test_validate_provenance_drops_unsupported():
    out = {
        "sections": [
            {"title": "Experience", "items": [
                {"text": "Real bullet", "evidence_ids": [1]},
                {"text": "Fabricated", "evidence_ids": []},
                {"text": "Wrong ID", "evidence_ids": [999]},
            ]}
        ]
    }
    cleaned = validate_provenance(out, evidence_ids_allowed={1})
    items = cleaned["sections"][0]["items"]
    # Only the supported one should survive
    surviving = [it for it in items if it.get("evidence_ids")]
    assert len(surviving) == 1
    assert surviving[0]["text"] == "Real bullet"
