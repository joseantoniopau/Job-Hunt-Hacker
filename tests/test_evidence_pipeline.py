"""Tests for the evidence ingestion + extraction + retrieval pipeline."""
from __future__ import annotations

import time

from backend.app.db import init_db
from backend.app.services import career_vault
from backend.app.services.evidence_extractor import extract_claims


def _setup():
    init_db()


def test_add_source_dedup():
    _setup()
    tag = f"dedup_{int(time.time() * 1000)}"
    body = f"unique content for {tag} — pinning content hash"
    a = career_vault.add_source("manual_paste", title=tag, raw_text=body)
    b = career_vault.add_source("manual_paste", title=tag, raw_text=body)
    assert isinstance(a, int) and isinstance(b, int)
    assert a == b


def test_extract_claims_handles_empty_text():
    assert extract_claims(0, "", "manual_paste") == []
    assert extract_claims(0, "   ", "manual_paste") == []


def test_extract_claims_no_fabrication():
    """Every emitted claim must be grounded in the source text — no skill or
    company appears in a claim that wasn't present in the original input.
    """
    text = "I worked at Acme as a Python engineer."
    claims = extract_claims(0, text, "manual_paste")
    assert claims, "expected at least one claim"

    text_lc = text.lower()
    for c in claims:
        # The literal claim text must be supported in the source. The extractor
        # already filters by _supported(), but we re-assert it from the test side.
        ctext = (c.get("claim_text") or "").lower()
        # Either a substring (short) or every alphabetic token appears in source
        if ctext and ctext not in text_lc:
            tokens = [t for t in ctext.replace(",", " ").replace(".", " ").split() if len(t) >= 3]
            for tok in tokens:
                assert tok in text_lc, f"fabricated token {tok!r} in claim {ctext!r}"

        # Specific fabrication checks: no skill outside source vocabulary
        skill = (c.get("skill") or "").lower()
        if skill:
            assert skill in text_lc, f"claim mentions skill {skill!r} not in source"

        # Specific fabrication checks: employer must appear in source
        emp = (c.get("employer") or "").lower()
        if emp:
            assert emp in text_lc, f"claim mentions employer {emp!r} not in source"


def test_vault_retrieve_for_job_returns_supported_only():
    """Add three distinct sources and confirm semantic retrieval surfaces
    the source whose text most resembles the job description query."""
    _setup()
    tag = f"ret_{int(time.time() * 1000)}"
    src_a_text = f"Python engineer at Acme {tag}"
    src_b_text = f"Rust engineer at Beta {tag}"
    src_c_text = f"Frontend designer at Gamma {tag}"

    sid_a = career_vault.add_source("manual_paste", title=f"A-{tag}", raw_text=src_a_text)
    sid_b = career_vault.add_source("manual_paste", title=f"B-{tag}", raw_text=src_b_text)
    sid_c = career_vault.add_source("manual_paste", title=f"C-{tag}", raw_text=src_c_text)
    assert sid_a != sid_b != sid_c

    for sid, t in ((sid_a, src_a_text), (sid_b, src_b_text), (sid_c, src_c_text)):
        claims = extract_claims(sid, t, "manual_paste")
        career_vault.add_claims(sid, claims)

    # Use a higher top-k so prior accumulated rows in the project DB
    # don't crowd out the test's own claims from the result window.
    results = career_vault.retrieve_for_job(src_a_text, top=50)
    assert results, "expected at least one retrieved claim"

    # Filter to results that came from THIS test's sources so the assertion
    # is robust against prior DB state from other tests / smoke runs.
    own_results = [r for r in results
                   if int((r.get("evidence") or {}).get("id") or -1) in (sid_a, sid_b, sid_c)]
    assert own_results, "test's own sources should appear in the retrieval window"

    # Top match among OUR sources must come from sid_a
    top = own_results[0]
    top_evidence = top.get("evidence") or {}
    assert int(top_evidence.get("id")) == sid_a, (
        f"expected top match from source {sid_a}, got {top_evidence.get('id')}"
    )
