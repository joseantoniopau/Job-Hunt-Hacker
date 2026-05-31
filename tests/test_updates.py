"""Tests for the self-update check.

Covers:
  * /api/updates/check returns the expected JSON shape even when the
    GitHub call fails (monkeypatched).
  * The CLI wrapper scripts/check_for_updates.py runs without crashing.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.routers import updates as updates_mod


REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_client(monkeypatch: pytest.MonkeyPatch, release: object | None) -> TestClient:
    """Build an isolated FastAPI app that mounts only the updates router so
    tests don't depend on main.py loading every router."""
    # Reset cache + force the upstream fetch to a deterministic value
    monkeypatch.setattr(
        updates_mod, "_cache", {"ts": 0.0, "payload": None}, raising=False
    )
    monkeypatch.setattr(updates_mod, "_fetch_latest_release", lambda timeout=5.0: release)

    app = FastAPI()
    app.version = "0.2.0"
    app.include_router(updates_mod.router)
    return TestClient(app)


def test_check_endpoint_returns_shape_when_github_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when GitHub is unreachable we must return a fully-formed payload."""
    client = _make_client(monkeypatch, None)

    r = client.get("/api/updates/check")
    assert r.status_code == 200

    body = r.json()
    assert body.get("ok") is True
    data = body.get("data") or {}

    # Required fields, regardless of upstream success
    for key in ("current", "latest", "update_available", "release_url"):
        assert key in data, f"missing field: {key}"

    assert isinstance(data["current"], str) and data["current"]
    assert data["latest"] is None
    assert data["update_available"] is False
    assert data["release_url"] == ""
    # Surfaced error message so the UI can show a friendly note
    assert data.get("error")


def test_check_endpoint_reports_update_when_newer(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_release = {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/joseantoniopau/Job-Hunt-Hacker/releases/tag/v9.9.9",
    }
    client = _make_client(monkeypatch, fake_release)

    r = client.get("/api/updates/check")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["latest"] == "9.9.9"
    assert data["update_available"] is True
    assert "9.9.9" in data["release_url"]
    assert data.get("error") is None


def test_check_endpoint_no_update_when_same_version(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_release = {
        "tag_name": "v0.2.0",
        "html_url": "https://github.com/joseantoniopau/Job-Hunt-Hacker/releases/tag/v0.2.0",
    }
    client = _make_client(monkeypatch, fake_release)

    data = client.get("/api/updates/check").json()["data"]
    assert data["latest"] == "0.2.0"
    assert data["update_available"] is False


def test_cli_check_for_updates_runs_without_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoke scripts/check_for_updates.py as a subprocess so we exercise its
    real shebang/entry path. Network may be unavailable in CI, so we accept
    exit codes 0 (up-to-date) or 1 (update available) — both are 'ran cleanly'.
    Crashes (>=2) would fail the test.
    """
    script = REPO_ROOT / "scripts" / "check_for_updates.py"
    assert script.exists(), f"missing CLI script: {script}"

    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode in (0, 1), (
        f"unexpected exit {proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    # Should print *something*
    assert (proc.stdout or proc.stderr).strip()
