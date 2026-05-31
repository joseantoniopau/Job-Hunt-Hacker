"""Per-request UUID4 correlation IDs.

The ID is sourced from the inbound `X-Request-ID` header when present (so
a reverse proxy or load tester can correlate against its own log), or
generated freshly. Either way it ends up on:

  - `request.state.request_id` for downstream handlers
  - the response `X-Request-ID` header so clients can echo it back
  - a `contextvars.ContextVar` so the JSON log formatter can inject it
    into every log line emitted while handling that request
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# Sentinel "no active request" value so callers (e.g. log formatter) can
# cheaply distinguish background tasks from HTTP requests.
request_id_var: ContextVar[Optional[str]] = ContextVar("jhh_request_id", default=None)


def get_request_id() -> Optional[str]:
    """Read the active request's correlation ID, or None outside a request."""
    return request_id_var.get()


def _is_valid_uuid_like(value: str) -> bool:
    # Accept any non-empty header value up to a sane length. We don't strictly
    # require a UUID — a load balancer may inject its own trace ID format.
    value = value.strip()
    return bool(value) and len(value) <= 200


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Generate or propagate `X-Request-ID` for every HTTP request."""

    header_name = "X-Request-ID"

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        incoming = request.headers.get(self.header_name, "")
        if incoming and _is_valid_uuid_like(incoming):
            rid = incoming.strip()
        else:
            rid = str(uuid.uuid4())

        request.state.request_id = rid
        token = request_id_var.set(rid)
        try:
            response: Response = await call_next(request)
        finally:
            # Always reset the context var so the slot can be reused by the
            # next coroutine on this worker thread.
            request_id_var.reset(token)

        response.headers[self.header_name] = rid
        return response
