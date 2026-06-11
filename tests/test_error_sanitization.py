"""Error sanitization tests.

Covers the global exception handlers added in ``backend.app.main``:

  * unhandled exceptions return a generic JSON 500 with an ``error_id`` and
    ``request_id`` — and never leak exception text or tracebacks to clients
  * the full traceback (with the real exception text) IS logged server-side
    under the same ``error_id`` so the operator can correlate
  * HTTPExceptions are normalized to ``{ok, detail, request_id}`` while
    preserving their status codes
  * the existing 404-detail behavior keeps working (route-level details are
    preserved; unmatched paths get "not found: <path>")
  * router-level 500s use short stable messages instead of interpolating the
    raw exception
"""
from __future__ import annotations

import logging
import re

import pytest
from fastapi.testclient import TestClient

from backend.app.db import init_db
from backend.app.main import app

SECRET = "SECRET-EXC-TEXT-c9f2/etc/passwd"

# Test-only route that always blows up, so we can exercise the global
# Exception handler without monkeypatching production code paths.
_BOOM_PATH = "/api/_test/boom"
if not any(getattr(r, "path", None) == _BOOM_PATH for r in app.routes):
    @app.get(_BOOM_PATH)
    def _boom() -> dict:  # pragma: no cover - body never returns
        raise RuntimeError(SECRET)

init_db()

# raise_server_exceptions=False lets the registered Exception handler build
# the 500 response instead of re-raising into the test process.
client = TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1) Unhandled exception → sanitized 500
# ---------------------------------------------------------------------------

def test_unhandled_exception_returns_sanitized_500() -> None:
    r = client.get(_BOOM_PATH)
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert body["detail"] == "internal error"
    # error_id is a 12-char hex slug
    assert re.fullmatch(r"[0-9a-f]{12}", body["error_id"])
    assert body["request_id"]
    # No exception text, class name, or traceback leaks to the client.
    assert SECRET not in r.text
    assert "RuntimeError" not in r.text
    assert "Traceback" not in r.text


def test_unhandled_exception_logs_traceback_with_error_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="jhh"):
        r = client.get(_BOOM_PATH)
    assert r.status_code == 500
    error_id = r.json()["error_id"]
    record_text = "\n".join(rec.getMessage() for rec in caplog.records)
    # The server log carries the error_id, the real exception text and the
    # traceback — everything the client response deliberately omits.
    assert error_id in record_text
    assert SECRET in record_text
    assert "Traceback" in record_text


def test_unhandled_exception_echoes_inbound_request_id() -> None:
    rid = "test-rid-boom-42"
    r = client.get(_BOOM_PATH, headers={"X-Request-ID": rid})
    assert r.status_code == 500
    assert r.json()["request_id"] == rid


# ---------------------------------------------------------------------------
# 2) HTTPException normalization: {ok, detail, request_id}, status preserved
# ---------------------------------------------------------------------------

def test_route_level_404_detail_preserved() -> None:
    r = client.get("/api/jobs/999999")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["detail"] == "job 999999 not found"
    assert body["request_id"]


def test_catch_all_404_includes_path_and_request_id() -> None:
    r = client.get("/api/definitely-not-a-route")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["detail"] == "not found: /api/definitely-not-a-route"
    assert body["request_id"]


def test_4xx_http_exception_normalized_with_request_id() -> None:
    rid = "test-rid-salary-7"
    r = client.get("/api/salary/market", params={"role": "Engineer", "window_days": 0},
                   headers={"X-Request-ID": rid})
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["detail"] == "window_days must be between 1 and 3650"
    assert body["request_id"] == rid
    assert r.headers["X-Request-ID"] == rid


# ---------------------------------------------------------------------------
# 3) Router 500s use stable messages — no raw exception interpolation
# ---------------------------------------------------------------------------

def test_router_500_does_not_leak_exception_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.routers import resume as resume_router

    def _explode(**kwargs):  # noqa: ANN003
        raise RuntimeError(SECRET)

    monkeypatch.setattr(resume_router.resume_tailor, "tailor_resume", _explode)
    r = client.post("/api/resume/tailor", json={"job_id": 1})
    assert r.status_code == 500
    body = r.json()
    assert body["ok"] is False
    assert body["detail"] == "tailor failed (see server log)"
    assert body["request_id"]
    assert SECRET not in r.text
