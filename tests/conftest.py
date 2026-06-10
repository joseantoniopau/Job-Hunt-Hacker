"""pytest configuration: redirect the SQLite vault to a temporary file BEFORE
the backend imports happen, so tests never touch the user's production
`data/jhh.db`.

Without this, every `pytest` run inserts fixture rows into the user's real
career vault, polluting the Setup/Vault pages. The fix is run-once and
applies to the whole session.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _isolate_db() -> None:
    """Point JHH_DB_PATH at a temp file. Must run BEFORE backend.app.config
    materializes the `settings` singleton, which means BEFORE any backend
    module is imported. pytest loads conftest.py before any test module,
    which is exactly when we want this to fire.
    """
    # If the env var is already set (e.g. CI provides its own), respect it.
    if os.environ.get("JHH_DB_PATH"):
        return
    tmp_dir = Path(tempfile.gettempdir()) / "jhh_pytest"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # New file per session — pytest's tempdir cleanup leaves it for inspection
    db_file = tmp_dir / f"jhh_test_{os.getpid()}.db"
    os.environ["JHH_DB_PATH"] = str(db_file)
    # Also relocate the uploads / packets / resumes directories so tests
    # can't leak artifacts into the production filesystem.
    # NOTE: We deliberately DO NOT override JHH_DATA_DIR — the data/ tree
    # also holds the seed/ subdir (ats_keywords.json, source_policies.json,
    # etc.) which compliance/keyword/scoring code reads at runtime. Moving
    # data_dir would orphan that read-only reference data.
    for env, sub in (("JHH_UPLOADS_DIR", "uploads"),
                     ("JHH_RESUMES_DIR", "resumes"),
                     ("JHH_PACKETS_DIR", "packets")):
        if not os.environ.get(env):
            d = tmp_dir / sub
            d.mkdir(parents=True, exist_ok=True)
            os.environ[env] = str(d)


def _isolate_llm() -> None:
    """Force the template LLM provider for tests unless the runner opts in
    to live inference with JHH_TEST_LIVE_LLM=1.

    Without this, a developer whose .env points at a real provider (e.g.
    JHH_LLM_PROVIDER=ollama with a 70B model and a 12-minute timeout) gets
    live inference inside unit tests — the suite slows from ~2 minutes to
    hours and assertions depend on nondeterministic model output.
    """
    if os.environ.get("JHH_TEST_LIVE_LLM") == "1":
        return
    os.environ["JHH_LLM_PROVIDER"] = "template"


_isolate_db()
_isolate_llm()
