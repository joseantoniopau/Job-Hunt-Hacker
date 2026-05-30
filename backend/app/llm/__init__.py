"""LLM provider auto-selection. Falls back to template provider when no
API key is present so the app always runs.
"""
from __future__ import annotations

from ..config import settings
from .base import LLMProvider
from .template_provider import TemplateProvider


def get_llm() -> LLMProvider:
    p = (settings.llm_provider or "auto").lower()
    if p in ("anthropic", "auto") and settings.anthropic_api_key:
        try:
            from .anthropic_provider import AnthropicProvider
            return AnthropicProvider()
        except Exception:
            pass
    if p in ("openai", "auto") and settings.openai_api_key:
        try:
            from .openai_provider import OpenAIProvider
            return OpenAIProvider()
        except Exception:
            pass
    if p in ("ollama", "auto") and settings.ollama_base_url:
        try:
            from .ollama_provider import OllamaProvider
            return OllamaProvider()
        except Exception:
            pass
    return TemplateProvider()


__all__ = ["LLMProvider", "get_llm"]
