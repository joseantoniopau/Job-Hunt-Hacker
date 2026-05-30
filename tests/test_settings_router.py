"""HTTP tests for the settings router (api-keys + source-test)."""
from __future__ import annotations

import os

from fastapi.testclient import TestClient

from backend.app.config import ENV_FILE
from backend.app.main import app

client = TestClient(app)


def test_list_api_keys_returns_supported_envs():
    r = client.get("/api/settings/api-keys")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    keys = (body.get("data") or {}).get("keys") or []
    # The whitelist holds 10+ entries (see API_KEYS in settings.py)
    assert len(keys) >= 10
    for k in keys:
        # Every entry has the canonical metadata shape
        assert "env" in k
        assert "label" in k
        assert "group" in k
        assert "configured" in k
        assert isinstance(k.get("configured"), bool)


def test_put_api_keys_rejects_unknown_env():
    r = client.put("/api/settings/api-keys", json={"keys": {"NOT_A_REAL_KEY": "x"}})
    assert r.status_code == 400


def test_put_api_keys_writes_to_env_file():
    """Set a benign key, verify .env was written, then clear it.

    The settings router uses JHH_LLM_MODEL as a "stored but unvalidated" key,
    so it's perfect for this round-trip test.
    """
    env = "JHH_LLM_MODEL"
    value = "test-model-XYZ"

    # Capture pre-state so we can restore the user's original env file content
    original_text = ENV_FILE.read_text() if ENV_FILE.exists() else None

    try:
        r = client.put("/api/settings/api-keys", json={"keys": {env: value}})
        assert r.status_code == 200
        data = r.json().get("data") or {}
        assert env in data.get("updated", [])
        # File must now contain the line
        assert ENV_FILE.exists()
        contents = ENV_FILE.read_text()
        assert f"{env}={value}" in contents
    finally:
        # Restore: clear the value (writes empty), then restore original file body
        try:
            client.put("/api/settings/api-keys", json={"keys": {env: ""}})
        except Exception:
            pass
        try:
            if original_text is None:
                if ENV_FILE.exists():
                    ENV_FILE.unlink()
            else:
                ENV_FILE.write_text(original_text)
        except Exception:
            pass
        # Also clear from current process env so other tests see fresh state
        os.environ.pop(env, None)


def test_test_api_key_handles_missing_value():
    """No value supplied and ANTHROPIC_API_KEY not set → status='empty'."""
    # Defensive: temporarily clear the env var so the test is deterministic
    # even when the developer has a key configured.
    saved = os.environ.pop("ANTHROPIC_API_KEY", None)
    from backend.app.config import settings as _settings  # noqa: WPS433
    saved_attr = getattr(_settings, "anthropic_api_key", "")
    _settings.anthropic_api_key = ""
    try:
        r = client.post("/api/settings/api-keys/test", json={"env": "ANTHROPIC_API_KEY"})
        assert r.status_code == 200
        data = r.json().get("data") or {}
        assert data.get("status") == "empty"
        assert data.get("ok") is False
    finally:
        if saved is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved
        _settings.anthropic_api_key = saved_attr


def test_test_source_invalid_name_returns_404():
    r = client.post("/api/settings/sources/test/not-a-source")
    assert r.status_code == 404
