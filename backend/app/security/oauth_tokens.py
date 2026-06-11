"""Hardened shared OAuth token store.

Tokens are ALWAYS encrypted at rest with Fernet. If the `cryptography`
package is unavailable, `save_tokens()` refuses to write (raises
RuntimeError — callers surface it) rather than fall back to plaintext.
`load_tokens()` can still read a legacy plaintext file, but logs a
CRITICAL warning once per process so the user knows to re-authorize.

On import, a legacy `gmail_tokens.json` (the old per-spec name) is
migrated to the canonical `oauth_tokens.json` and deleted, re-encrypting
it on the way over when possible.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

from ..config import settings
from ..db import audit

log = logging.getLogger("jhh.security.oauth_tokens")

TOKEN_PATH = settings.data_dir / "oauth_tokens.json"
GMAIL_TOKEN_PATH = settings.data_dir / "gmail_tokens.json"  # legacy/per-spec name
KEY_PATH = settings.data_dir / ".oauth_fernet.key"

try:
    from cryptography.fernet import Fernet  # type: ignore
    _HAS_FERNET = True
except Exception:  # noqa: BLE001
    Fernet = None  # type: ignore
    _HAS_FERNET = False

# One-shot flag so the plaintext warning doesn't spam every sweep.
_plaintext_warned = False


def _get_fernet() -> Optional["Fernet"]:
    if not _HAS_FERNET:
        return None
    if KEY_PATH.exists():
        try:
            key = KEY_PATH.read_bytes().strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read fernet key: %s", exc)
            return None
    else:
        key = Fernet.generate_key()
        try:
            _write_private(KEY_PATH, key)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write fernet key: %s", exc)
            return None
    try:
        return Fernet(key)
    except Exception as exc:  # noqa: BLE001
        log.warning("fernet init failed: %s", exc)
        return None


def _chmod600(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _write_private(path: Path, data: bytes) -> None:
    """Write bytes to a file that is 0600 from the moment it exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # O_CREAT's mode only applies to NEW files; tighten pre-existing ones.
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def save_tokens(tokens: dict, path: Path | None = None) -> None:
    """Encrypt and persist tokens. Refuses to write plaintext: raises
    RuntimeError when Fernet is unavailable so callers surface the problem
    instead of silently leaving secrets on disk unprotected.
    """
    path = path or TOKEN_PATH
    f = _get_fernet()
    if f is None:
        raise RuntimeError(
            "refusing to save OAuth tokens unencrypted — the 'cryptography' "
            "package is missing or its key is unusable; run "
            "`pip install cryptography` and re-authorize"
        )
    raw = json.dumps(tokens, default=str).encode("utf-8")
    _write_private(path, b"FERNET1:" + f.encrypt(raw))
    try:
        # Never log/audit token VALUES — field names only.
        audit("oauth_tokens_save", "oauth_tokens", None,
              path=str(path), fields=sorted(tokens.keys()))
    except Exception:
        pass


def load_tokens(path: Path | None = None) -> dict:
    global _plaintext_warned
    path = path or TOKEN_PATH
    if not path.exists():
        # try legacy
        if path == TOKEN_PATH and GMAIL_TOKEN_PATH.exists():
            return load_tokens(GMAIL_TOKEN_PATH)
        return {}
    try:
        data = path.read_bytes()
    except Exception:
        return {}
    if data.startswith(b"FERNET1:"):
        f = _get_fernet()
        if f is None:
            log.warning("encrypted tokens but cryptography missing")
            return {}
        try:
            raw = f.decrypt(data[len(b"FERNET1:"):])
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("fernet decrypt failed: %s", exc)
            return {}
    try:
        parsed = json.loads(data.decode("utf-8"))
    except Exception:
        return {}
    if isinstance(parsed, dict) and parsed and not _plaintext_warned:
        _plaintext_warned = True
        log.critical(
            "OAuth tokens at %s are stored in PLAINTEXT (legacy file written "
            "before encryption was enforced). Install 'cryptography' and "
            "re-authorize so they are re-saved encrypted.",
            path,
        )
    return parsed if isinstance(parsed, dict) else {}


def clear_tokens(path: Path | None = None) -> int:
    """Delete stored token files. With no explicit path, removes both the
    canonical and the legacy file. Returns how many files were removed.
    """
    targets = [path] if path is not None else [TOKEN_PATH, GMAIL_TOKEN_PATH]
    removed = 0
    for p in targets:
        try:
            if p.exists():
                p.unlink()
                removed += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("could not remove token file %s: %s", p, exc)
    try:
        audit("oauth_tokens_clear", "oauth_tokens", None, removed=removed)
    except Exception:
        pass
    return removed


def _migrate_legacy_token_file() -> None:
    """One-time import/startup migration: GMAIL_TOKEN_PATH -> TOKEN_PATH.

    Re-encrypts plaintext legacy content when Fernet is available; otherwise
    moves the file as-is (load_tokens will raise the plaintext alarm). The
    legacy file is always removed afterwards.
    """
    try:
        if not GMAIL_TOKEN_PATH.exists():
            return
        if TOKEN_PATH.exists():
            GMAIL_TOKEN_PATH.unlink()
            log.warning(
                "removed stale legacy token file %s (canonical %s already exists)",
                GMAIL_TOKEN_PATH, TOKEN_PATH,
            )
            return
        data = GMAIL_TOKEN_PATH.read_bytes()
        if not data.startswith(b"FERNET1:") and _get_fernet() is not None:
            tokens = None
            try:
                tokens = json.loads(data.decode("utf-8"))
            except Exception:
                tokens = None
            if isinstance(tokens, dict) and tokens:
                save_tokens(tokens)
                GMAIL_TOKEN_PATH.unlink()
                log.warning(
                    "migrated legacy token file %s -> %s (re-encrypted)",
                    GMAIL_TOKEN_PATH, TOKEN_PATH,
                )
                return
        GMAIL_TOKEN_PATH.replace(TOKEN_PATH)
        _chmod600(TOKEN_PATH)
        log.warning("migrated legacy token file %s -> %s", GMAIL_TOKEN_PATH, TOKEN_PATH)
    except Exception as exc:  # noqa: BLE001
        log.warning("legacy token migration failed: %s", exc)


_migrate_legacy_token_file()
