"""GET /metrics — Prometheus text exposition.

Returns the global registry contents; the actual counter/histogram
definitions live in `backend.app.middleware.metrics` so they can be
imported and incremented from anywhere without circular deps.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from ..middleware import metrics as _metrics

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    body, content_type = _metrics.render_latest()
    return Response(content=body, media_type=content_type)
