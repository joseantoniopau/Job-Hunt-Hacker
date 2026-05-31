"""Anthropic Messages API provider.

Uses httpx directly to avoid pulling in the official SDK as a hard dep.
Endpoint: https://api.anthropic.com/v1/messages
Headers: x-api-key, anthropic-version: 2023-06-01, content-type: application/json
"""
from __future__ import annotations

import logging
import time

import httpx

from ..config import settings
from .base import (
    LLMProvider,
    LLMResult,
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_RATE_LIMITED,
)

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
        # Backward-compat path: delegate to status-aware impl and return only the text.
        return self.complete_with_status(system, user, max_tokens=max_tokens, temperature=temperature).text

    def complete_with_status(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> LLMResult:
        t0 = time.perf_counter()
        if not self.api_key:
            log.warning("anthropic provider invoked with no API key")
            return LLMResult(
                text="",
                status=STATUS_ERROR,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                error="missing ANTHROPIC_API_KEY",
            )
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
                latency = int((time.perf_counter() - t0) * 1000)
                if r.status_code == 429:
                    detail = r.text[:500]
                    log.warning("anthropic 429: %s", detail)
                    return LLMResult(text="", status=STATUS_RATE_LIMITED, latency_ms=latency, error=detail)
                if 500 <= r.status_code < 600:
                    detail = r.text[:500]
                    log.warning("anthropic %d: %s", r.status_code, detail)
                    return LLMResult(text="", status=STATUS_ERROR, latency_ms=latency, error=f"http {r.status_code}: {detail}")
                if r.status_code >= 400:
                    detail = r.text[:500]
                    log.warning("anthropic %d: %s", r.status_code, detail)
                    return LLMResult(text="", status=STATUS_ERROR, latency_ms=latency, error=f"http {r.status_code}: {detail}")
                data = r.json()
                content = data.get("content") or []
                if content and isinstance(content, list):
                    first = content[0]
                    if isinstance(first, dict):
                        text = (first.get("text") or "").strip()
                        if text:
                            return LLMResult(text=text, status=STATUS_OK, latency_ms=latency)
                        return LLMResult(text="", status=STATUS_EMPTY, latency_ms=latency)
                return LLMResult(text="", status=STATUS_EMPTY, latency_ms=latency)
        except Exception as e:  # noqa: BLE001
            latency = int((time.perf_counter() - t0) * 1000)
            log.warning("anthropic call failed: %s", e)
            return LLMResult(text="", status=STATUS_ERROR, latency_ms=latency, error=f"{type(e).__name__}: {e}")
