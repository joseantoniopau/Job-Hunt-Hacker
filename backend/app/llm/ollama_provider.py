"""Ollama native chat API provider.

Endpoint: {OLLAMA_BASE_URL}/api/chat — returns `{message: {content: ...}}`.
"""
from __future__ import annotations

import logging
import os

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

log = logging.getLogger("jhh.llm.ollama")

_DEFAULT_MODEL = "llama3"
# Local models can be slow, especially 70B-class quantizations doing
# multi-thousand-token JSON output (offer analysis, resume tailor,
# interview prep packets at max_tokens=3500). Default is 12 minutes;
# override via JHH_OLLAMA_TIMEOUT (seconds).
_DEFAULT_TIMEOUT = float(os.getenv("JHH_OLLAMA_TIMEOUT", "720.0"))


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self) -> None:
        self.base_url = (settings.ollama_base_url or "http://localhost:11434").rstrip("/")
        self.model = settings.llm_model or _DEFAULT_MODEL
        self.timeout = _DEFAULT_TIMEOUT

    def complete(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.3) -> str:
        return self.complete_with_status(
            system, user, max_tokens=max_tokens, temperature=temperature
        ).text

    def complete_with_status(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> LLMResult:
        import time as _t
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system or ""},
                {"role": "user", "content": user or ""},
            ],
            "stream": False,
            "options": {
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
            },
        }
        t0 = _t.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(url, json=payload)
                latency = int((_t.perf_counter() - t0) * 1000)
                if r.status_code >= 400:
                    log.warning("ollama %d: %s", r.status_code, r.text[:500])
                    status = STATUS_RATE_LIMITED if r.status_code == 429 else STATUS_ERROR
                    return LLMResult(text="", status=status, latency_ms=latency,
                                     error=f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                msg = data.get("message") or {}
                text = (msg.get("content") or "").strip()
                if not text:
                    return LLMResult(text="", status=STATUS_EMPTY, latency_ms=latency)
                return LLMResult(text=text, status=STATUS_OK, latency_ms=latency)
        except Exception as e:  # noqa: BLE001
            log.warning("ollama call failed: %s", e)
            return LLMResult(text="", status=STATUS_ERROR,
                             latency_ms=int((_t.perf_counter() - t0) * 1000),
                             error=f"{type(e).__name__}: {e}")
