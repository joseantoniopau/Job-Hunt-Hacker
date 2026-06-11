"""OAuth token security + secrets hygiene tests.

Covers: encryption-required save path, plaintext legacy read warning,
legacy file migration, the /api/email/disconnect endpoint (with the Google
revoke call mocked), .env permission tightening, and token-redacting logs.
"""
from __future__ import annotations

import json
import logging
import os
import stat

import pytest
from fastapi.testclient import TestClient

from backend.app import config
from backend.app.integrations import gmail
from backend.app.routers import email as email_router
from backend.app.routers import settings as settings_router
from backend.app.security import oauth_tokens
from backend.app.main import app

client = TestClient(app)


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _isolate_store(monkeypatch, tmp_path):
    """Point the token store at a temp dir so tests never touch data/."""
    monkeypatch.setattr(oauth_tokens, "TOKEN_PATH", tmp_path / "oauth_tokens.json")
    monkeypatch.setattr(oauth_tokens, "GMAIL_TOKEN_PATH", tmp_path / "gmail_tokens.json")
    monkeypatch.setattr(oauth_tokens, "KEY_PATH", tmp_path / ".oauth_fernet.key")


# ---- save_tokens: encryption required ----

def test_save_tokens_raises_without_fernet(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    monkeypatch.setattr(oauth_tokens, "_HAS_FERNET", False)
    target = tmp_path / "tokens.json"
    with pytest.raises(RuntimeError, match="unencrypted"):
        oauth_tokens.save_tokens({"access_token": "secret-A"}, target)
    # nothing was written
    assert not target.exists()


def test_save_tokens_encrypts_and_chmods(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    target = tmp_path / "tokens.json"
    oauth_tokens.save_tokens({"access_token": "secret-A", "refresh_token": "secret-R"}, target)
    blob = target.read_bytes()
    assert blob.startswith(b"FERNET1:")
    assert b"secret-A" not in blob and b"secret-R" not in blob
    assert _mode(target) == 0o600
    assert _mode(tmp_path / ".oauth_fernet.key") == 0o600
    # round-trips
    assert oauth_tokens.load_tokens(target) == {
        "access_token": "secret-A", "refresh_token": "secret-R",
    }


def test_load_plaintext_logs_critical_once(monkeypatch, tmp_path, caplog):
    _isolate_store(monkeypatch, tmp_path)
    monkeypatch.setattr(oauth_tokens, "_plaintext_warned", False)
    legacy = tmp_path / "plain.json"
    legacy.write_text(json.dumps({"access_token": "legacy-tok"}))
    with caplog.at_level(logging.CRITICAL, logger="jhh.security.oauth_tokens"):
        out = oauth_tokens.load_tokens(legacy)
        assert out == {"access_token": "legacy-tok"}
        crits = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert len(crits) == 1
        assert "PLAINTEXT" in crits[0].getMessage()
        # second read: no second CRITICAL
        caplog.clear()
        oauth_tokens.load_tokens(legacy)
        assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]


def test_legacy_gmail_file_migrates_and_is_deleted(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    legacy = oauth_tokens.GMAIL_TOKEN_PATH
    legacy.write_text(json.dumps({"refresh_token": "legacy-R"}))
    oauth_tokens._migrate_legacy_token_file()
    assert not legacy.exists()
    canonical = oauth_tokens.TOKEN_PATH
    assert canonical.exists()
    # re-encrypted on the way over
    assert canonical.read_bytes().startswith(b"FERNET1:")
    assert oauth_tokens.load_tokens() == {"refresh_token": "legacy-R"}


# ---- DELETE /api/email/disconnect ----

class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


def test_disconnect_revokes_and_clears(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    oauth_tokens.save_tokens({"access_token": "at-1", "refresh_token": "rt-1"})
    calls: list[dict] = []

    def fake_post(url, **kw):
        calls.append({"url": url, **kw})
        return _FakeResp(200)

    monkeypatch.setattr(email_router.httpx, "post", fake_post)
    r = client.delete("/api/email/disconnect")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["revoked"] is True
    assert data["files_removed"] == 1
    assert not oauth_tokens.TOKEN_PATH.exists()
    # revoke got the refresh token, with the 5s timeout
    assert len(calls) == 1
    assert calls[0]["url"] == email_router.GOOGLE_REVOKE_URL
    assert calls[0]["data"] == {"token": "rt-1"}
    assert calls[0]["timeout"] == 5


def test_disconnect_survives_revoke_failure(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)
    oauth_tokens.save_tokens({"access_token": "at-2"})

    def boom(url, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(email_router.httpx, "post", boom)
    r = client.delete("/api/email/disconnect")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["revoked"] is False
    assert data["files_removed"] == 1
    assert not oauth_tokens.TOKEN_PATH.exists()


def test_disconnect_with_no_tokens(monkeypatch, tmp_path):
    _isolate_store(monkeypatch, tmp_path)

    def fail(url, **kw):  # pragma: no cover - must not be called
        raise AssertionError("revoke must not be called without tokens")

    monkeypatch.setattr(email_router.httpx, "post", fail)
    r = client.delete("/api/email/disconnect")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["revoked"] is False
    assert data["files_removed"] == 0


# ---- secrets hygiene: .env permissions ----

def test_load_env_file_tightens_permissions(monkeypatch, tmp_path, caplog):
    env = tmp_path / ".env"
    env.write_text("JHH_TEST_PERM_KEY=hello\n")
    os.chmod(env, 0o644)
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.delenv("JHH_TEST_PERM_KEY", raising=False)
    try:
        with caplog.at_level(logging.WARNING, logger="jhh.config"):
            config._load_env_file()
        assert _mode(env) == 0o600
        assert any("tightened to 0600" in r.getMessage() for r in caplog.records)
        # the file still parsed normally
        assert os.environ.get("JHH_TEST_PERM_KEY") == "hello"
    finally:
        os.environ.pop("JHH_TEST_PERM_KEY", None)


def test_load_env_file_leaves_tight_permissions_alone(monkeypatch, tmp_path, caplog):
    env = tmp_path / ".env"
    env.write_text("# just a comment\n")
    os.chmod(env, 0o600)
    monkeypatch.setattr(config, "ENV_FILE", env)
    with caplog.at_level(logging.WARNING, logger="jhh.config"):
        config._load_env_file()
    assert _mode(env) == 0o600
    assert not any("tightened" in r.getMessage() for r in caplog.records)


def test_write_env_creates_file_with_0600(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(settings_router, "ENV_FILE", env)
    settings_router._write_env({"JHH_TEST_WRITE_KEY": "v1"})
    assert env.exists()
    assert _mode(env) == 0o600
    assert "JHH_TEST_WRITE_KEY=v1" in env.read_text()
    # rewrite path (existing file) stays 0600 and preserves other lines
    settings_router._write_env({"JHH_OTHER_KEY": "v2"})
    assert _mode(env) == 0o600
    text = env.read_text()
    assert "JHH_TEST_WRITE_KEY=v1" in text and "JHH_OTHER_KEY=v2" in text
    # no leftover tmp file
    assert not (tmp_path / ".env.tmp").exists()


# ---- gmail log sanitization ----

def test_sanitize_token_text_strips_values():
    tokens = {"access_token": "ya29.SECRET-AT", "refresh_token": "1//SECRET-RT"}
    msg = (
        'HTTP 400 for {"access_token": "ya29.SECRET-AT", '
        '"refresh_token": "1//SECRET-RT"} header Authorization: Bearer ya29.SECRET-AT '
        "and query refresh_token=1//SECRET-RT"
    )
    out = gmail._sanitize_token_text(msg, tokens)
    assert "ya29.SECRET-AT" not in out
    assert "1//SECRET-RT" not in out
    assert "[REDACTED]" in out


def test_refresh_failure_log_never_contains_token(monkeypatch, caplog):
    monkeypatch.setattr(gmail.settings, "google_client_id", "cid")
    monkeypatch.setattr(gmail.settings, "google_client_secret", "csecret")
    tokens = {"refresh_token": "1//VERY-SECRET-RT", "expires_at": 1.0}

    def boom(*a, **kw):
        raise RuntimeError("refresh blew up: refresh_token=1//VERY-SECRET-RT")

    monkeypatch.setattr(gmail.httpx, "post", boom)
    with caplog.at_level(logging.WARNING, logger="jhh.integrations.gmail"):
        out = gmail._refresh_if_needed(dict(tokens))
    assert out["refresh_token"] == "1//VERY-SECRET-RT"  # tokens returned unchanged
    assert "1//VERY-SECRET-RT" not in caplog.text
    assert "[REDACTED]" in caplog.text
