"""LLM provider interface.

Two surface methods:
  - complete(system, user, max_tokens) -> str
  - complete_json(system, user, schema_hint) -> dict  (always returns dict; repairs invalid JSON)

Providers should NEVER raise on transient errors — wrap in try/except and
return a string explaining the failure. The TemplateProvider exists so the
app always has a working LLM, even with no API keys.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def complete(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.3) -> str: ...

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
        return {}

    def supports_json(self) -> bool:
        return True
