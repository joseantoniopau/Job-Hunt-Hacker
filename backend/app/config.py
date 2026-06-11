"""Central config. Reads .env once; everything else imports `settings`."""
from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("jhh.config")

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"

# Single source of truth for the app version — main.py (FastAPI metadata,
# which /api/updates/check reads), /api/settings, and data exports all
# reference this.
APP_VERSION = "0.5.0"


def _load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    # .env holds API keys — it must never be group/world readable.
    try:
        mode = stat.S_IMODE(ENV_FILE.stat().st_mode)
        if mode & 0o077:
            os.chmod(ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)
            log.warning(
                ".env at %s had permissive mode %s; tightened to 0600",
                ENV_FILE, oct(mode),
            )
    except OSError as exc:
        log.warning("could not check/tighten .env permissions: %s", exc)
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    v = _env(name, "").lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    # core paths
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: ROOT / _env("JHH_DATA_DIR", "data"))
    uploads_dir: Path = field(default_factory=lambda: ROOT / _env("JHH_UPLOADS_DIR", "uploads"))
    resumes_dir: Path = field(default_factory=lambda: ROOT / _env("JHH_RESUMES_DIR", "resumes"))
    packets_dir: Path = field(default_factory=lambda: ROOT / _env("JHH_PACKETS_DIR", "packets"))
    cache_dir: Path = field(default_factory=lambda: ROOT / "cache")
    db_path: Path = field(default_factory=lambda: ROOT / _env("JHH_DB_PATH", "data/jhh.db"))

    # server
    host: str = field(default_factory=lambda: _env("JHH_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("JHH_PORT", 8731))

    # llm
    llm_provider: str = field(default_factory=lambda: _env("JHH_LLM_PROVIDER", "auto"))
    llm_model: str = field(default_factory=lambda: _env("JHH_LLM_MODEL", ""))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL"))
    ollama_base_url: str = field(default_factory=lambda: _env("OLLAMA_BASE_URL"))

    # embeddings
    embed_provider: str = field(default_factory=lambda: _env("JHH_EMBED_PROVIDER", "auto"))
    embed_model: str = field(default_factory=lambda: _env("JHH_EMBED_MODEL", ""))

    # job sources
    serpapi_key: str = field(default_factory=lambda: _env("SERPAPI_API_KEY"))
    searchapi_key: str = field(default_factory=lambda: _env("SEARCHAPI_API_KEY"))
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    # Optional outbound proxy for job scrapers (jobspy + httpx adapters).
    scraper_proxy: str = field(default_factory=lambda: _env("JHH_SCRAPER_PROXY"))
    # Circuit breaker: after this many consecutive adapter failures, skip the
    # adapter for `adapter_cooldown_s` seconds before trying again.
    adapter_breaker_threshold: int = field(default_factory=lambda: _int("JHH_ADAPTER_BREAKER_THRESHOLD", 3))
    adapter_cooldown_s: int = field(default_factory=lambda: _int("JHH_ADAPTER_COOLDOWN_S", 3600))

    # gmail/calendar
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    google_redirect_uri: str = field(default_factory=lambda: _env("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8731/oauth/google/callback"))
    imap_host: str = field(default_factory=lambda: _env("IMAP_HOST"))
    imap_user: str = field(default_factory=lambda: _env("IMAP_USER"))
    imap_pass: str = field(default_factory=lambda: _env("IMAP_PASS"))

    # compliance / auto-apply
    auto_apply_enabled: bool = field(default_factory=lambda: _bool("JHH_AUTO_APPLY_ENABLED", False))
    auto_apply_daily_cap: int = field(default_factory=lambda: _int("JHH_AUTO_APPLY_DAILY_CAP", 5))
    # Clamped to [0, 100]: compliance divides by 100 to get a fraction, so
    # an out-of-range env value would silently break the score gate.
    auto_apply_min_score: int = field(default_factory=lambda: max(0, min(100, _int("JHH_AUTO_APPLY_MIN_SCORE", 85))))
    default_mode: str = field(default_factory=lambda: _env("JHH_DEFAULT_MODE", "assisted"))

    def __post_init__(self) -> None:
        for d in (self.data_dir, self.uploads_dir, self.resumes_dir, self.packets_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
