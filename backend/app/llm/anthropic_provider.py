"""Anthropic Messages API provider.

Uses httpx directly to avoid pulling in the official SDK as a hard dep.
Endpoint: https://api.anthropic.com/v1/messages
Headers: x-api-key, anthropic-version: 2023-06-01, content-type: application/json
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings
from .base import LLMProvider

log = logging.getLogger("jhh.llm.anthropic")

_ENDPOINT = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_TIMEOUT = 60.0


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self) -> None:
        self.api_key = settings.anthropic_api_key
        self.model = settings.llm_model or _DEFAULT_MODEL
        self.timeout = _DEFAULT_TIMEOUT

    def complete(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.3) -> str:
        if not self.api_key:
            log.warning("anthropic provider invoked with no API key")
            return ""
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "system": system or "",
            "messages": [{"role": "user", "content": user or ""}],
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(_ENDPOINT, headers=headers, json=payload)
                if r.status_code >= 400:
                    log.warning("anthropic %d: %s", r.status_code, r.text[:500])
                    return ""
                data = r.json()
                content = data.get("content") or []
                if content and isinstance(content, list):
                    first = content[0]
                    if isinstance(first, dict):
                        return (first.get("text") or "").strip()
                return ""
        except Exception as e:  # noqa: BLE001
            log.warning("anthropic call failed: %s", e)
            return ""
