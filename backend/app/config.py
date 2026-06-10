"""Central config. Reads .env once; everything else imports `settings`."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"

# Single source of truth for the app version — main.py (FastAPI metadata,
# which /api/updates/check reads), /api/settings, and data exports all
# reference this.
APP_VERSION = "0.4.0"


def _load_env_file() -> None:
    if not ENV_FILE.exists():
        return
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
