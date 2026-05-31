"""Structured JSON logging with request-ID injection.

Env knobs:
  JHH_LOG_FORMAT=json|text   (default: json in production, text on a TTY)
  JHH_LOG_LEVEL=INFO|DEBUG|... (default INFO)

Behaviour:
  - JSON mode emits one object per line: ts/level/logger/msg + extras.
  - Text mode keeps the legacy `<asctime> <level> <logger> | <msg>` layout
    so developers running `uvicorn` in a terminal still get readable lines.
  - When `python-json-logger` is unavailable the formatter quietly degrades
    to text mode — the server must keep booting on minimal dependencies.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from .request_id import get_request_id

# python-json-logger 3+ moved the module to `pythonjsonlogger.json`. Try the
# new path first and fall back to the legacy import so we keep working on
# the version pinned in requirements-dev.txt (2.x) too.
try:
    from pythonjsonlogger.json import JsonFormatter as _JsonFormatter  # type: ignore
    _HAS_JSONLOGGER = True
except Exception:  # noqa: BLE001
    try:
        from pythonjsonlogger.jsonlogger import JsonFormatter as _JsonFormatter  # type: ignore
        _HAS_JSONLOGGER = True
    except Exception:  # noqa: BLE001
        _JsonFormatter = None  # type: ignore
        _HAS_JSONLOGGER = False


_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s | %(message)s"


class _RequestIDFilter(logging.Filter):
    """Attach the current request ID (if any) to every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        rid = get_request_id()
        # Always set so JSON formatter sees a stable key (None becomes null
        # in JSON, which still beats an absent field for ingestion tools).
        record.request_id = rid
        return True


def _decide_format(explicit: Optional[str] = None) -> str:
    """Resolve the effective log format.

    Precedence:
      1. explicit arg (used by tests)
      2. JHH_LOG_FORMAT env
      3. tty → text, else json
    """
    if explicit:
        return explicit.lower()
    env = (os.environ.get("JHH_LOG_FORMAT") or "").strip().lower()
    if env in ("json", "text"):
        return env
    try:
        if sys.stdout.isatty():
            return "text"
    except Exception:
        pass
    return "json"


def _make_json_formatter() -> logging.Formatter:
    if not _HAS_JSONLOGGER:
        return logging.Formatter(_TEXT_FORMAT)
    # `%(...)s` placeholders are the canonical way to tell python-json-logger
    # which built-in LogRecord attributes to include. Any `extra=` kwargs on
    # the call site get merged in automatically.
    fmt = (
        "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s "
        "%(pathname)s %(lineno)d"
    )
    formatter = _JsonFormatter(
        fmt,
        rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
        json_ensure_ascii=False,
    )
    return formatter


def configure_logging(level: Optional[str] = None, fmt: Optional[str] = None) -> str:
    """Install the JHH log handler. Returns the chosen format ('json'|'text').

    Safe to call multiple times — replaces existing handlers on the root
    logger so reconfiguration in tests/REPL works as expected.
    """
    effective_level = (level or os.environ.get("JHH_LOG_LEVEL") or "INFO").upper()
    effective_fmt = _decide_format(fmt)

    handler = logging.StreamHandler(stream=sys.stdout)
    if effective_fmt == "json":
        handler.setFormatter(_make_json_formatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    handler.addFilter(_RequestIDFilter())

    root = logging.getLogger()
    # Wipe pre-existing handlers (uvicorn / pytest add their own) so we
    # don't double-emit every line.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    try:
        root.setLevel(getattr(logging, effective_level))
    except AttributeError:
        root.setLevel(logging.INFO)

    return effective_fmt
