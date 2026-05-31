"""Tests for compliance gating: auto-apply allow/deny + kill switch lifecycle."""
from __future__ import annotations

from backend.app.applications import compliance


def test_default_auto_apply_disabled():
    """JobSpy is GRAY-risk → blocked regardless of any other flag."""
    allowed, reason = compliance.is_auto_apply_allowed("jobspy")
    assert allowed is False
    assert isinstance(reason, str) and reason  # human-readable explanation


def test_kill_switch_lifecycle():
    # Ensure we start clean — if a prior test halted, lift it first
    if compliance.kill_switch_active():
        compliance.resume(i_understand=True)

    assert compliance.kill_switch_active() is False

    compliance.halt()
    assert compliance.kill_switch_active() is True

    # resume() without confirmation is a no-op
    assert compliance.resume() is False
    assert compliance.kill_switch_active() is True

    # resume() with explicit confirmation lifts the flag
    assert compliance.resume(i_understand=True) is True
    assert compliance.kill_switch_active() is False


def test_legal_source_is_allowed():
    """LEGAL adapters (Greenhouse / Lever / Ashby / Remotive / WWR / Google
    Jobs) may have packets auto-prepared. Submission is still manual — the
    auto_apply pipeline writes status=auto_packet_ready for human review.
    """
    for src in ("greenhouse", "lever", "ashby", "remotive", "wwr"):
        allowed, reason = compliance.is_auto_apply_allowed(src)
        assert allowed is True, f"{src}: {reason}"


def test_gray_source_blocked():
    """GRAY-risk adapters (JobSpy) must NEVER permit auto-prep — user
    clicks through manually because scraping major boards may violate TOS."""
    allowed, reason = compliance.is_auto_apply_allowed("jobspy")
    assert allowed is False
    assert "GRAY" in reason or "blocked" in reason.lower()


def test_unknown_source_is_blocked():
    """An unrecognized source must default to denied."""
    allowed, reason = compliance.is_auto_apply_allowed("not_a_real_source_xyz")
    assert allowed is False
    assert "unknown" in reason.lower()
