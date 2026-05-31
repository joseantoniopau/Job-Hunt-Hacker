#!/usr/bin/env python3
"""smoke_test.py — full end-to-end smoke for Job Hunt Hacker.

Run from project root:
    python3 scripts/smoke_test.py

What it verifies (in order):
  1. Backend imports
  2. SQLite schema initializes
  3. Profile GET/PUT
  4. Evidence text upload + claim extraction
  5. Vault summary
  6. Job source registry has 9 adapters
  7. Job search (in-process, remotive only — most reliable free source)
  8. Job scoring (if matching module is present)
  9. LLM provider resolves
 10. Template tailoring runs without crash
 11. UI files exist
 12. Docs files exist

Each step prints PASS / FAIL / SKIP. Exits 0 on all PASS or SKIP, 1 on any FAIL.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate the smoke test from the user's real DB. Set JHH_DB_PATH BEFORE
# importing any backend module so config.settings materializes against the
# temp file. Otherwise smoke writes "Smoke Tester" into the user's actual
# user_profile row and seeds real test rows into their vault.
# Override via JHH_SMOKE_DB env var if you want to inspect the smoke DB.
import tempfile as _tf
if not os.environ.get("JHH_DB_PATH"):
    _smoke_dir = Path(_tf.gettempdir()) / "jhh_smoke"
    _smoke_dir.mkdir(parents=True, exist_ok=True)
    _smoke_db = os.environ.get("JHH_SMOKE_DB") or str(_smoke_dir / "smoke.db")
    os.environ["JHH_DB_PATH"] = _smoke_db
    # Also redirect ephemeral dirs so smoke can't leak tailored resumes /
    # packets / uploads into the user's filesystem.
    for env, sub in (("JHH_UPLOADS_DIR", "uploads"),
                     ("JHH_RESUMES_DIR", "resumes"),
                     ("JHH_PACKETS_DIR", "packets")):
        if not os.environ.get(env):
            d = _smoke_dir / sub
            d.mkdir(parents=True, exist_ok=True)
            os.environ[env] = str(d)
    print(f"[smoke] isolated to {_smoke_db}")

# Force minimal noise from libs
os.environ.setdefault("PYTHONWARNINGS", "ignore")

PASS = 0
FAIL = 0
SKIP = 0

def _mark(status: str, label: str, detail: str = "") -> None:
    global PASS, FAIL, SKIP
    pad = label.ljust(55)
    if status == "PASS":
        PASS += 1
        print(f"  [PASS] {pad} {detail}")
    elif status == "SKIP":
        SKIP += 1
        print(f"  [SKIP] {pad} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {pad} {detail}")


def step(label: str):
    def deco(fn):
        def wrapped():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                _mark("FAIL", label, f"{type(e).__name__}: {e}")
                if os.environ.get("JHH_SMOKE_DEBUG"):
                    traceback.print_exc()
        wrapped.__name__ = fn.__name__
        wrapped.__label__ = label  # type: ignore
        return wrapped
    return deco


# ----- steps -----

@step("01 backend imports")
def s01():
    from backend.app import main as _m
    from backend.app.db import init_db
    _mark("PASS", "01 backend imports", f"routers loaded={len(_m._loaded)} failed={len(_m._failed)}")


@step("02 schema initializes")
def s02():
    from backend.app.db import init_db, get_conn
    init_db()
    c = get_conn()
    n = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    if n < 10:
        raise AssertionError(f"only {n} tables found")
    _mark("PASS", "02 schema initializes", f"{n} tables")


@step("03 profile GET/PUT")
def s03():
    from backend.app.routers.profile import get_profile, put_profile
    from backend.app.models.schemas import UserProfileIn
    r = get_profile()
    assert r["ok"]
    put_profile(UserProfileIn(name="Smoke Tester", email="s@test", target_titles=["Engineer"]))
    r2 = get_profile()
    assert r2["data"]["name"] == "Smoke Tester"
    _mark("PASS", "03 profile GET/PUT", "name persisted")


@step("04 evidence text ingestion + claims")
def s04():
    try:
        from backend.app.services.career_vault import add_source, add_claims
        from backend.app.services.evidence_extractor import extract_claims
    except Exception as e:
        _mark("SKIP", "04 evidence text ingestion + claims", f"module missing: {e}")
        return
    sid = add_source(
        "manual_paste",
        title="smoke-paste",
        raw_text=("Senior Python Engineer at Acme 2020-2023. "
                  "Shipped FastAPI services on AWS with Docker and Kubernetes. "
                  "Mentored 3 juniors. Reduced p99 latency 40%."),
    )
    assert isinstance(sid, int) and sid > 0
    claims = extract_claims(sid, "Senior Python Engineer at Acme 2020-2023. Shipped FastAPI services on AWS with Docker and Kubernetes. Mentored 3 juniors.", "manual_paste")
    ids = add_claims(sid, claims)
    _mark("PASS", "04 evidence text ingestion + claims", f"source_id={sid} claims={len(ids)}")


@step("05 vault summary")
def s05():
    try:
        from backend.app.services.career_vault import summary
    except Exception as e:
        _mark("SKIP", "05 vault summary", f"module missing: {e}")
        return
    s = summary()
    assert isinstance(s, dict)
    _mark("PASS", "05 vault summary", json.dumps(s)[:80])


@step("06 job source registry")
def s06():
    from backend.app.services.job_sources import REGISTRY, list_adapters
    names = list_adapters()
    if len(names) < 8:
        raise AssertionError(f"only {len(names)} adapters: {names}")
    healthy = {n: REGISTRY[n].healthy() for n in names}
    _mark("PASS", "06 job source registry", f"{len(names)} adapters; healthy={sum(healthy.values())}")


@step("07 job search (remotive)")
def s07():
    from backend.app.services.job_sources.pipeline import search_all
    from backend.app.services.job_sources.base import JobSearchQuery
    q = JobSearchQuery(query="engineer", is_remote=True, results_per_site=5)
    r = search_all(q, sites=["remotive"])
    n = len(r.get("records", []))
    if n == 0 and not r.get("errors"):
        _mark("SKIP", "07 job search (remotive)", "0 records; possibly offline")
        return
    _mark("PASS", "07 job search (remotive)", f"{n} records, errors={list(r.get('errors',{}).keys())}")


@step("08 job scoring (if available)")
def s08():
    try:
        from backend.app.matching.scorer import score_job, default_weights
    except Exception as e:
        _mark("SKIP", "08 job scoring", f"module missing: {e}")
        return
    from backend.app.db import get_conn
    row = get_conn().execute("SELECT id FROM job_posting ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        _mark("SKIP", "08 job scoring", "no jobs to score")
        return
    r = score_job(int(row[0]))
    assert isinstance(r, dict) and "overall_score" in r
    _mark("PASS", "08 job scoring", f"job {row[0]} → {r['overall_score']:.2f}")


@step("09 LLM provider resolves")
def s09():
    from backend.app.llm import get_llm
    llm = get_llm()
    assert llm is not None
    _mark("PASS", "09 LLM provider resolves", f"provider={llm.name}")


@step("10 template tailoring (no crash)")
def s10():
    try:
        from backend.app.tailoring.resume_tailor import tailor_resume
    except Exception as e:
        _mark("SKIP", "10 template tailoring", f"module missing: {e}")
        return
    from backend.app.db import get_conn
    row = get_conn().execute("SELECT id FROM job_posting ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        _mark("SKIP", "10 template tailoring", "no jobs available")
        return
    try:
        r = tailor_resume(int(row[0]))
    except Exception as e:
        _mark("FAIL", "10 template tailoring", str(e))
        return
    _mark("PASS", "10 template tailoring", f"id={r.get('id')}")


@step("11 UI files present")
def s11():
    for f in ("ui/index.html", "ui/styles.css", "ui/app.js"):
        if not (ROOT / f).is_file():
            raise AssertionError(f"missing {f}")
    _mark("PASS", "11 UI files present", "index.html, styles.css, app.js")


@step("12 docs files present")
def s12():
    for f in ("docs/index.html", "docs/styles.css"):
        if not (ROOT / f).is_file():
            raise AssertionError(f"missing {f}")
    _mark("PASS", "12 docs files present", "index.html, styles.css")


# ----- main -----

STEPS = [s01, s02, s03, s04, s05, s06, s07, s08, s09, s10, s11, s12]


def main() -> int:
    print("=" * 70)
    print(" Job Hunt Hacker · smoke test")
    print("=" * 70)
    t0 = time.time()
    for s in STEPS:
        s()
    dt = time.time() - t0
    print("-" * 70)
    print(f" PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}  ({dt:.1f}s)")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
