"""Single-binary launcher for Job Hunt Hacker.

PyInstaller bundles this module as the entry point. At runtime it boots
uvicorn programmatically so the binary is self-contained — no need for
`python -m uvicorn ...` to be on the user's PATH.

Run:
    ./jhh                       # binds 127.0.0.1:8731
    JHH_HOST=0.0.0.0 ./jhh      # override host
    JHH_PORT=9000 ./jhh         # override port
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap_paths() -> None:
    """When running inside a PyInstaller one-folder/one-file bundle the
    extracted resources live under ``sys._MEIPASS``. Make sure that root is
    importable so ``backend.app.main`` resolves, and chdir there so the app's
    relative paths (ui/, docs/, data/seed/) still work.
    """
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        bundle_path = Path(bundle_dir)
        if str(bundle_path) not in sys.path:
            sys.path.insert(0, str(bundle_path))
        try:
            os.chdir(bundle_path)
        except OSError:
            pass
    else:
        # Plain python invocation: ensure the repo root is importable.
        here = Path(__file__).resolve().parent
        repo_root = here.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))


def main() -> int:
    _bootstrap_paths()

    host = os.environ.get("JHH_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("JHH_PORT", "8731"))
    except ValueError:
        port = 8731

    try:
        import uvicorn  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        print(f"failed to import uvicorn: {exc}", file=sys.stderr)
        return 2

    # Import lazily so PyInstaller's static analysis still picks it up via
    # the spec's `hiddenimports`, but a partial bundle reports a friendlier
    # error.
    try:
        from backend.app.main import app  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        print(f"failed to import backend.app.main: {exc}", file=sys.stderr)
        return 2

    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
