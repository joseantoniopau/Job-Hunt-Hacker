"""Compliance & gating for auto-apply / packet prep.

Single source of truth for "may we automate this?" decisions. Backed by
the static policy file `data/seed/source_policies.json` plus a runtime
kill-switch flag stored under `cache/auto_apply_halt.flag`.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from ..config import settings
from ..db import audit, get_conn

log = logging.getLogger("jhh.compliance")

_POLICY_PATH = settings.data_dir / "seed" / "source_policies.json"
_HALT_FLAG = settings.cache_dir / "auto_apply_halt.flag"

_cache: dict | None = None
_cache_loaded_at: float = 0.0
_CACHE_TTL_S = 60.0


def _load_policies() -> dict:
    global _cache, _cache_loaded_at
    now = time.time()
    if _cache is not None and (now - _cache_loaded_at) < _CACHE_TTL_S:
        return _cache
    if not _POLICY_PATH.exists():
        _cache = {}
        _cache_loaded_at = now
        return _cache
    try:
        _cache = json.loads(_POLICY_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("source_policies.json parse failed: %s", exc)
        _cache = {}
    _cache_loaded_at = now
    return _cache


def get_policy(source: str) -> Optional[dict]:
    if not source:
        return None
    pol = _load_policies()
    if source in pol:
        return pol[source]
    # fuzzy: jobspy may report sub-source like "jobspy_indeed"
    for key in pol:
        if source.startswith(key) or key.startswith(source):
            return pol[key]
    return None


def is_auto_apply_allowed(source: str) -> tuple[bool, str]:
    """Static policy check. Even sources with apply_automation_allowed=True
    are STILL only prepared and queued — we never auto-submit.
    """
    pol = get_policy(source)
    if pol is None:
        return False, f"unknown source: {source}"
    risk = (pol.get("risk_level") or "").upper()
    if risk in ("GRAY", "TOS-RISK"):
        return False, f"{risk} risk: {pol.get('display_name', source)} — packet prep blocked"
    if not pol.get("apply_automation_allowed", False):
        return False, f"policy: {pol.get('display_name', source)} does not allow apply automation (LEGAL but apply via company site)"
    return True, "allowed"


def kill_switch_active() -> bool:
    return _HALT_FLAG.exists()


def halt() -> None:
    _HALT_FLAG.parent.mkdir(parents=True, exist_ok=True)
    _HALT_FLAG.write_text(str(time.time()), encoding="utf-8")
    try:
        audit("auto_apply_halt", "system")
    except Exception as exc:  # noqa: BLE001
        log.debug("audit halt failed: %s", exc)


def resume(i_understand: bool = False) -> bool:
    """Lift the kill switch. Requires explicit confirmation."""
    if not i_understand:
        return False
    if _HALT_FLAG.exists():
        try:
            _HALT_FLAG.unlink()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not unlink halt flag: %s", exc)
            return False
    try:
        audit("auto_apply_resume", "system")
    except Exception:
        pass
    return True


def today_applied_count() -> int:
    """Count auto-apply packet-prep actions performed today (audit-driven)."""
    conn = get_conn()
    # midnight in local time
    now = time.time()
    midnight = now - (now % 86400)
    row = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE ts >= ? AND action IN (?, ?)",
        (midnight, "auto_apply_packet_prepared", "auto_apply_submitted"),
    ).fetchone()
    return int(row[0]) if row else 0


def enforce_caps(profile_today: dict | None = None) -> tuple[bool, str]:
    """Aggregate gate: kill switch + daily cap + min_score sanity.

    profile_today: optional dict with keys {today_count, min_score}
    """
    if not settings.auto_apply_enabled:
        return False, "auto_apply_disabled"
    if kill_switch_active():
        return False, "kill_switch_active"
    today = (profile_today or {}).get("today_count")
    if today is None:
        today = today_applied_count()
    if today >= settings.auto_apply_daily_cap:
        return False, f"daily_cap_reached ({today}/{settings.auto_apply_daily_cap})"
    return True, "ok"


def status_snapshot() -> dict:
    return {
        "enabled": bool(settings.auto_apply_enabled),
        "kill_switch_active": kill_switch_active(),
        "daily_cap": int(settings.auto_apply_daily_cap),
        "min_score": int(settings.auto_apply_min_score),
        "today_count": today_applied_count(),
        "policies_loaded": len(_load_policies()),
    }
