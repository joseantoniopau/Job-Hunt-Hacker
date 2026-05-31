"""Prometheus metrics — counters/histograms + HTTP middleware.

Optional dep. If `prometheus-client` is missing we expose no-op stand-ins
so call sites can do `metrics.adapter_search_total.labels(...).inc()`
without guarding every line.

Counters/histograms exposed (importable globally):
  http_requests_total     {method, path, status}
  http_request_duration   {method, path}
  adapter_search_total    {adapter, outcome}
  autopilot_runs_total    {outcome}
  scoring_duration_seconds

The middleware wraps every HTTP request to update the http_* metrics.
`/metrics` itself is excluded from the counters so scrape traffic doesn't
inflate dashboards.
"""
from __future__ import annotations

import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Pin the legacy Prometheus exposition content-type. prometheus_client >=0.20
# defaults to `version=1.0.0` but every operational tool (cAdvisor, kube
# exporters, Grafana scrapers) still expects 0.0.4 as the canonical text
# format. We emit 0.0.4 explicitly so the contract is stable across client
# upgrades.
CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


class _Noop:
    """Stand-in for a prometheus metric when the client isn't installed."""

    def labels(self, *_a: Any, **_kw: Any) -> "_Noop":
        return self

    def inc(self, *_a: Any, **_kw: Any) -> None:
        return None

    def observe(self, *_a: Any, **_kw: Any) -> None:
        return None

    def time(self):  # pragma: no cover — present for parity with prom API
        class _Ctx:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False
        return _Ctx()


try:
    from prometheus_client import (  # type: ignore
        REGISTRY,
        CollectorRegistry,
        Counter,
        Histogram,
        generate_latest as _prom_generate_latest,
    )
    _HAS_PROM = True
except Exception:  # noqa: BLE001
    _HAS_PROM = False
    REGISTRY = None  # type: ignore
    CollectorRegistry = None  # type: ignore
    Counter = None  # type: ignore
    Histogram = None  # type: ignore

    def _prom_generate_latest(*_a: Any, **_kw: Any) -> bytes:
        return b"# prometheus-client not installed\n"


def _make_counter(name: str, doc: str, labels: list[str]):
    if _HAS_PROM:
        try:
            return Counter(name, doc, labels)
        except ValueError:
            # Already registered (e.g. during pytest module reload). Locate
            # the existing collector and return it so we don't crash.
            for collector in list(REGISTRY._collector_to_names.keys()):  # type: ignore[attr-defined]
                if getattr(collector, "_name", None) == name:
                    return collector
            raise
    return _Noop()


def _make_histogram(name: str, doc: str, labels: list[str] | None = None):
    if _HAS_PROM:
        try:
            if labels:
                return Histogram(name, doc, labels)
            return Histogram(name, doc)
        except ValueError:
            for collector in list(REGISTRY._collector_to_names.keys()):  # type: ignore[attr-defined]
                if getattr(collector, "_name", None) == name:
                    return collector
            raise
    return _Noop()


# ---- exposed metrics ----------------------------------------------------

http_requests_total = _make_counter(
    "jhh_http_requests_total",
    "Total HTTP requests handled by Job Hunt Hacker.",
    ["method", "path", "status"],
)

http_request_duration_seconds = _make_histogram(
    "jhh_http_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "path"],
)

adapter_search_total = _make_counter(
    "jhh_adapter_search_total",
    "Job board adapter searches by outcome (ok|error).",
    ["adapter", "outcome"],
)

autopilot_runs_total = _make_counter(
    "jhh_autopilot_runs_total",
    "Autopilot run outcomes.",
    ["outcome"],
)

scoring_duration_seconds = _make_histogram(
    "jhh_scoring_duration_seconds",
    "Per-job scoring latency in seconds.",
)


# ---- helpers ------------------------------------------------------------

def render_latest() -> tuple[bytes, str]:
    """Render the global Prometheus registry to text. Returns (body, content_type)."""
    if not _HAS_PROM:
        return _prom_generate_latest(), CONTENT_TYPE_LATEST
    return _prom_generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def _normalize_path(request: Request) -> str:
    """Use the matched route template when available so we don't explode
    cardinality on path params like `/api/jobs/12345`. Fallback to the raw
    URL path."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    return request.url.path


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Increments the http_* metrics for every request except /metrics itself."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        # Skip metrics scrape so a busy Prometheus doesn't drown the histogram.
        if request.url.path == "/metrics":
            return await call_next(request)

        start = time.perf_counter()
        status = 500
        try:
            response: Response = await call_next(request)
            status = response.status_code
            return response
        except Exception:
            status = 500
            raise
        finally:
            elapsed = time.perf_counter() - start
            path = _normalize_path(request)
            method = request.method.upper()
            try:
                http_requests_total.labels(method=method, path=path, status=str(status)).inc()
                http_request_duration_seconds.labels(method=method, path=path).observe(elapsed)
            except Exception:
                # Never let a metrics bug break a real request.
                pass
