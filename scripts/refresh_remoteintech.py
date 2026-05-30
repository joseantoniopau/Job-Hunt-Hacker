#!/usr/bin/env python3
"""Refresh data/seed/companies_remoteintech.json from the remoteintech repo.

Usage:
    python scripts/refresh_remoteintech.py [--limit N] [--quiet]

Idempotent. If GitHub rate-limits or the network is unreachable, leaves the
existing seed file in place.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make `backend.app...` importable when running directly.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="Max companies to fetch")
    ap.add_argument("--quiet", action="store_true", help="Suppress info logs")
    args = ap.parse_args()

    level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(name)s | %(message)s")

    from backend.app.services.job_sources.pipeline import refresh_remoteintech

    n = refresh_remoteintech(limit=args.limit, quiet=args.quiet)
    if not args.quiet:
        print(f"remoteintech seed: {n} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
