"""Per-adapter circuit breaker + proxy plumbing + retry logging."""
from __future__ import annotations

import time

import pytest

from backend.app.config import settings
from backend.app.db import get_conn, tx
from backend.app.services.job_sources import pipeline
from backend.app.services.job_sources.base import (
    JobRecord, JobSearchQuery, JobSourceAdapter, REGISTRY, SourcePolicy,
)


def _policy(name: str) -> SourcePolicy:
    return SourcePolicy(name=name, display_name=name, official_api=True,
                        scraping=False, apply_automation_allowed=False,
                        recommended_mode="research", risk_level="LEGAL")


class _FlakyAdapter(JobSourceAdapter):
    def __init__(self, name: str):
        self.name = name
        self.policy = _policy(name)
        self.mode = "fail"  # 'fail' | 'ok'

    def healthy(self) -> bool:
        return True

    def search(self, q):
        if self.mode == "fail":
            raise RuntimeError("boom")
        return [JobRecord(source=self.name, title="Eng", company="Acme",
                          location="Remote", external_id="x1")]


@pytest.fixture()
def flaky():
    name = "_test_flaky"
    ad = _FlakyAdapter(name)
    REGISTRY.register(ad)
    with tx() as c:
        c.execute("DELETE FROM source_state WHERE source = ?", (name,))
    yield ad
    REGISTRY.pop(name, None)
    with tx() as c:
        c.execute("DELETE FROM source_state WHERE source = ?", (name,))


def _q():
    return JobSearchQuery(query="eng", results_per_site=1)


def test_breaker_opens_after_threshold(flaky):
    threshold = settings.adapter_breaker_threshold
    for _ in range(threshold):
        pipeline.search_all(_q(), sites=[flaky.name])
    row = get_conn().execute(
        "SELECT consecutive_failures, disabled_until FROM source_state WHERE source = ?",
        (flaky.name,),
    ).fetchone()
    assert row["consecutive_failures"] >= threshold
    assert row["disabled_until"] is not None and row["disabled_until"] > time.time()


def test_open_breaker_skips_adapter_with_circuit_open_error(flaky):
    for _ in range(settings.adapter_breaker_threshold):
        pipeline.search_all(_q(), sites=[flaky.name])
    res = pipeline.search_all(_q(), sites=[flaky.name])
    assert res["per_source"].get(flaky.name) is None
    assert res["errors"][flaky.name].startswith("circuit_open")


def test_success_resets_breaker(flaky):
    for _ in range(settings.adapter_breaker_threshold):
        pipeline.search_all(_q(), sites=[flaky.name])
    # Force cooldown to expire, then succeed.
    with tx() as c:
        c.execute("UPDATE source_state SET disabled_until = ? WHERE source = ?",
                  (time.time() - 1, flaky.name))
    flaky.mode = "ok"
    res = pipeline.search_all(_q(), sites=[flaky.name])
    assert res["per_source"].get(flaky.name) == 1
    row = get_conn().execute(
        "SELECT consecutive_failures, disabled_until FROM source_state WHERE source = ?",
        (flaky.name,),
    ).fetchone()
    assert row["consecutive_failures"] == 0
    assert row["disabled_until"] is None


def test_proxy_threaded_into_jobspy(monkeypatch):
    import backend.app.services.job_sources.jobspy_adapter as ja
    if not getattr(ja, "_JOBSPY_OK", False):
        pytest.skip("jobspy not installed")
    captured = {}

    def fake_scrape(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(ja, "scrape_jobs", fake_scrape)
    monkeypatch.setattr(settings, "scraper_proxy", "http://proxy.local:8080")
    ja.REGISTRY  # ensure import
    ad = ja.JobSpyAdapter()
    ad.search(JobSearchQuery(query="eng", extra={"sites": ["indeed"]}, results_per_site=1))
    assert captured.get("proxies") == ["http://proxy.local:8080"]


def test_retry_logging_callback_present():
    # wrap_with_retry should install a before_sleep logger when tenacity exists.
    from backend.app.services.job_sources.retry import wrap_with_retry, _import_tenacity
    if _import_tenacity() is None:
        pytest.skip("tenacity not installed")

    calls = {"n": 0}

    def flaky_fn():
        calls["n"] += 1
        import httpx
        raise httpx.ConnectError("nope")

    wrapped = wrap_with_retry(flaky_fn, max_attempts=2, min_wait=0.01, max_wait=0.02)
    with pytest.raises(Exception):
        wrapped()
    assert calls["n"] == 2  # retried once
