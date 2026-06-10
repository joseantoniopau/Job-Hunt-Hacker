"""GET/POST /api/settings — providers/sources view + API key management.

The API-keys endpoints let the user enter optional credentials through the
UI instead of editing .env by hand. Values are persisted to .env (preserving
other lines) AND applied to the running process so providers refresh
without a restart.
"""
from __future__ import annotations

import logging
import os
import stat
import time
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import APP_VERSION, ENV_FILE, settings
from ..db import audit
from ..services.job_sources import REGISTRY

log = logging.getLogger("jhh.settings")

router = APIRouter(prefix="/api", tags=["settings"])


# Whitelist of env keys exposed through the UI, with metadata for grouping
# and rendering. Order matters — UI walks this in order.
API_KEYS: list[dict[str, Any]] = [
    # --- LLM providers ---
    {"env": "ANTHROPIC_API_KEY",  "label": "Anthropic API key",
     "group": "llm",          "kind": "secret", "purpose": "best resume / cover-letter quality",
     "settings_attr": "anthropic_api_key"},
    {"env": "OPENAI_API_KEY",     "label": "OpenAI API key",
     "group": "llm",          "kind": "secret", "purpose": "alternate LLM + embeddings",
     "settings_attr": "openai_api_key"},
    {"env": "OPENAI_BASE_URL",    "label": "OpenAI base URL",
     "group": "llm",          "kind": "url",    "purpose": "for Ollama / local OpenAI-compatible endpoints",
     "settings_attr": "openai_base_url"},
    {"env": "OLLAMA_BASE_URL",    "label": "Ollama base URL",
     "group": "llm",          "kind": "url",    "purpose": "e.g. http://localhost:11434",
     "settings_attr": "ollama_base_url"},
    {"env": "JHH_LLM_PROVIDER",   "label": "LLM provider preference",
     "group": "llm",          "kind": "choice", "choices": ["auto", "anthropic", "openai", "ollama", "template"],
     "purpose": "force a specific provider", "settings_attr": "llm_provider"},
    {"env": "JHH_LLM_MODEL",      "label": "LLM model (optional)",
     "group": "llm",          "kind": "string", "purpose": "e.g. claude-sonnet-4-6 or gpt-4o-mini",
     "settings_attr": "llm_model"},

    # --- Job sources ---
    {"env": "SERPAPI_API_KEY",    "label": "SerpApi key",
     "group": "jobs",         "kind": "secret", "purpose": "enables Google Jobs adapter",
     "settings_attr": "serpapi_key"},
    {"env": "SEARCHAPI_API_KEY",  "label": "SearchAPI key",
     "group": "jobs",         "kind": "secret", "purpose": "alternate Google Jobs provider",
     "settings_attr": "searchapi_key"},
    {"env": "GITHUB_TOKEN",       "label": "GitHub token",
     "group": "jobs",         "kind": "secret", "purpose": "raises GitHub ingest rate limit",
     "settings_attr": "github_token"},

    # --- Email / Calendar ---
    {"env": "GOOGLE_CLIENT_ID",     "label": "Google OAuth client ID",
     "group": "google",       "kind": "string", "purpose": "for Gmail + Calendar OAuth",
     "settings_attr": "google_client_id"},
    {"env": "GOOGLE_CLIENT_SECRET", "label": "Google OAuth client secret",
     "group": "google",       "kind": "secret", "purpose": "matches the client ID above",
     "settings_attr": "google_client_secret"},
    {"env": "IMAP_HOST",            "label": "IMAP host",
     "group": "imap",         "kind": "string", "purpose": "fallback inbox monitor (non-Gmail)",
     "settings_attr": "imap_host"},
    {"env": "IMAP_USER",            "label": "IMAP user",
     "group": "imap",         "kind": "string", "purpose": "",
     "settings_attr": "imap_user"},
    {"env": "IMAP_PASS",            "label": "IMAP password / app token",
     "group": "imap",         "kind": "secret", "purpose": "stored in .env on this machine only",
     "settings_attr": "imap_pass"},
]


# ----- existing endpoint -----

class ModeUpdate(BaseModel):
    mode: str


ALLOWED_MODES = {"research", "assisted", "auto"}


@router.put("/settings/mode")
def put_mode(body: ModeUpdate) -> dict:
    """Persist the user-visible default mode.

    Writes to BOTH `user_profile.mode` (so the choice survives restart and
    is visible alongside the rest of the profile) AND `settings.default_mode`
    in-memory (so subsequent calls during the same process see it without
    re-reading the DB).
    """
    mode = (body.mode or "").strip().lower()
    if mode not in ALLOWED_MODES:
        raise HTTPException(400, f"invalid mode: {body.mode!r}; allowed={sorted(ALLOWED_MODES)}")
    from ..db import get_conn
    conn = get_conn()
    conn.execute(
        "UPDATE user_profile SET mode = ?, updated_at = ? WHERE id = 1",
        (mode, time.time()),
    )
    settings.default_mode = mode
    audit("settings_mode_update", "settings", None, mode=mode)
    return {"ok": True, "data": {"mode": mode}}


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
            "version": APP_VERSION,
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


# ----- API keys management -----

@router.get("/settings/api-keys")
def list_api_keys() -> dict:
    """Return the list of supported API keys, plus a masked preview of each
    currently-configured value. The full key is never returned.
    """
    out = []
    for spec in API_KEYS:
        attr = spec.get("settings_attr") or ""
        val = getattr(settings, attr, "") or ""
        out.append({
            "env": spec["env"],
            "label": spec["label"],
            "group": spec["group"],
            "kind": spec["kind"],
            "purpose": spec.get("purpose", ""),
            "choices": spec.get("choices"),
            "configured": bool(val),
            "preview": _mask(val) if spec["kind"] == "secret" else val,
        })
    return {"ok": True, "data": {"keys": out, "env_path": str(ENV_FILE)}}


class APIKeyUpdate(BaseModel):
    keys: dict[str, str]   # {env_name: value}; empty string deletes


@router.put("/settings/api-keys")
def put_api_keys(body: APIKeyUpdate) -> dict:
    """Apply key updates: writes to .env (preserving comments + order) and
    refreshes the running settings object so providers see the change
    without a restart.
    """
    allowed = {spec["env"]: spec for spec in API_KEYS}
    updates: dict[str, str] = {}
    for env_name, value in (body.keys or {}).items():
        if env_name not in allowed:
            raise HTTPException(400, f"unknown key: {env_name}")
        updates[env_name] = (value or "").strip()

    if not updates:
        return {"ok": True, "data": {"updated": [], "cleared": []}}

    _write_env(updates)

    # Apply in-process: update os.environ and settings attrs
    updated_names = []
    cleared_names = []
    for env_name, value in updates.items():
        os.environ[env_name] = value
        spec = allowed[env_name]
        attr = spec.get("settings_attr")
        if attr and hasattr(settings, attr):
            setattr(settings, attr, value)
        (updated_names if value else cleared_names).append(env_name)

    audit("api_keys_update", "settings", None,
          updated=updated_names, cleared=cleared_names)
    return {"ok": True, "data": {
        "updated": updated_names,
        "cleared": cleared_names,
        "env_path": str(ENV_FILE),
    }}


# ----- helpers -----

def _mask(val: str) -> str:
    if not val:
        return ""
    v = val.strip()
    if len(v) <= 8:
        return "•" * len(v)
    return v[:4] + "•" * 8 + v[-4:]


# ----- live API key tester -----

class APIKeyTestRequest(BaseModel):
    env: str
    # Optional override value (test before saving). If absent, uses the
    # currently-persisted value from settings.
    value: str | None = None


@router.post("/settings/api-keys/test")
def test_api_key(body: APIKeyTestRequest) -> dict:
    """Try the key against its provider's API. Returns a structured result
    the UI can render as a green check / red X.
    """
    allowed = {spec["env"]: spec for spec in API_KEYS}
    if body.env not in allowed:
        # Return our standard {ok:false, data:{...}} envelope so the UI's
        # api wrapper picks up the message and renders it on the row.
        return {"ok": False, "data": {
            "env": body.env, "ok": False, "status": "unknown_key",
            "message": f"unknown key: {body.env}",
        }}
    spec = allowed[body.env]
    attr = spec.get("settings_attr") or ""
    val = (body.value or "").strip() or (getattr(settings, attr, "") or "")
    if not val and spec["kind"] != "choice":
        return {"ok": True, "data": {"env": body.env, "status": "empty",
                                     "ok": False, "message": "no value set"}}

    res = _run_test(body.env, val)
    audit("api_key_test", "settings", None, env=body.env, ok=res.get("ok"),
          status=res.get("status"))
    return {"ok": True, "data": {"env": body.env, **res}}


@router.post("/settings/api-keys/test-all")
def test_all_keys() -> dict:
    """Run the test for every key that has a value. Includes a 'unlocks'
    field naming the job sources / features that flip live after a pass.
    """
    out = []
    for spec in API_KEYS:
        attr = spec.get("settings_attr") or ""
        val = getattr(settings, attr, "") or ""
        if not val or spec["kind"] == "choice":
            continue
        res = _run_test(spec["env"], val)
        out.append({"env": spec["env"], **res, "unlocks": _unlocks(spec["env"])})
    return {"ok": True, "data": {"results": out, "tested_at": time.time()}}


@router.post("/settings/sources/test/{name}")
def test_source(name: str) -> dict:
    """Probe a job-source adapter with a cheap real search.

    Returns latency_ms + record count. Used to verify Greenhouse / Lever /
    Ashby / Remotive / WWR / Google Jobs are actually reachable from this
    machine, not just registered.
    """
    from ..services.job_sources import REGISTRY
    from ..services.job_sources.base import JobSearchQuery
    adapter = REGISTRY.get(name)
    if adapter is None:
        raise HTTPException(404, f"no such adapter: {name}")
    if not adapter.healthy():
        return {"ok": True, "data": {"name": name, "ok": False,
                                     "status": "unhealthy",
                                     "message": "adapter not configured / missing dep"}}
    started = time.time()
    try:
        records = adapter.search(JobSearchQuery(query="engineer", results_per_site=5,
                                                hours_old=720))
        dt = int((time.time() - started) * 1000)
        n = len(records)
        return {"ok": True, "data": {
            "name": name,
            "ok": True,
            "status": "ok",
            "records": n,
            "latency_ms": dt,
            "message": f"{n} records in {dt}ms",
        }}
    except Exception as exc:  # noqa: BLE001
        dt = int((time.time() - started) * 1000)
        return {"ok": True, "data": {
            "name": name,
            "ok": False,
            "status": "error",
            "latency_ms": dt,
            "message": f"{type(exc).__name__}: {exc}",
        }}


def _unlocks(env_name: str) -> list[str]:
    return {
        "SERPAPI_API_KEY":   ["google_jobs"],
        "SEARCHAPI_API_KEY": ["google_jobs"],
        "GITHUB_TOKEN":      ["github ingest (raised rate limit)"],
        "ANTHROPIC_API_KEY": ["llm (Anthropic)", "resume tailoring", "cover letters"],
        "OPENAI_API_KEY":    ["llm (OpenAI)", "embeddings (OpenAI)"],
        "OLLAMA_BASE_URL":   ["llm (Ollama local)"],
        "OPENAI_BASE_URL":   ["llm (OpenAI-compatible local / proxy)"],
        "GOOGLE_CLIENT_ID":  ["gmail oauth", "google calendar"],
        "GOOGLE_CLIENT_SECRET": ["gmail oauth", "google calendar"],
        "IMAP_HOST":         ["inbox monitoring (IMAP)"],
        "IMAP_USER":         ["inbox monitoring (IMAP)"],
        "IMAP_PASS":         ["inbox monitoring (IMAP)"],
    }.get(env_name, [])


def _run_test(env: str, val: str) -> dict[str, Any]:
    """Live network/socket test for a single key. Returns
    {ok: bool, status: str, latency_ms: int, message: str}. Never raises.
    """
    started = time.time()
    try:
        if env == "ANTHROPIC_API_KEY":
            return _http_test(
                "POST", "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": val, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json_body={"model": settings.llm_model or "claude-sonnet-4-6",
                           "max_tokens": 4,
                           "messages": [{"role": "user", "content": "ping"}]},
                started=started,
                success_codes={200},
            )

        if env == "OPENAI_API_KEY":
            base = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
            return _http_test(
                "GET", f"{base}/models",
                headers={"Authorization": f"Bearer {val}"},
                started=started,
                success_codes={200},
            )

        if env == "OPENAI_BASE_URL":
            url = val.rstrip("/") + "/models"
            headers = {}
            if settings.openai_api_key:
                headers["Authorization"] = f"Bearer {settings.openai_api_key}"
            return _http_test("GET", url, headers=headers, started=started,
                              success_codes={200, 401})  # 401 = up, just unauthenticated

        if env == "OLLAMA_BASE_URL":
            return _http_test("GET", val.rstrip("/") + "/api/tags",
                              started=started, success_codes={200})

        if env == "SERPAPI_API_KEY":
            return _http_test(
                "GET", "https://serpapi.com/account",
                params={"api_key": val},
                started=started, success_codes={200},
            )

        if env == "SEARCHAPI_API_KEY":
            return _http_test(
                "GET", "https://www.searchapi.io/api/v1/search",
                params={"api_key": val, "q": "test", "engine": "google_jobs", "num": "1"},
                started=started,
                success_codes={200, 400},  # 400 = key OK but query rejected
            )

        if env == "GITHUB_TOKEN":
            return _http_test(
                "GET", "https://api.github.com/user",
                headers={"Authorization": f"Bearer {val}",
                         "Accept": "application/vnd.github+json"},
                started=started, success_codes={200},
            )

        if env in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
            # Cannot validate without the full OAuth dance. Sanity-check format.
            ok = len(val) >= 10 and val.replace("-", "").replace("_", "").replace(".", "").isalnum()
            return {
                "ok": bool(ok),
                "status": "format_ok" if ok else "format_bad",
                "latency_ms": int((time.time() - started) * 1000),
                "message": "format looks plausible — full validation requires running the OAuth flow"
                           if ok else "value doesn't look like a Google OAuth credential",
            }

        if env == "IMAP_HOST":
            return _imap_test(host=val,
                              user=settings.imap_user, passwd=settings.imap_pass,
                              started=started)

        if env in ("IMAP_USER", "IMAP_PASS"):
            return _imap_test(host=settings.imap_host,
                              user=settings.imap_user, passwd=settings.imap_pass,
                              started=started)

        if env == "JHH_LLM_MODEL":
            # Not testable in isolation. Considered "set".
            return {"ok": True, "status": "set", "latency_ms": 0,
                    "message": "model name applied — actual validity tested by your LLM provider key"}

        # Fallback: treat as plain string
        return {"ok": True, "status": "set", "latency_ms": 0,
                "message": "value stored (no live test available for this key)"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": "error",
                "latency_ms": int((time.time() - started) * 1000),
                "message": f"{type(exc).__name__}: {exc}"}


def _http_test(method: str, url: str, *, started: float,
               headers: dict[str, str] | None = None,
               params: dict[str, str] | None = None,
               json_body: dict[str, Any] | None = None,
               success_codes: set[int] | None = None) -> dict[str, Any]:
    try:
        import httpx
    except Exception:
        return {"ok": False, "status": "missing_httpx", "latency_ms": 0,
                "message": "httpx not installed"}
    success = success_codes or {200}
    try:
        with httpx.Client(timeout=12) as c:
            r = c.request(method, url, headers=headers or {}, params=params or None,
                          json=json_body)
        dt = int((time.time() - started) * 1000)
        if r.status_code in success:
            return {"ok": True, "status": "ok", "latency_ms": dt,
                    "message": f"HTTP {r.status_code} in {dt}ms"}
        # 401 / 403 / 429 are the typical "key bad" failures
        body = (r.text or "")[:120].replace("\n", " ")
        return {"ok": False, "status": f"http_{r.status_code}", "latency_ms": dt,
                "message": f"HTTP {r.status_code}: {body}"}
    except Exception as exc:  # noqa: BLE001
        dt = int((time.time() - started) * 1000)
        return {"ok": False, "status": "network", "latency_ms": dt,
                "message": f"{type(exc).__name__}: {exc}"}


def _imap_test(*, host: str, user: str, passwd: str, started: float) -> dict[str, Any]:
    if not (host and user and passwd):
        return {"ok": False, "status": "incomplete", "latency_ms": 0,
                "message": "need HOST + USER + PASS to test IMAP"}
    try:
        import imaplib
        with imaplib.IMAP4_SSL(host, timeout=12) as imap:
            imap.login(user, passwd)
            imap.logout()
        dt = int((time.time() - started) * 1000)
        return {"ok": True, "status": "ok", "latency_ms": dt,
                "message": f"IMAP login OK in {dt}ms"}
    except Exception as exc:  # noqa: BLE001
        dt = int((time.time() - started) * 1000)
        return {"ok": False, "status": "auth_or_network", "latency_ms": dt,
                "message": f"{type(exc).__name__}: {exc}"}


def _write_env(updates: dict[str, str]) -> None:
    """Atomically rewrite .env: preserve comments + order, update or append
    each requested key. Empty value comments-out (leaves `KEY=` so user can
    see it's been cleared)."""
    path = ENV_FILE
    if not path.exists():
        # If no .env yet, create from example or empty
        example = path.with_suffix(".env.example") if path.suffix == ".env" else (path.parent / ".env.example")
        path.write_text(example.read_text() if example.exists() else "")

    lines = path.read_text().splitlines()
    seen: set[str] = set()
    new_lines: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(raw)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(raw)

    # Append any updates that weren't already present
    appended = []
    for k, v in updates.items():
        if k not in seen:
            appended.append(f"{k}={v}")
    if appended:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append("# --- added via UI ---")
        new_lines.extend(appended)

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
