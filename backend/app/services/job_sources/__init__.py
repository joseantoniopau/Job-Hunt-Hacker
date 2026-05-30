"""Registry of job source adapters."""
from .base import JobSourceAdapter, JobSearchQuery, JobRecord, SourcePolicy, REGISTRY

# Import each adapter so they self-register; tolerate missing optional deps.
for _name in ("jobspy_adapter", "remoteintech_adapter", "greenhouse_adapter",
              "lever_adapter", "ashby_adapter", "remotive_adapter",
              "weworkremotely_adapter", "google_jobs_adapter", "custom_rss_adapter"):
    try:
        __import__(f"backend.app.services.job_sources.{_name}", fromlist=["*"])
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger("jhh.sources").warning("adapter %s skipped: %s", _name, e)


def get_adapter(name: str) -> JobSourceAdapter | None:
    return REGISTRY.get(name)


def list_adapters() -> list[str]:
    return sorted(REGISTRY.keys())


__all__ = [
    "JobSourceAdapter", "JobSearchQuery", "JobRecord", "SourcePolicy",
    "REGISTRY", "get_adapter", "list_adapters",
]
