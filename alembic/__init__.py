"""Alembic migration root for Job Hunt Hacker.

This is an *optional* alternative to the implicit ``_init_schema`` /
``_ensure_column`` bootstrap inside ``backend/app/db.py``. The default
runtime still uses that bootstrap so that fresh installs need no manual
``alembic upgrade``; this package is here for users who want a tracked
migration history.

Why this file shims the installed alembic package
-------------------------------------------------
Alembic's recommended layout puts migrations under a directory literally
named ``alembic/``. When the project root is on ``sys.path`` (which is the
default for ``python -c`` invocations from the repo root, and which pytest
does as well), Python's import system finds *this* directory first and
shadows the installed ``alembic`` distribution, breaking
``from alembic.config import Config``.

To stay backward-compatible with the task contract that explicitly asks for
``alembic/__init__.py`` to exist while keeping ``import alembic`` functional,
we relocate this module out of the way and re-export the installed package
under the canonical ``alembic`` name. The migrations directory continues
to live at ``alembic/versions/`` on disk; alembic's CLI resolves those by
filesystem path (``script_location``), so the script discovery is unaffected.
"""
from __future__ import annotations

import importlib.util as _importlib_util
import os as _os
import sys as _sys
from pathlib import Path as _Path

# Find the *installed* alembic distribution by walking sys.path entries that
# are NOT this project directory.
_this_dir = _Path(__file__).resolve().parent
_project_root = _this_dir.parent


def _locate_real_alembic() -> _Path | None:
    for entry in _sys.path:
        if not entry:
            continue
        try:
            entry_path = _Path(entry).resolve()
        except Exception:  # noqa: BLE001
            continue
        # Skip the project root — that's *us*.
        if entry_path == _project_root:
            continue
        candidate = entry_path / "alembic" / "__init__.py"
        if candidate.exists():
            return candidate
    return None


_real_init = _locate_real_alembic()
if _real_init is not None and str(_real_init) != __file__:
    _spec = _importlib_util.spec_from_file_location(
        "alembic",
        str(_real_init),
        submodule_search_locations=[str(_real_init.parent)],
    )
    if _spec is not None and _spec.loader is not None:
        _module = _importlib_util.module_from_spec(_spec)
        # Swap ourselves out so any subsequent `import alembic` gets the real one.
        _sys.modules[__name__] = _module
        _spec.loader.exec_module(_module)  # type: ignore[union-attr]
