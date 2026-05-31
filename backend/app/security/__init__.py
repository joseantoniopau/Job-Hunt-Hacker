"""Security hardening: optional bearer-token auth, rate limiting, upload
validation. Each submodule degrades to a no-op if its optional deps are
missing — Job Hunt Hacker keeps running.
"""
from __future__ import annotations

from .auth import BearerTokenMiddleware, get_auth_token
from .rate_limit import get_limiter, rate_limit
from .uploads import validate_upload, get_max_upload_bytes

__all__ = [
    "BearerTokenMiddleware",
    "get_auth_token",
    "get_limiter",
    "rate_limit",
    "validate_upload",
    "get_max_upload_bytes",
]
