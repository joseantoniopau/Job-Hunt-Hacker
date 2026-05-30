"""Ollama native chat API provider.

Endpoint: {OLLAMA_BASE_URL}/api/chat — returns `{message: {content: ...}}`.
"""
from __future__ import annotations

import logging

import httpx

from ..config import settings
from .base import LLMProvider

log = logging.getLogger("jhh.llm.ollama")

_DEFAULT_MODEL = "llama3"
_DEFAULT_TIMEOUT = 120.0  # local models can be slow


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self) -> None:
        self.base_url = (settings.ollama_base_url or "http://localhost:11434").rstrip("/")
        self.model = settings.llm_model or _DEFAULT_MODEL
        self.timeout = _DEFAULT_TIMEOUT

    def complete(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.3) -> str:
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
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(url, json=payload)
                if r.status_code >= 400:
                    log.warning("ollama %d: %s", r.status_code, r.text[:500])
                    return ""
                data = r.json()
                msg = data.get("message") or {}
                return (msg.get("content") or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("ollama call failed: %s", e)
            return ""
