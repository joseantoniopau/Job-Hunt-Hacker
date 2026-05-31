#!/usr/bin/env python3
"""CLI: check whether a newer Job Hunt Hacker release is available.

Exit codes:
    0 = up to date (or unable to determine — fail-open)
    1 = a newer release was found

Usage:
    python scripts/check_for_updates.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _ensure_repo_on_path() -> None:
    # Allow running the script from anywhere — add the repo root so
    # `backend.app...` imports resolve without requiring `pip install -e .`.
    here = Path(__file__).resolve().parent
    repo_root = here.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check for Job Hunt Hacker updates on GitHub."
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    parser.add_argument(
        "--force", action="store_true", help="bypass the 24h cache"
    )
    args = parser.parse_args(argv)

    _ensure_repo_on_path()

    try:
        from backend.app.routers.updates import check_for_updates  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        # Couldn't even import — fail-open with a clear message.
        print(f"could not load updates module: {exc}", file=sys.stderr)
        return 0

    payload = check_for_updates(force=args.force)

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        current = payload.get("current", "?")
        latest = payload.get("latest")
        err = payload.get("error")
        if payload.get("update_available"):
            print(f"update available: {current} -> {latest}")
            url = payload.get("release_url") or ""
            if url:
                print(f"  {url}")
        elif latest:
            print(f"up to date (current {current}, latest {latest})")
        else:
            note = err or "could not determine latest version"
            print(f"current version: {current} ({note})")

    return 1 if payload.get("update_available") else 0


if __name__ == "__main__":
    raise SystemExit(main())
