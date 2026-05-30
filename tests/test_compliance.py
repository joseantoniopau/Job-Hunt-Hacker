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


def test_legal_source_still_blocked_by_policy():
    """Greenhouse is LEGAL but has apply_automation_allowed=False, so the
    aggregate gate must still return False."""
    allowed, reason = compliance.is_auto_apply_allowed("greenhouse")
    assert allowed is False
    assert "automation" in reason.lower() or "policy" in reason.lower()


def test_unknown_source_is_blocked():
    """An unrecognized source must default to denied."""
    allowed, reason = compliance.is_auto_apply_allowed("not_a_real_source_xyz")
    assert allowed is False
    assert "unknown" in reason.lower()
