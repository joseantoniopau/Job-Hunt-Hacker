"""Upload validation: size cap + extension whitelist + optional MIME check.

The size cap (``JHH_MAX_UPLOAD_MB``, default 10) is enforced two ways:

1. If a ``Content-Length`` header is present we reject early with 413 before
   reading bytes — saves memory on attacker-supplied huge uploads.
2. After the handler has read the bytes (parsers often need the full body)
   the caller can re-call :func:`validate_upload` with the materialised
   bytes and we re-check. The function accepts either path.

Extension validation is case-insensitive. MIME validation via ``python-magic``
is best-effort: when the library isn't installed (it ships with libmagic as
a system dep) we silently skip MIME and still enforce extension + size.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

from fastapi import HTTPException, UploadFile

log = logging.getLogger("jhh.security.uploads")


# Best-effort python-magic probe.
try:
    import magic  # type: ignore
    _HAS_MAGIC = True
except Exception as _exc:  # noqa: BLE001
    log.info("python-magic not available — MIME validation disabled (%s)", _exc)
    magic = None  # type: ignore[assignment]
    _HAS_MAGIC = False


def get_max_upload_bytes() -> int:
    """Return the configured cap, re-read on every call so tests can flip it."""
    raw = os.environ.get("JHH_MAX_UPLOAD_MB", "10").strip()
    try:
        mb = int(raw)
        if mb < 1:
            mb = 10
    except ValueError:
        mb = 10
    return mb * 1024 * 1024


def _ext_of(name: str | None) -> str:
    if not name:
        return ""
    return Path(name).suffix.lstrip(".").lower()


def _normalize(exts: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for e in exts:
        if not e:
            continue
        out.add(e.lstrip(".").lower())
    return out


# Rough MIME-by-extension whitelist for the file types we accept. Used to
# reject obvious mismatches (e.g. a ``.txt`` file whose actual content is
# a Windows PE binary) without blowing up on, say, ``.md`` which libmagic
# usually reports as ``text/plain``.
_ACCEPTED_MIMES_BY_EXT: dict[str, set[str]] = {
    "pdf": {"application/pdf"},
    "docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",  # docx is technically a zip — libmagic often reports this
        "application/octet-stream",
    },
    "doc": {"application/msword", "application/octet-stream"},
    "md": {"text/plain", "text/markdown", "text/x-markdown"},
    "txt": {"text/plain"},
    "rtf": {"application/rtf", "text/rtf"},
    "html": {"text/html", "text/plain"},
    "htm": {"text/html", "text/plain"},
}


def _content_length(upload: UploadFile) -> int | None:
    """Best-effort: pull Content-Length off the underlying request headers."""
    headers = getattr(upload, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("content-length")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def validate_upload(
    upload: UploadFile,
    allowed_exts: Iterable[str],
    *,
    raw_bytes: bytes | None = None,
) -> None:
    """Validate an UploadFile against the security policy.

    Raises ``HTTPException`` on failure:
      * 413 — file too large (Content-Length header OR materialised bytes)
      * 415 — extension not in whitelist OR MIME mismatch

    Caller pattern::

        validate_upload(file, ("pdf", "docx"))      # cheap header-only check
        raw = await file.read()
        validate_upload(file, ("pdf", "docx"), raw_bytes=raw)  # safety-net
    """
    cap = get_max_upload_bytes()

    # ---- size: header check (cheap, runs before the body is consumed) ----
    cl = _content_length(upload)
    if cl is not None and cl > cap:
        raise HTTPException(413, "file too large")

    # ---- size: byte check (safety net for parsers that buffer the whole file)
    if raw_bytes is not None and len(raw_bytes) > cap:
        raise HTTPException(413, "file too large")

    # ---- extension whitelist ----
    allowed = _normalize(allowed_exts)
    if allowed:
        ext = _ext_of(upload.filename)
        if not ext or ext not in allowed:
            raise HTTPException(415, "unsupported file type")

    # ---- optional MIME check ----
    if _HAS_MAGIC and raw_bytes:
        ext = _ext_of(upload.filename)
        accepted = _ACCEPTED_MIMES_BY_EXT.get(ext)
        if accepted:
            try:
                detected = magic.from_buffer(raw_bytes[:4096], mime=True)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001
                log.debug("MIME probe failed for %r: %s", upload.filename, exc)
                detected = None
            if detected and detected not in accepted:
                # Don't be paranoid: text-like formats fingerprint as
                # ``text/plain`` for almost anything. Only reject when we
                # really do have a confident mismatch.
                if not detected.startswith("text/"):
                    raise HTTPException(415, "file content does not match extension")
