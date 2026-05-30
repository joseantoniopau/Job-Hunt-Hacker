"""Verify the adapter registry is populated and each adapter has a valid policy."""
from backend.app.services.job_sources import REGISTRY, list_adapters
from backend.app.services.job_sources.base import SourcePolicy


def test_registry_has_expected_adapters():
    names = set(list_adapters())
    expected = {"ashby", "custom_rss", "google_jobs", "greenhouse", "jobspy",
                "lever", "remoteintech", "remotive", "wwr"}
    missing = expected - names
    assert not missing, f"missing adapters: {missing}"


def test_every_adapter_declares_policy():
    for name, adapter in REGISTRY.items():
        assert hasattr(adapter, "policy"), f"{name} missing policy"
        assert adapter.policy.name == name or adapter.policy.name == name.split(":")[0]
        assert adapter.policy.risk_level in ("LEGAL", "GRAY", "TOS-RISK")
        assert adapter.policy.recommended_mode in ("research", "assisted", "auto")


def test_no_adapter_silently_allows_auto_apply_for_risky_sources():
    for name, adapter in REGISTRY.items():
        if adapter.policy.risk_level in ("GRAY", "TOS-RISK"):
            assert adapter.policy.apply_automation_allowed is False, \
                f"{name} marked risky but allows auto-apply"
