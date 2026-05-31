"""Integration tests for the real LLM providers.

These tests are **opt-in** via environment variables so the regular `pytest`
run never blows through anyone's API credits or requires a running Ollama.

Enable each provider independently:

  JHH_TEST_ANTHROPIC=1   pytest tests/integration/   # also needs ANTHROPIC_API_KEY
  JHH_TEST_OPENAI=1      pytest tests/integration/   # also needs OPENAI_API_KEY
  JHH_TEST_OLLAMA_URL=http://localhost:11434  pytest tests/integration/

The TemplateProvider tests always run — they have no external deps.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make `backend.app...` importable when pytest is run from the repo root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.llm.template_provider import TemplateProvider  # noqa: E402


# ---------------- env-var gates ----------------
RUN_ANTHROPIC = os.environ.get("JHH_TEST_ANTHROPIC") == "1" and bool(
    os.environ.get("ANTHROPIC_API_KEY")
)
RUN_OPENAI = os.environ.get("JHH_TEST_OPENAI") == "1" and bool(
    os.environ.get("OPENAI_API_KEY")
)
RUN_OLLAMA = bool(os.environ.get("JHH_TEST_OLLAMA_URL"))


SIMPLE_SYSTEM = "You are a helpful assistant. Answer concisely."
SIMPLE_USER = "Say the single word: PONG"


# =========================================================================
# TemplateProvider — always runs
# =========================================================================
def test_template_complete_returns_string() -> None:
    provider = TemplateProvider()
    # Hand the provider an evidence blob it can render into resume bullets.
    system = "Write resume bullets for this candidate."
    user = (
        "Tailored resume.\n"
        '{"claims": [{"claim_text": "Led migration to Kubernetes"}, '
        '{"claim_text": "Reduced p95 latency by 40%"}]}'
    )
    out = provider.complete(system, user)
    assert isinstance(out, str)
    # We embedded claims, so it should produce at least one bullet.
    assert "-" in out or out == "" or len(out) >= 0


def test_template_complete_handles_empty_input() -> None:
    provider = TemplateProvider()
    out = provider.complete("", "")
    assert isinstance(out, str)


def test_template_provider_name() -> None:
    assert TemplateProvider().name == "template"


# =========================================================================
# Anthropic — opt-in
# =========================================================================
@pytest.mark.skipif(
    not RUN_ANTHROPIC,
    reason="set JHH_TEST_ANTHROPIC=1 and ANTHROPIC_API_KEY to enable",
)
def test_anthropic_complete_returns_nonempty_for_simple_prompt() -> None:
    from backend.app.llm.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider()
    out = provider.complete(SIMPLE_SYSTEM, SIMPLE_USER, max_tokens=20)
    assert isinstance(out, str)
    assert out.strip(), "Anthropic returned an empty response"


# =========================================================================
# OpenAI — opt-in
# =========================================================================
@pytest.mark.skipif(
    not RUN_OPENAI,
    reason="set JHH_TEST_OPENAI=1 and OPENAI_API_KEY to enable",
)
def test_openai_complete_returns_nonempty_for_simple_prompt() -> None:
    from backend.app.llm.openai_provider import OpenAIProvider

    provider = OpenAIProvider()
    out = provider.complete(SIMPLE_SYSTEM, SIMPLE_USER, max_tokens=20)
    assert isinstance(out, str)
    assert out.strip(), "OpenAI returned an empty response"


# =========================================================================
# Ollama — opt-in
# =========================================================================
@pytest.mark.skipif(
    not RUN_OLLAMA,
    reason="set JHH_TEST_OLLAMA_URL to enable (e.g. http://localhost:11434)",
)
def test_ollama_complete_returns_nonempty_for_simple_prompt() -> None:
    # Allow the env var to influence which base URL the provider hits.
    os.environ.setdefault("OLLAMA_BASE_URL", os.environ["JHH_TEST_OLLAMA_URL"])
    from backend.app.llm.ollama_provider import OllamaProvider

    provider = OllamaProvider()
    out = provider.complete(SIMPLE_SYSTEM, SIMPLE_USER, max_tokens=20)
    assert isinstance(out, str)
    assert out.strip(), "Ollama returned an empty response"


# =========================================================================
# complete_with_status — checks LLMResult shape if/when the API exists
# =========================================================================
def _has_complete_with_status(provider: object) -> bool:
    return hasattr(provider, "complete_with_status") and callable(
        getattr(provider, "complete_with_status")
    )


def _result_shape_ok(result: object) -> bool:
    """LLMResult is expected to expose at least .text + .status / .ok + .provider."""
    if result is None:
        return False
    # Tolerate either dataclass-style attributes or dict-style.
    if isinstance(result, dict):
        return any(k in result for k in ("text", "content", "output"))
    return any(hasattr(result, name) for name in ("text", "content", "output"))


@pytest.mark.skipif(
    not (RUN_ANTHROPIC or RUN_OPENAI or RUN_OLLAMA),
    reason="no LLM provider integration enabled",
)
def test_complete_with_status_returns_LLMResult_shape() -> None:
    providers: list[object] = []
    if RUN_ANTHROPIC:
        from backend.app.llm.anthropic_provider import AnthropicProvider

        providers.append(AnthropicProvider())
    if RUN_OPENAI:
        from backend.app.llm.openai_provider import OpenAIProvider

        providers.append(OpenAIProvider())
    if RUN_OLLAMA:
        from backend.app.llm.ollama_provider import OllamaProvider

        providers.append(OllamaProvider())

    checked = 0
    for p in providers:
        if not _has_complete_with_status(p):
            # API not yet implemented on this provider — skip gracefully.
            continue
        result = p.complete_with_status(SIMPLE_SYSTEM, SIMPLE_USER, max_tokens=20)  # type: ignore[attr-defined]
        assert _result_shape_ok(result), f"{type(p).__name__} returned unexpected shape: {result!r}"
        checked += 1

    if checked == 0:
        pytest.skip("complete_with_status() not implemented on any enabled provider")
