"""GET /api/settings — server-side view of which providers are configured.
Also exposes the list of registered job source adapters and their policy.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from ..config import settings
from ..services.job_sources import REGISTRY

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings")
def get_settings() -> dict:
    sources = []
    for name, adapter in sorted(REGISTRY.items()):
        try:
            policy = asdict(adapter.policy)
        except Exception:
            policy = {"name": name, "display_name": name, "risk_level": "GRAY"}
        sources.append({"name": name, "healthy": bool(adapter.healthy()), "policy": policy})

    return {
        "ok": True,
        "data": {
            "version": "0.1.0",
            "default_mode": settings.default_mode,
            "auto_apply_enabled": settings.auto_apply_enabled,
            "auto_apply_daily_cap": settings.auto_apply_daily_cap,
            "auto_apply_min_score": settings.auto_apply_min_score,
            "llm": {
                "provider": settings.llm_provider,
                "anthropic_configured": bool(settings.anthropic_api_key),
                "openai_configured": bool(settings.openai_api_key),
                "ollama_configured": bool(settings.ollama_base_url),
                "model": settings.llm_model or "(default)",
            },
            "embeddings": {
                "provider": settings.embed_provider,
                "openai_configured": bool(settings.openai_api_key),
                "model": settings.embed_model or "(auto)",
            },
            "integrations": {
                "serpapi": bool(settings.serpapi_key),
                "searchapi": bool(settings.searchapi_key),
                "github": bool(settings.github_token),
                "google_oauth": bool(settings.google_client_id and settings.google_client_secret),
                "imap": bool(settings.imap_host and settings.imap_user),
            },
            "job_sources": sources,
        },
    }
