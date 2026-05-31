"""Self-update check: compares the installed app version against the latest
GitHub release. Wrapped in defensive try/except so a flaky network never
breaks the dashboard, and cached for 24h to avoid hammering the GitHub API
on every page load.

Endpoint:
    GET /api/updates/check
        -> {current, latest, update_available, release_url, cached, error}
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter

log = logging.getLogger("jhh.updates")

router = APIRouter(prefix="/api/updates", tags=["updates"])

GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/joseantoniopau/Job-Hunt-Hacker/releases/latest"
)
CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h

# Module-level cache. Single-process, threadsafe-enough for our needs.
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {
    "ts": 0.0,
    "payload": None,  # last successful response payload
}


def _current_version() -> str:
    """Read app.version off the FastAPI app object. Defensive — falls back
    to '0.0.0' if main hasn't initialized yet (e.g. during isolated tests).
    """
    try:
        from ..main import app  # local import to avoid circular
        return str(getattr(app, "version", "0.0.0") or "0.0.0")
    except Exception:  # noqa: BLE001
        return "0.0.0"


def _parse_version(v: str) -> tuple[int, ...]:
    """Liberal semver parse. Strips a leading 'v' and any pre-release/build
    suffix (anything after '-' or '+'). Non-numeric segments become 0.
    """
    if not v:
        return (0,)
    v = v.strip().lstrip("vV")
    for sep in ("-", "+"):
        if sep in v:
            v = v.split(sep, 1)[0]
    parts: list[int] = []
    for chunk in v.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _parse_version(latest) > _parse_version(current)
    except Exception:  # noqa: BLE001
        return False


def _fetch_latest_release(timeout: float = 5.0) -> Optional[dict[str, Any]]:
    """Hit GitHub Releases API. Returns parsed JSON or None on failure."""
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(
                GITHUB_RELEASES_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "job-hunt-hacker-update-check",
                },
            )
            if r.status_code != 200:
                log.info("github releases returned %d", r.status_code)
                return None
            return r.json()
    except Exception as exc:  # noqa: BLE001
        log.info("github releases fetch failed: %s", exc)
        return None


def _build_payload(release: Optional[dict[str, Any]], current: str) -> dict[str, Any]:
    if not release:
        return {
            "current": current,
            "latest": None,
            "update_available": False,
            "release_url": "",
            "error": "could not reach github releases api",
        }
    latest_raw = str(release.get("tag_name") or release.get("name") or "").strip()
    latest = latest_raw.lstrip("vV") or None
    url = str(release.get("html_url") or "")
    available = bool(latest and _is_newer(latest, current))
    return {
        "current": current,
        "latest": latest,
        "update_available": available,
        "release_url": url,
        "error": None,
    }


def check_for_updates(force: bool = False) -> dict[str, Any]:
    """Public helper — used by both the HTTP route and the CLI script."""
    current = _current_version()
    now = time.time()
    with _cache_lock:
        cached_payload = _cache.get("payload")
        cached_ts = float(_cache.get("ts") or 0.0)
        fresh = cached_payload is not None and (now - cached_ts) < CACHE_TTL_SECONDS

    if fresh and not force:
        out = dict(cached_payload)  # type: ignore[arg-type]
        # Refresh the 'current' field in case the app was upgraded in-place
        # since the cached payload was built.
        out["current"] = current
        out["update_available"] = bool(
            out.get("latest") and _is_newer(str(out["latest"]), current)
        )
        out["cached"] = True
        return out

    release = _fetch_latest_release()
    payload = _build_payload(release, current)
    payload["cached"] = False

    # Only cache successful lookups; transient failures shouldn't lock us
    # out of GitHub for 24h.
    if release is not None:
        with _cache_lock:
            _cache["ts"] = now
            _cache["payload"] = {k: v for k, v in payload.items() if k != "cached"}

    return payload


@router.get("/check")
def check() -> dict[str, Any]:
    return {"ok": True, "data": check_for_updates()}
