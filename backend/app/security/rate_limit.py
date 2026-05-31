"""Rate-limiting helpers, backed by slowapi when available.

If ``slowapi`` isn't installed the module exposes a no-op decorator and a
no-op limiter so endpoints keep working — Job Hunt Hacker prefers degraded
service over a crashed server.

Usage from a router::

    from ..security.rate_limit import rate_limit

    @router.post("/search")
    @rate_limit("10/minute")
    def post_search(request: Request, body: ...): ...

The ``request: Request`` parameter is required by slowapi so it can extract
the client IP via ``slowapi.util.get_remote_address``. Endpoints that don't
already take a ``Request`` need it added.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

log = logging.getLogger("jhh.security.rate_limit")


def _per_minute_default() -> int:
    raw = os.environ.get("JHH_RATE_LIMIT_PER_MIN", "10").strip()
    try:
        v = int(raw)
        if v < 1:
            return 10
        return v
    except ValueError:
        return 10


# ---------------------------------------------------------------------------
# slowapi probe — defensive import so missing dep doesn't break the server.
# ---------------------------------------------------------------------------
try:
    from slowapi import Limiter  # type: ignore
    from slowapi.errors import RateLimitExceeded  # type: ignore
    from slowapi.util import get_remote_address  # type: ignore
    from starlette.requests import Request  # type: ignore
    from starlette.responses import JSONResponse  # type: ignore
    _HAS_SLOWAPI = True
except Exception as _exc:  # noqa: BLE001
    log.info("slowapi not available — rate limiting disabled (%s)", _exc)
    Limiter = None  # type: ignore[assignment]
    RateLimitExceeded = Exception  # type: ignore[assignment,misc]
    get_remote_address = None  # type: ignore[assignment]
    Request = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    _HAS_SLOWAPI = False


# Singleton limiter — keyed by client IP, default = JHH_RATE_LIMIT_PER_MIN/min.
_limiter: Any = None


def get_limiter() -> Any | None:
    """Return the shared limiter instance, or ``None`` if slowapi missing."""
    global _limiter
    if not _HAS_SLOWAPI:
        return None
    if _limiter is None:
        _limiter = Limiter(  # type: ignore[misc]
            key_func=get_remote_address,
            default_limits=[f"{_per_minute_default()}/minute"],
        )
    return _limiter


def _resolve_rate(spec: str) -> str:
    """Allow the env var to override caller-specified per-minute defaults.

    If the caller wrote ``"10/minute"`` and the operator set
    ``JHH_RATE_LIMIT_PER_MIN=60``, we honour the operator. Any other unit
    (``"5/second"``, ``"100/hour"``) is passed through verbatim.
    """
    s = (spec or "").strip()
    if s.endswith("/minute"):
        return f"{_per_minute_default()}/minute"
    return s


def rate_limit(spec: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator wrapper that becomes a no-op when slowapi is unavailable.

    Also tolerates direct in-process calls that don't carry a ``Request``
    (autopilot invokes ``profile_router.infer_profile`` like a plain async
    function): if no Request is found in args/kwargs we skip the rate-limit
    check and call the wrapped function directly. The HTTP path still goes
    through slowapi because FastAPI always injects a Request there.
    """
    if not _HAS_SLOWAPI:
        def _noop(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn
        return _noop
    limiter = get_limiter()
    slow_decorator = limiter.limit(_resolve_rate(spec))  # type: ignore[union-attr]

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        slow_wrapped = slow_decorator(fn)

        def _has_request(args: tuple, kwargs: dict) -> bool:
            if "request" in kwargs and isinstance(kwargs["request"], Request):  # type: ignore[arg-type]
                return True
            for a in args:
                if isinstance(a, Request):  # type: ignore[arg-type]
                    return True
            return False

        import asyncio
        import functools

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def _async_inner(*args: Any, **kwargs: Any) -> Any:
                if not _has_request(args, kwargs):
                    return await fn(*args, **kwargs)
                return await slow_wrapped(*args, **kwargs)
            return _async_inner

        @functools.wraps(fn)
        def _sync_inner(*args: Any, **kwargs: Any) -> Any:
            if not _has_request(args, kwargs):
                return fn(*args, **kwargs)
            return slow_wrapped(*args, **kwargs)
        return _sync_inner

    return _wrap


def install_rate_limit_handler(app: Any) -> None:
    """Wire the limiter onto a FastAPI app + register a friendly 429 handler.

    Safe to call multiple times; no-op when slowapi is missing.
    """
    if not _HAS_SLOWAPI:
        return
    limiter = get_limiter()
    app.state.limiter = limiter

    async def _on_rate_limit_exceeded(request: Any, exc: Any) -> Any:  # noqa: ANN401
        per_min = _per_minute_default()
        detail = (
            f"rate limited ({per_min}/minute); "
            "slow down or set JHH_RATE_LIMIT_PER_MIN higher"
        )
        return JSONResponse(  # type: ignore[misc]
            status_code=429,
            content={"ok": False, "detail": detail},
        )

    app.add_exception_handler(RateLimitExceeded, _on_rate_limit_exceeded)
