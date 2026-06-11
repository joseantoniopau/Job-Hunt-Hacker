"""Retry helper for adapter HTTP calls.

`wrap_with_retry(func, ...)` returns a callable that retries `func` only on
transient failures: httpx network errors and HTTP 429 / 5xx responses. It
NEVER retries on 4xx other than 429 -- those are caller errors, not
something a backoff will fix.

If tenacity is unavailable we degrade to a passthrough so the adapter
pipeline still works (just without retries).
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Callable

import httpx

log = logging.getLogger("jhh.sources.retry")


def _import_tenacity():
    """Indirect import so tests can mock 'tenacity missing'."""
    try:
        return importlib.import_module("tenacity")
    except Exception:
        return None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.NetworkError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = getattr(exc.response, "status_code", 0)
        return code == 429 or (500 <= code < 600)
    return False


def wrap_with_retry(
    func: Callable[..., Any],
    *,
    max_attempts: int = 3,
    min_wait: float = 2.0,
    max_wait: float = 30.0,
) -> Callable[..., Any]:
    """Wrap `func` with exponential-backoff retry on transient HTTP errors.

    When tenacity is missing, returns `func` unmodified so callers don't
    branch -- the pipeline keeps working, just without retries.
    """
    tenacity = _import_tenacity()
    if tenacity is None:
        log.debug("tenacity unavailable; retry disabled for %s", getattr(func, "__name__", "fn"))
        return func

    fn_name = getattr(func, "__name__", "fn")

    def _before_sleep(retry_state: Any) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        log.warning("retry %s attempt %d after transient error: %s",
                    fn_name, retry_state.attempt_number,
                    f"{type(exc).__name__}: {exc}" if exc else "unknown")

    retry_if = tenacity.retry_if_exception(_is_retryable)
    decorator = tenacity.retry(
        retry=retry_if,
        stop=tenacity.stop_after_attempt(int(max_attempts)),
        wait=tenacity.wait_exponential(multiplier=1, min=float(min_wait), max=float(max_wait)),
        before_sleep=_before_sleep,
        reraise=True,
    )
    return decorator(func)
