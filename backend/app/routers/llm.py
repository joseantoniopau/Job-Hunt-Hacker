"""GET/POST /api/llm — discover and configure local LLM providers.

Job Hunt Hacker works without an LLM (TemplateProvider fallback), but every
inference + tailoring step gets noticeably better with one. This router:

  * Scans the host for known local-LLM daemons (Ollama, LM Studio, vLLM,
    llama.cpp server) on their conventional ports.
  * Reports each daemon's installed models.
  * Picks a recommended model based on detected system RAM + the role the
    LLM plays in this app (resume parsing → structured JSON → instruct
    models in the 7B–70B range work best).
  * Lets the UI pin a chosen base_url + model into .env in one click.
  * Returns install guidance for users who have no local LLM yet, with
    OS-aware one-liners.

No network calls are made to anything but the user's own loopback
addresses — the discovery routine is deliberately scoped to localhost.
"""
from __future__ import annotations

import logging
import os
import platform
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import ENV_FILE, settings
from ..db import audit

log = logging.getLogger("jhh.llm")
router = APIRouter(prefix="/api/llm", tags=["llm"])


# Conventional local LLM endpoints — these are the daemons we know how to
# probe. We only scan loopback addresses; nothing else.
_LOCAL_PROBES: list[dict[str, str]] = [
    {"type": "ollama",     "base_url": "http://localhost:11434", "list_path": "/api/tags"},
    {"type": "ollama",     "base_url": "http://127.0.0.1:11434", "list_path": "/api/tags"},
    {"type": "lmstudio",   "base_url": "http://localhost:1234",  "list_path": "/v1/models"},
    {"type": "lmstudio",   "base_url": "http://127.0.0.1:1234",  "list_path": "/v1/models"},
    {"type": "vllm",       "base_url": "http://localhost:8000",  "list_path": "/v1/models"},
    {"type": "llamacpp",   "base_url": "http://localhost:8080",  "list_path": "/v1/models"},
]


def _detect_ram_gb() -> int:
    """Return the host's installed RAM in GB, or 0 if we can't tell."""
    try:
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names and "SC_PHYS_PAGES" in os.sysconf_names:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return int(pages * page_size / (1024 ** 3))
    except Exception:
        pass
    try:
        # macOS fallback via sysctl
        import subprocess
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2)
        if out.returncode == 0 and out.stdout.strip().isdigit():
            return int(int(out.stdout.strip()) / (1024 ** 3))
    except Exception:
        pass
    return 0


def _recommend_model(installed: list[str], ram_gb: int) -> dict[str, Any]:
    """Pick the best installed model for our workload (resume parsing,
    structured JSON, instruct-following). Falls back to a 'please install'
    suggestion when no local model exists.

    Preference order, biggest → smallest, biased toward instruct tunes:
      llama3.3:70b-instruct, qwen2.5:72b-instruct, mixtral:8x22b,
      qwen2.5-coder:32b, qwen2.5:32b, qwen2.5:14b-instruct,
      llama3.1:8b, qwen2.5:7b-instruct, phi3:mini, llama3.2:3b.
    """
    preference = [
        # 70B class — best quality, needs ~48GB+ RAM
        "llama3.3:70b-instruct-q5_K_M", "llama3.3:70b-instruct", "llama3.3:70b",
        "qwen2.5:72b-instruct-q5_K_M", "qwen2.5:72b-instruct", "qwen2.5:72b",
        "deepseek-r1:70b",
        "mixtral:8x22b-instruct",
        # 32B class — strong, needs ~24GB
        "qwen2.5-coder:32b", "qwen2.5:32b",
        # 14B class — solid, needs ~12GB
        "qwen2.5:14b-instruct-q4_K_M", "qwen2.5:14b-instruct", "qwen2.5:14b",
        # 7-8B class — fine on most laptops, needs ~6-8GB
        "llama3.1:8b", "qwen2.5:7b-instruct", "qwen2.5:7b", "mistral:7b-instruct",
        # 3B class — last-resort tiny
        "phi3:mini", "llama3.2:3b",
    ]
    installed_lower = {m.lower(): m for m in installed}
    for cand in preference:
        if cand.lower() in installed_lower:
            return {
                "name": installed_lower[cand.lower()],
                "reason": _why(cand, ram_gb),
                "installed": True,
            }
    # No good match installed. Suggest one based on RAM.
    if ram_gb >= 48:
        return {"name": "llama3.3:70b-instruct-q5_K_M", "installed": False,
                "reason": "Best quality. Your machine has the RAM for it."}
    if ram_gb >= 24:
        return {"name": "qwen2.5:32b", "installed": False,
                "reason": "Great quality, fits comfortably in your RAM."}
    if ram_gb >= 12:
        return {"name": "qwen2.5:14b-instruct-q4_K_M", "installed": False,
                "reason": "Solid balance of quality and speed."}
    if ram_gb >= 6:
        return {"name": "qwen2.5:7b-instruct", "installed": False,
                "reason": "Fits in modest RAM; good instruct following."}
    return {"name": "llama3.2:3b", "installed": False,
            "reason": "Smallest viable model for low-RAM machines."}


def _why(model: str, ram_gb: int) -> str:
    if "70b" in model.lower() or "72b" in model.lower() or "8x22b" in model.lower():
        return f"Best quality available locally — recommended for your {ram_gb} GB machine."
    if "32b" in model.lower():
        return f"Strong quality at smaller footprint — comfortable on {ram_gb} GB."
    if "14b" in model.lower():
        return f"Good balance of quality and speed for {ram_gb} GB."
    if "7b" in model.lower() or "8b" in model.lower():
        return f"Lightweight and quick — fine for {ram_gb} GB."
    return f"Selected for your {ram_gb} GB machine."


def _install_guide() -> dict[str, Any]:
    """OS-aware one-liner install instructions for Ollama — the easiest
    path for non-technical users."""
    system = platform.system().lower()
    if system == "darwin":
        # Ollama bundles a clickable .dmg. brew is more sysadmin-friendly.
        return {
            "os": "macOS",
            "steps": [
                {"title": "Install Ollama",
                 "command": "brew install ollama",
                 "alt_command": "Download from https://ollama.com/download (clickable .dmg)"},
                {"title": "Start Ollama",
                 "command": "ollama serve",
                 "note": "Or just open the Ollama app — it runs in the menu bar."},
                {"title": "Pull a recommended model",
                 "command": "ollama pull llama3.3:70b-instruct-q5_K_M",
                 "note": "Smaller? Try: ollama pull qwen2.5:7b-instruct"},
                {"title": "Come back to this Settings page and click DETECT LOCAL LLMs"},
            ],
        }
    if system == "linux":
        return {
            "os": "Linux",
            "steps": [
                {"title": "Install Ollama",
                 "command": "curl -fsSL https://ollama.com/install.sh | sh"},
                {"title": "Start Ollama (auto-starts on most installs)",
                 "command": "systemctl --user enable --now ollama",
                 "alt_command": "ollama serve"},
                {"title": "Pull a recommended model",
                 "command": "ollama pull qwen2.5:7b-instruct",
                 "note": "Bigger machine? ollama pull llama3.3:70b-instruct-q5_K_M"},
                {"title": "Come back and click DETECT LOCAL LLMs"},
            ],
        }
    if system == "windows":
        return {
            "os": "Windows",
            "steps": [
                {"title": "Download the Ollama installer",
                 "command": "https://ollama.com/download/windows",
                 "note": "Double-click to install. Ollama starts automatically."},
                {"title": "Pull a recommended model (in PowerShell)",
                 "command": "ollama pull qwen2.5:7b-instruct"},
                {"title": "Come back and click DETECT LOCAL LLMs"},
            ],
        }
    return {
        "os": "Unknown",
        "steps": [
            {"title": "Visit Ollama's site",
             "command": "https://ollama.com/download",
             "note": "Download for your OS, install, then run: ollama pull qwen2.5:7b-instruct"},
        ],
    }


@router.get("/discover")
def discover_local_llms() -> dict:
    """Probe loopback ports for known local-LLM daemons and report what is
    running, what models are installed on each, and what we recommend."""
    found: list[dict[str, Any]] = []
    seen_base_urls: set[str] = set()
    for probe in _LOCAL_PROBES:
        base = probe["base_url"]
        if base in seen_base_urls:
            continue
        try:
            r = httpx.get(base + probe["list_path"], timeout=2.0)
            if r.status_code != 200:
                continue
            data = r.json()
            models: list[str] = []
            if probe["type"] == "ollama":
                models = [m.get("name", "") for m in (data.get("models") or []) if m.get("name")]
            else:  # OpenAI-compatible (LM Studio, vLLM, llama.cpp server)
                models = [m.get("id", "") for m in (data.get("data") or []) if m.get("id")]
            if not models:
                continue
            seen_base_urls.add(base)
            found.append({
                "type": probe["type"],
                "base_url": base,
                "models": sorted(models),
            })
        except Exception:
            continue

    ram_gb = _detect_ram_gb()
    all_models: list[str] = []
    for daemon in found:
        all_models.extend(daemon["models"])

    recommended = _recommend_model(all_models, ram_gb)
    # Bind the recommended model to a specific daemon when it is installed
    if recommended.get("installed"):
        for daemon in found:
            if recommended["name"] in daemon["models"]:
                recommended["base_url"] = daemon["base_url"]
                recommended["type"] = daemon["type"]
                break

    return {
        "ok": True,
        "data": {
            "daemons": found,
            "ram_gb": ram_gb,
            "current": {
                "provider": settings.llm_provider,
                "model": settings.llm_model or "",
                "ollama_base_url": settings.ollama_base_url or "",
                "openai_base_url": settings.openai_base_url or "",
            },
            "recommended": recommended,
            "install_guide": _install_guide() if not found else None,
        },
    }


class UseLocalRequest(BaseModel):
    """Pin a specific local LLM to be the active provider.

    `base_url` selects the daemon (e.g. http://localhost:11434).
    `model` selects which installed model to use.
    `provider_kind` is one of {"ollama", "openai-compatible"} — Ollama uses
    its native API; everything else uses the OpenAI-compatible path.
    """
    base_url: str
    model: str
    provider_kind: str = "ollama"


@router.post("/use-local")
def use_local(body: UseLocalRequest) -> dict:
    base_url = (body.base_url or "").strip()
    model = (body.model or "").strip()
    kind = (body.provider_kind or "ollama").strip().lower()
    if not base_url or not model:
        raise HTTPException(400, "base_url and model are required")
    if not (base_url.startswith("http://localhost") or
            base_url.startswith("http://127.0.0.1") or
            base_url.startswith("http://0.0.0.0")):
        raise HTTPException(400, "base_url must be a loopback address (localhost / 127.0.0.1)")

    # Sanity-ping the daemon so we don't pin a broken config.
    try:
        if kind == "ollama":
            r = httpx.get(f"{base_url}/api/tags", timeout=3.0)
        else:
            r = httpx.get(f"{base_url}/v1/models", timeout=3.0)
        if r.status_code != 200:
            raise HTTPException(502, f"daemon returned HTTP {r.status_code}")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"could not reach daemon at {base_url}: {exc}") from exc

    updates: dict[str, str] = {
        "JHH_LLM_PROVIDER": "ollama" if kind == "ollama" else "openai",
        "JHH_LLM_MODEL": model,
    }
    if kind == "ollama":
        updates["OLLAMA_BASE_URL"] = base_url
    else:
        # OpenAI-compatible: point OPENAI_BASE_URL at the local daemon and
        # set a placeholder key so the OpenAI client doesn't refuse to send.
        updates["OPENAI_BASE_URL"] = base_url.rstrip("/") + "/v1"
        if not (settings.openai_api_key or "").strip():
            updates["OPENAI_API_KEY"] = "sk-local-anyvalue"

    # Persist via the same writer the settings router uses so .env stays
    # comment-preserving and the running process picks up the change.
    from .settings import _write_env  # local import to avoid cycle at boot
    _write_env(updates)
    for k, v in updates.items():
        os.environ[k] = v

    # Reflect into the in-memory settings so providers refresh without restart
    settings.llm_provider = updates.get("JHH_LLM_PROVIDER", settings.llm_provider)
    settings.llm_model = updates.get("JHH_LLM_MODEL", settings.llm_model)
    if "OLLAMA_BASE_URL" in updates:
        settings.ollama_base_url = updates["OLLAMA_BASE_URL"]
    if "OPENAI_BASE_URL" in updates:
        settings.openai_base_url = updates["OPENAI_BASE_URL"]
    if "OPENAI_API_KEY" in updates:
        settings.openai_api_key = updates["OPENAI_API_KEY"]

    audit("llm_use_local", "settings", None, provider=updates["JHH_LLM_PROVIDER"], model=model)
    return {
        "ok": True,
        "data": {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "base_url": base_url,
            "env_path": str(ENV_FILE),
        },
    }


@router.post("/use-template")
def use_template() -> dict:
    """Pin the deterministic template provider (no LLM). Useful for
    explicit privacy or when the user wants to disable LLM use entirely."""
    updates = {"JHH_LLM_PROVIDER": "template"}
    from .settings import _write_env
    _write_env(updates)
    os.environ["JHH_LLM_PROVIDER"] = "template"
    settings.llm_provider = "template"
    audit("llm_use_template", "settings", None)
    return {"ok": True, "data": {"provider": settings.llm_provider}}


@router.get("/runs")
def list_llm_runs(limit: int = 50, since_id: int = 0, stage: str | None = None) -> dict:
    """List recent LLM runs (newest first). UI polls this for the live activity panel."""
    from ..llm.observability import list_runs, active_count
    limit = max(1, min(int(limit), 200))
    runs = list_runs(limit=limit, since_id=int(since_id), stage=stage)
    return {"ok": True, "data": {"runs": runs, "active": active_count()}}


@router.get("/runs/{run_id}")
def get_llm_run(run_id: int) -> dict:
    """Return the full prompt + output for a single LLM run."""
    from ..llm.observability import get_run
    r = get_run(int(run_id))
    if not r:
        raise HTTPException(404, "run not found")
    return {"ok": True, "data": r}


@router.post("/test")
def test_active_provider() -> dict:
    """Run one tiny LLM call against the currently-configured provider and
    return latency + the first tokens of its response. Surfaces obvious
    misconfigurations (wrong model name, daemon stopped, etc.)."""
    from ..llm import get_llm
    import time

    try:
        provider = get_llm()
    except Exception as exc:
        return {"ok": False, "error": f"provider init failed: {exc}",
                "data": {"provider": settings.llm_provider}}

    if getattr(provider, "name", "") == "template":
        return {"ok": True, "data": {
            "provider": "template",
            "note": "Deterministic template provider — no network call to test.",
        }}

    from ..llm.observability import observed_complete
    started = time.time()
    try:
        out, run_id = observed_complete(
            provider,
            stage="llm_test",
            system="You are a brief assistant.",
            user="Reply with exactly the word OK.",
            max_tokens=8,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": True,
            "data": {
                "provider": getattr(provider, "name", settings.llm_provider),
                "model": settings.llm_model or "(default)",
                "elapsed_ms": elapsed_ms,
                "sample": (out or "")[:120],
                "llm_run_id": run_id,
            },
        }
    except Exception as exc:
        elapsed_ms = int((time.time() - started) * 1000)
        return {
            "ok": False,
            "error": str(exc),
            "data": {
                "provider": getattr(provider, "name", settings.llm_provider),
                "model": settings.llm_model or "(default)",
                "elapsed_ms": elapsed_ms,
            },
        }
