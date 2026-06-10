"""LLM provider interface.

Surface methods:
  - complete(system, user, max_tokens) -> str               (legacy; still supported)
  - complete_with_status(system, user, ...) -> LLMResult    (modern; exposes rate-limit / error status)
  - complete_json(system, user, schema_hint) -> dict        (always returns dict; repairs invalid JSON)

Providers should NEVER raise on transient errors -- wrap in try/except and
return a string (legacy) / `LLMResult(status="error", ...)` (modern). The
TemplateProvider exists so the app always has a working LLM, even with no
API keys.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


# Canonical status values for LLMResult.status. Keep narrow on purpose;
# callers branch on these so adding new ones is a UI-visible event.
STATUS_OK = "ok"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_ERROR = "error"
STATUS_EMPTY = "empty"


@dataclass
class LLMResult:
    text: str
    status: str = STATUS_OK
    latency_ms: int = 0
    error: str | None = None


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def complete(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.3) -> str: ...

    def complete_with_status(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> LLMResult:
        """Default impl wraps `complete()`; subclasses override to surface
        status codes (rate_limited / error). Backward compatible: providers
        that only implement `complete()` still work."""
        import time as _t
        t0 = _t.perf_counter()
        try:
            text = self.complete(system, user, max_tokens=max_tokens, temperature=temperature)
        except Exception as exc:  # noqa: BLE001
            return LLMResult(
                text="",
                status=STATUS_ERROR,
                latency_ms=int((_t.perf_counter() - t0) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        latency = int((_t.perf_counter() - t0) * 1000)
        if not text:
            return LLMResult(text="", status=STATUS_EMPTY, latency_ms=latency)
        return LLMResult(text=text, status=STATUS_OK, latency_ms=latency)

    def complete_json(self, system: str, user: str, schema_hint: dict | None = None,
                      max_tokens: int = 2048, temperature: float = 0.2) -> dict:
        from .json_repair import extract_json
        sys2 = system + "\n\nRespond ONLY with valid JSON. No prose, no fences."
        if schema_hint:
            import json as _j
            sys2 += "\n\nExpected JSON shape:\n" + _j.dumps(schema_hint, indent=2)
        for attempt in range(3):
            raw = self.complete(sys2, user, max_tokens=max_tokens, temperature=temperature)
            data = extract_json(raw)
            if data is not None:
                return data
            user = user + f"\n\n(Attempt {attempt + 1} returned invalid JSON. Return ONLY valid JSON now.)"
        import logging
        logging.getLogger("jhh.llm").warning(
            "complete_json: %s returned unparseable JSON after 3 attempts "
            "(last output %d chars) — caller will see an empty dict",
            getattr(self, "name", type(self).__name__), len(raw or ""),
        )
        return {}

    def supports_json(self) -> bool:
        return True
