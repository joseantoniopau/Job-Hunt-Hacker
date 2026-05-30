"""Tiny shared OAuth token store. Encrypts with Fernet when cryptography
is installed; otherwise falls back to plain JSON at mode 0600.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

from ..config import settings

log = logging.getLogger("jhh.oauth_tokens")

TOKEN_PATH = settings.data_dir / "oauth_tokens.json"
GMAIL_TOKEN_PATH = settings.data_dir / "gmail_tokens.json"  # legacy/per-spec name
KEY_PATH = settings.data_dir / ".oauth_fernet.key"

try:
    from cryptography.fernet import Fernet  # type: ignore
    _HAS_FERNET = True
except Exception:  # noqa: BLE001
    Fernet = None  # type: ignore
    _HAS_FERNET = False


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
            KEY_PATH.write_bytes(key)
            try:
                os.chmod(KEY_PATH, stat.S_IRUSR | stat.S_IWUSR)
            except Exception:
                pass
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


def save_tokens(tokens: dict, path: Path | None = None) -> None:
    path = path or TOKEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(tokens, default=str).encode("utf-8")
    f = _get_fernet()
    if f is not None:
        try:
            blob = f.encrypt(raw)
            path.write_bytes(b"FERNET1:" + blob)
            _chmod600(path)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("fernet encrypt failed, falling back to plain: %s", exc)
    path.write_bytes(raw)
    _chmod600(path)


def load_tokens(path: Path | None = None) -> dict:
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
        return json.loads(data.decode("utf-8"))
    except Exception:
        return {}


def clear_tokens(path: Path | None = None) -> None:
    p = path or TOKEN_PATH
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass
