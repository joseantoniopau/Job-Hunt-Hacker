"""Tiny Slack incoming-webhook client.

Surface:

  * ``is_configured()`` — True iff ``SLACK_WEBHOOK_URL`` env var is set.
  * ``post(text, blocks=None)`` — POST to the webhook, returns bool.
    Never raises — failures are logged and surfaced as False.

There is intentionally no SDK dep here. Slack incoming webhooks accept a
plain JSON POST, so a single ``httpx.post`` (or ``urllib`` as a
fall-back) is enough for the daily digest + alert use cases.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

log = logging.getLogger("jhh.integrations.slack")


def _webhook_url() -> str:
    return (os.environ.get("SLACK_WEBHOOK_URL") or "").strip()


def is_configured() -> bool:
    url = _webhook_url()
    return bool(url and url.startswith(("http://", "https://")))


def _build_payload(text: str, blocks: Optional[list] = None) -> dict:
    payload: dict[str, Any] = {"text": text or ""}
    if blocks:
        # Slack rejects non-list blocks; coerce defensively.
        if isinstance(blocks, list):
            payload["blocks"] = blocks
        else:
            log.debug("slack.post: ignoring non-list blocks (%r)", type(blocks).__name__)
    return payload


def _post_httpx(url: str, payload: dict) -> bool:
    try:
        import httpx  # type: ignore
    except Exception:  # pragma: no cover — httpx is in requirements.txt
        return False
    try:
        resp = httpx.post(url, json=payload, timeout=10.0)
        ok = 200 <= resp.status_code < 300
        if not ok:
            log.warning("slack webhook returned %s: %s", resp.status_code, resp.text[:200])
        return ok
    except Exception as exc:  # noqa: BLE001
        log.warning("slack webhook httpx error: %s", exc)
        return False


def _post_urllib(url: str, payload: dict) -> bool:
    try:
        from urllib import request as _urlreq
        body = json.dumps(payload).encode("utf-8")
        req = _urlreq.Request(url, data=body,
                              headers={"Content-Type": "application/json"})
        with _urlreq.urlopen(req, timeout=10) as resp:
            status = getattr(resp, "status", 200)
            return 200 <= int(status) < 300
    except Exception as exc:  # noqa: BLE001
        log.warning("slack webhook urllib error: %s", exc)
        return False


def post(text: str, blocks: list | None = None) -> bool:
    """POST a message to the configured Slack webhook.

    Returns True on success, False on any failure (including missing
    config). Never raises.
    """
    url = _webhook_url()
    if not url:
        log.debug("slack.post called but SLACK_WEBHOOK_URL is not set")
        return False
    payload = _build_payload(text, blocks)
    # Prefer httpx (project dep), fall back to stdlib urllib.
    if _post_httpx(url, payload):
        return True
    return _post_urllib(url, payload)


__all__ = ["is_configured", "post"]
