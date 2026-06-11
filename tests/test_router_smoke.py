"""Smoke test: every parameterless GET endpoint must respond without a 500.

Walks the live FastAPI route table so new routers are covered automatically.
A 4xx (e.g. 404/400 for endpoints needing state) is fine — we only fail on
5xx, which means an unhandled server error. This is the cheap net that
catches a router that imports fine but crashes on its first call.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)

# Endpoints with side effects or external calls we don't want in a smoke pass.
_SKIP_PATHS = {
    "/api/data/export",          # streams a file
    "/api/data/export.zip",
    "/metrics",                  # prometheus text, may be heavy
}


def _parameterless_get_paths():
    paths = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if "GET" not in methods:
            continue
        if "{" in path:           # needs a path param
            continue
        if path in _SKIP_PATHS:
            continue
        if not path.startswith(("/api", "/")):
            continue
        paths.append(path)
    return sorted(set(paths))


def _is_unhandled_crash(resp) -> bool:
    """An UNHANDLED crash is a 500 carrying the global handler's envelope
    (error_id / detail='internal error'). A deliberately-raised status — incl.
    503 'not configured' for optional integrations — is handled behavior and
    must not fail the smoke pass."""
    if resp.status_code != 500:
        return False
    try:
        body = resp.json()
    except Exception:
        return True  # 500 with a non-JSON body is a raw crash
    return "error_id" in body or body.get("detail") == "internal error"


def test_smoke_every_parameterless_get():
    failures = []
    for path in _parameterless_get_paths():
        try:
            r = client.get(path)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path} raised {type(exc).__name__}: {exc}")
            continue
        if _is_unhandled_crash(r):
            failures.append(f"{path} -> {r.status_code}: {r.text[:160]}")
    assert not failures, "unhandled 500s from read-only endpoints:\n" + "\n".join(failures)


def test_health_ok():
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"]
    # Every router declared in main must have loaded cleanly.
    assert body["routers_failed"] == {}


def test_root_serves_ui():
    r = client.get("/")
    assert r.status_code == 200
    assert "JOB" in r.text.upper()
