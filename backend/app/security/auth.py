"""Optional bearer-token auth middleware.

When env var ``JHH_AUTH_TOKEN`` is set, every request whose path begins with
``/api/`` must carry ``Authorization: Bearer <token>``. A small whitelist of
public paths (the static UI assets and the health endpoint) is exempt so the
UI can boot and external probes still work without a token.

When the env var is unset (or empty), the middleware is a complete no-op:
behavior matches the pre-hardening server exactly.
"""
from __future__ import annotations

import os
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


# Paths that must remain accessible without a token even when auth is on.
# - ``/``, ``/styles.css``, ``/app.js`` so the UI can serve itself.
# - ``/api/health`` so liveness probes and the install script can still ping.
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/",
    "/styles.css",
    "/app.js",
    "/api/health",
})


def get_auth_token() -> str:
    """Return the configured token, or empty string when auth is disabled.

    Re-read from the environment every call so tests can flip the env via
    ``monkeypatch.setenv`` without reloading the module.
    """
    return (os.environ.get("JHH_AUTH_TOKEN") or "").strip()


def _extract_bearer(header: str | None) -> str:
    if not header:
        return ""
    parts = header.strip().split(None, 1)
    if len(parts) != 2:
        return ""
    scheme, value = parts
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """ASGI middleware enforcing ``Authorization: Bearer <token>`` on
    ``/api/*`` routes when ``JHH_AUTH_TOKEN`` is set.

    Implemented as a regular middleware (not a dependency) so it can also
    gate routers that don't go through FastAPI's Depends machinery.
    """

    def __init__(self, app, public_paths: Iterable[str] | None = None) -> None:
        super().__init__(app)
        self._public = frozenset(public_paths) if public_paths else PUBLIC_PATHS

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        token = get_auth_token()
        # No token configured → middleware is a no-op.
        if not token:
            return await call_next(request)

        path = request.url.path
        # Public paths bypass auth even when the token is configured.
        if path in self._public:
            return await call_next(request)
        # Anything outside /api/ is not gated (e.g. /docs, /openapi.json,
        # static assets we forgot to whitelist). The UI is the only browser
        # surface and it's whitelisted above; the API is the real gate.
        if not path.startswith("/api/"):
            return await call_next(request)

        provided = _extract_bearer(request.headers.get("authorization"))
        if not provided or provided != token:
            return JSONResponse(
                status_code=401,
                content={"ok": False, "detail": "auth required"},
            )
        return await call_next(request)
