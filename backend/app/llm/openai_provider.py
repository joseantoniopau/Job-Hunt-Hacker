"""OpenAI chat completions provider.

Also works with any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio,
LiteLLM, Together, etc.) by setting OPENAI_BASE_URL.
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

log = logging.getLogger("jhh.llm.openai")

_DEFAULT_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_TIMEOUT = 60.0


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self) -> None:
        self.api_key = settings.openai_api_key
        self.base_url = (settings.openai_base_url or _DEFAULT_BASE).rstrip("/")
        self.model = settings.llm_model or _DEFAULT_MODEL
        self.timeout = _DEFAULT_TIMEOUT

    def complete(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.3) -> str:
        return self.complete_with_status(system, user, max_tokens=max_tokens, temperature=temperature).text

    def complete_with_status(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> LLMResult:
        t0 = time.perf_counter()
        url = f"{self.base_url}/chat/completions"
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system or ""},
                {"role": "user", "content": user or ""},
            ],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(url, headers=headers, json=payload)
                latency = int((time.perf_counter() - t0) * 1000)
                if r.status_code == 429:
                    detail = r.text[:500]
                    log.warning("openai 429: %s", detail)
                    return LLMResult(text="", status=STATUS_RATE_LIMITED, latency_ms=latency, error=detail)
                if 500 <= r.status_code < 600:
                    detail = r.text[:500]
                    log.warning("openai %d: %s", r.status_code, detail)
                    return LLMResult(text="", status=STATUS_ERROR, latency_ms=latency, error=f"http {r.status_code}: {detail}")
                if r.status_code >= 400:
                    detail = r.text[:500]
                    log.warning("openai %d: %s", r.status_code, detail)
                    return LLMResult(text="", status=STATUS_ERROR, latency_ms=latency, error=f"http {r.status_code}: {detail}")
                data = r.json()
                choices = data.get("choices") or []
                if choices:
                    msg = (choices[0] or {}).get("message") or {}
                    text = (msg.get("content") or "").strip()
                    if text:
                        return LLMResult(text=text, status=STATUS_OK, latency_ms=latency)
                    return LLMResult(text="", status=STATUS_EMPTY, latency_ms=latency)
                return LLMResult(text="", status=STATUS_EMPTY, latency_ms=latency)
        except Exception as e:  # noqa: BLE001
            latency = int((time.perf_counter() - t0) * 1000)
            log.warning("openai call failed: %s", e)
            return LLMResult(text="", status=STATUS_ERROR, latency_ms=latency, error=f"{type(e).__name__}: {e}")
