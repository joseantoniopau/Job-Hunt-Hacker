"""FastAPI app. Routers are imported lazily so import failures in one
module don't take down the whole server.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse

from .config import APP_VERSION, settings
from .db import init_db, audit

log = logging.getLogger("jhh")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- startup ----
    init_db()
    audit("server_start", "system")
    log.info("Job Hunt Hacker ready at http://%s:%d", settings.host, settings.port)
    try:
        from .integrations import scheduler as _sched
        _sched.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduler failed to start: %s", exc)
    yield
    # ---- shutdown ----
    try:
        from .integrations import scheduler as _sched
        _sched.shutdown()
    except Exception:
        pass


app = FastAPI(
    title="Job Hunt Hacker",
    description="Find better jobs. Tailor honestly. Apply with discipline.",
    version=APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security hardening: optional bearer-token auth + rate-limit handler.
# Both degrade to no-ops when their optional deps / env vars are missing.
try:
    from .security.auth import BearerTokenMiddleware
    app.add_middleware(BearerTokenMiddleware)
except Exception as exc:  # noqa: BLE001
    log.warning("auth middleware failed to install: %s", exc)

try:
    from .security.rate_limit import install_rate_limit_handler
    install_rate_limit_handler(app)
except Exception as exc:  # noqa: BLE001
    log.warning("rate-limit handler failed to install: %s", exc)

# Observability: request-id middleware + prometheus metrics middleware +
# structured logging. All degrade to no-op when optional deps are missing.
try:
    from .middleware.structured_logging import configure_logging
    configure_logging()
except Exception as exc:  # noqa: BLE001
    log.warning("structured logging init failed: %s", exc)

try:
    from .middleware.request_id import RequestIDMiddleware
    app.add_middleware(RequestIDMiddleware)
except Exception as exc:  # noqa: BLE001
    log.warning("request-id middleware failed to install: %s", exc)

try:
    from .middleware.metrics import PrometheusMiddleware
    app.add_middleware(PrometheusMiddleware)
except Exception as exc:  # noqa: BLE001
    log.warning("metrics middleware failed to install: %s", exc)


# --- routers (import each in try/except so failures degrade gracefully) ---

ROUTER_MODULES = [
    "autopilot",
    "profile",
    "evidence",
    "vault",
    "search",
    "jobs",
    "resume",
    "resume_iterate",
    "cover_letter",
    "recruiter",
    "applications",
    "email",
    "calendar",
    "settings",
    "llm",
    "llm_rerank",
    "github",
    "urls",
    "scheduler",
    "auto_apply",
    "stats",
    "data",
    "audit",
    "metrics",
    "gaps",
    "effectiveness",
    "bulk",
    # ---- Headhunter mode ----
    "salary",
    "companies",
    "connections",
    "velocity",
    "negotiation",
    "offer_analysis",
    "interview_prep",
    "followups",
    # ---- Operational ----
    "updates",
]

_loaded: list[str] = []
_failed: dict[str, str] = {}

for name in ROUTER_MODULES:
    try:
        mod = __import__(f"backend.app.routers.{name}", fromlist=["router"])
        app.include_router(mod.router)
        _loaded.append(name)
    except Exception as exc:  # noqa: BLE001
        _failed[name] = f"{type(exc).__name__}: {exc}"
        log.warning("router %s failed to load: %s", name, exc)
        log.debug(traceback.format_exc())


@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "version": app.version,
        "routers_loaded": _loaded,
        "routers_failed": _failed,
        "data_dir": str(settings.data_dir),
        "auto_apply_enabled": settings.auto_apply_enabled,
        "default_mode": settings.default_mode,
    }


# --- UI (static brutalist HTML/CSS/JS) ---

UI_DIR = Path(__file__).resolve().parents[2] / "ui"


@app.get("/")
def root() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


@app.get("/styles.css")
def styles() -> FileResponse:
    return FileResponse(UI_DIR / "styles.css")


@app.get("/app.js")
def app_js() -> FileResponse:
    return FileResponse(UI_DIR / "app.js")


# Catch-all 404 — but preserve the route-level detail when a handler
# explicitly raised HTTPException(404). Without this guard, "job 999 not
# found" gets clobbered by the generic "not found: /api/jobs/999" message.
@app.exception_handler(404)
async def not_found(request, exc):  # type: ignore[no-untyped-def]
    detail = getattr(exc, "detail", None)
    if detail and str(detail).strip().lower() not in ("not found", ""):
        return JSONResponse(status_code=404, content={"ok": False, "detail": str(detail)})
    return JSONResponse(status_code=404, content={"ok": False, "detail": f"not found: {request.url.path}"})
