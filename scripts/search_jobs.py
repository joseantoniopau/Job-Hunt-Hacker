#!/usr/bin/env python3
"""CLI: search jobs across sources and print a monospace table.

Examples:
    python scripts/search_jobs.py --query "Senior PM" --location Remote \\
        --sites indeed,google --results 25
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _truncate(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--query", "-q", required=True)
    ap.add_argument("--location", "-l", default="")
    ap.add_argument("--remote", action="store_true")
    ap.add_argument("--sites", default="indeed,google,glassdoor,remotive,greenhouse")
    ap.add_argument("--results", type=int, default=25)
    ap.add_argument("--hours-old", type=int, default=168)
    ap.add_argument("--country", default="usa")
    ap.add_argument("--top", type=int, default=40, help="Max rows to print")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s | %(message)s")

    from backend.app.services.job_sources import REGISTRY  # noqa: F401  (trigger registration)
    from backend.app.services.job_sources.base import JobSearchQuery
    from backend.app.services.job_sources.pipeline import search_all

    sites_in = [s.strip() for s in args.sites.split(",") if s.strip()]

    jobspy_sites = {"indeed", "glassdoor", "google", "linkedin",
                    "zip_recruiter", "bayt", "naukri", "bdjobs"}
    requested: list[str] = []
    if any(s in jobspy_sites for s in sites_in):
        requested.append("jobspy")
    for s in sites_in:
        from backend.app.services.job_sources import REGISTRY as _REG
        if s in _REG and s not in requested:
            requested.append(s)

    q = JobSearchQuery(
        query=args.query,
        location=args.location or None,
        is_remote=bool(args.remote),
        results_per_site=int(args.results),
        hours_old=int(args.hours_old),
        country=args.country,
        extra={"sites": sites_in},
    )
    res = search_all(q, requested)
    recs = res["records"]

    cols = ("#", "SOURCE", "COMPANY", "TITLE", "LOCATION")
    widths = (3, 20, 22, 50, 22)
    sep = " | "
    print(sep.join(c.ljust(w) for c, w in zip(cols, widths)))
    print(sep.join("-" * w for w in widths))
    for i, r in enumerate(recs[: args.top], 1):
        row = (
            str(i),
            _truncate(r.source, widths[1]),
            _truncate(r.company, widths[2]),
            _truncate(r.title, widths[3]),
            _truncate(r.location, widths[4]),
        )
        print(sep.join(c.ljust(w) for c, w in zip(row, widths)))
    print()
    print(f"discovered={len(recs)} per_source={res['per_source']} errors={res['errors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
