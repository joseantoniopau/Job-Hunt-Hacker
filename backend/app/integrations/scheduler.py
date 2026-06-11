"""APScheduler wrapper. Idempotent registration.

Without APScheduler installed, exposes the same API but as no-ops.
"""
from __future__ import annotations

import functools
import json
import logging
import time
from typing import Any, Optional

from ..config import settings
from ..db import audit, get_conn, row_to_dict, tx

log = logging.getLogger("jhh.integrations.scheduler")

try:
    from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore
    from apscheduler.triggers.cron import CronTrigger  # type: ignore
    from apscheduler.triggers.interval import IntervalTrigger  # type: ignore
    _HAS_APS = True
except Exception as _exc:  # noqa: BLE001
    BackgroundScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    IntervalTrigger = None  # type: ignore
    _HAS_APS = False
    log.warning("APScheduler not installed: %s", _exc)


_scheduler: Optional[Any] = None
_started: bool = False
_INBOX_JOB_ID = "jhh.inbox_sweep"
_FOLLOWUP_JOB_ID = "jhh.followups"
_DIGEST_JOB_ID = "jhh.daily_digest"


# ---------- module-owned schema (kept here, not db.py, by ownership) ----------

def _ensure_scheduler_schema() -> None:
    """Create the job-run history table + user timezone column. Idempotent;
    safe to call on every import / start()."""
    try:
        conn = get_conn()
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scheduler_job_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                finished_at REAL,
                status TEXT NOT NULL,
                error TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobrun_job ON scheduler_job_run(job_id, started_at DESC)"
        )
        from ..db import _ensure_column
        _ensure_column(conn, "user_profile", "timezone", "TEXT")
    except Exception as exc:  # noqa: BLE001
        log.debug("scheduler schema ensure failed: %s", exc)


def _record_run(job_id: str, fn):
    """Wrap a scheduled job: record start/finish/status/error into
    scheduler_job_run so status() can surface health, and trim history to
    the last 50 runs per job. Never lets bookkeeping swallow the result."""
    started = time.time()
    status = "ok"
    error = None
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        log.warning("scheduled job %s failed: %s", job_id, error)
        raise
    finally:
        try:
            with tx() as c:
                c.execute(
                    "INSERT INTO scheduler_job_run (job_id, started_at, finished_at, status, error) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (job_id, started, time.time(), status, error),
                )
                c.execute(
                    "DELETE FROM scheduler_job_run WHERE job_id = ? AND id NOT IN "
                    "(SELECT id FROM scheduler_job_run WHERE job_id = ? ORDER BY id DESC LIMIT 50)",
                    (job_id, job_id),
                )
        except Exception:
            pass


def _get_scheduler() -> Optional[Any]:
    global _scheduler
    if not _HAS_APS:
        return None
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="UTC")
    return _scheduler


def is_running() -> bool:
    s = _get_scheduler()
    return bool(s and getattr(s, "running", False))


def start() -> None:
    global _started
    s = _get_scheduler()
    if s is None:
        log.info("scheduler unavailable (apscheduler missing)")
        return
    if _started or getattr(s, "running", False):
        return
    try:
        _ensure_scheduler_schema()
        s.start(paused=False)
        _started = True
        # standing jobs
        _register_inbox_sweep()
        _register_followups()
        _register_daily_digest()
        register_audit_retention()
        register_email_calendar_retention()
        register_db_maintenance()
        register_saved_searches()
        register_deadline_reminders()
        try:
            audit("scheduler_start", "system", jobs=[j.id for j in s.get_jobs()])
        except Exception:
            pass
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduler.start failed: %s", exc)


def shutdown() -> None:
    s = _get_scheduler()
    if s is None or not getattr(s, "running", False):
        return
    try:
        s.shutdown(wait=False)
    except Exception:
        pass


# ---------- saved searches ----------

def list_saved_searches() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM saved_search ORDER BY id DESC"
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def create_saved_search(label: str, query: dict, frequency_hours: int = 24, enabled: bool = True) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO saved_search (label, query_json, frequency_hours, enabled, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (label, json.dumps(query), int(frequency_hours), 1 if enabled else 0, time.time()),
        )
        sid = int(cur.lastrowid)
    try:
        audit("saved_search_create", "saved_search", sid, label=label, frequency_hours=frequency_hours)
    except Exception:
        pass
    # auto-register if scheduler running
    if is_running():
        _register_one_saved_search(sid, label, query, int(frequency_hours))
    return sid


def update_saved_search(saved_search_id: int, *, enabled: bool | None = None,
                        frequency_hours: int | None = None) -> dict | None:
    """Update a saved search's enabled flag and/or cadence, keeping the
    APScheduler job in sync: disable removes the job, enable (re)registers
    it, and a cadence change reschedules. Returns the updated row, or
    None if the id doesn't exist."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM saved_search WHERE id = ?", (int(saved_search_id),)
    ).fetchone()
    if not row:
        return None
    rec = row_to_dict(row)

    new_enabled = rec.get("enabled") if enabled is None else (1 if enabled else 0)
    new_freq = int(rec.get("frequency_hours") or 24) if frequency_hours is None else int(frequency_hours)
    with tx() as c:
        c.execute(
            "UPDATE saved_search SET enabled = ?, frequency_hours = ? WHERE id = ?",
            (new_enabled, new_freq, int(saved_search_id)),
        )
    try:
        audit("saved_search_update", "saved_search", int(saved_search_id),
              enabled=bool(new_enabled), frequency_hours=new_freq)
    except Exception:
        pass

    job_id = f"saved_search_{int(saved_search_id)}"
    s = _get_scheduler()
    if s is not None:
        if not new_enabled:
            try:
                if s.get_job(job_id):
                    s.remove_job(job_id)
            except Exception:
                pass
        elif is_running():
            query = rec.get("query_json") or {}
            if isinstance(query, str):
                try:
                    query = json.loads(query)
                except Exception:
                    query = {}
            # replace_existing=True in the register helper makes this a
            # reschedule when the job already exists.
            _register_one_saved_search(int(saved_search_id), rec.get("label") or "",
                                       query, new_freq)

    rec["enabled"] = new_enabled
    rec["frequency_hours"] = new_freq
    return rec


def delete_saved_search(saved_search_id: int) -> bool:
    s = _get_scheduler()
    job_id = f"saved_search_{int(saved_search_id)}"
    if s is not None:
        try:
            if s.get_job(job_id):
                s.remove_job(job_id)
        except Exception:
            pass
    with tx() as conn:
        cur = conn.execute("DELETE FROM saved_search WHERE id = ?", (int(saved_search_id),))
        ok = cur.rowcount > 0
    if ok:
        try:
            audit("saved_search_delete", "saved_search", int(saved_search_id))
        except Exception:
            pass
    return ok


def _run_saved_search(saved_search_id: int) -> dict:
    """Execute one saved search: search + persist + score new ids.

    Any failure (raised exception, pipeline import error, ...) is recorded
    on the row as last_error/last_error_ts and audited as
    `saved_search_failed`; a successful run clears last_error.
    """
    conn = get_conn()
    row = conn.execute("SELECT * FROM saved_search WHERE id = ?", (int(saved_search_id),)).fetchone()
    if not row:
        return {"ok": False, "detail": f"saved_search {saved_search_id} not found"}
    rec = row_to_dict(row)
    if not rec.get("enabled"):
        return {"ok": False, "detail": "disabled"}
    try:
        return _execute_saved_search(int(saved_search_id), rec)
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        log.warning("saved_search %s failed: %s", saved_search_id, err)
        try:
            with tx() as c:
                c.execute(
                    "UPDATE saved_search SET last_error = ?, last_error_ts = ? WHERE id = ?",
                    (err, time.time(), int(saved_search_id)),
                )
        except Exception:
            pass
        try:
            audit("saved_search_failed", "saved_search", int(saved_search_id), error=err)
        except Exception:
            pass
        return {"ok": False, "saved_search_id": int(saved_search_id), "detail": err}


def _build_saved_search_query(rec: dict, results_cap: int | None = None):
    """Build (JobSearchQuery, requested_adapters) from a saved_search row.
    Shared by the live run and the dry-run preview."""
    query = rec.get("query_json") or {}
    if isinstance(query, str):
        try:
            query = json.loads(query)
        except Exception:
            query = {}

    from ..services.job_sources.base import JobSearchQuery
    from ..services.job_sources import REGISTRY

    sites = query.get("sites") or []
    requested: list[str] = []
    jobspy_sites = {"indeed", "glassdoor", "google", "linkedin", "zip_recruiter"}
    if any(s in jobspy_sites for s in sites):
        requested.append("jobspy")
    for s in sites:
        if s in REGISTRY and s not in requested:
            requested.append(s)
    if not requested:
        requested = list(REGISTRY.keys())

    rps = int(query.get("results_per_site") or 25)
    if results_cap is not None:
        rps = min(rps, int(results_cap))
    # extra.sites is read by the jobspy adapter and must contain SCRAPER
    # names (indeed/google/...), not adapter ids — passing adapter ids
    # ("jobspy", "greenhouse") filters to an empty list inside the adapter,
    # which then silently returns no results. An empty list here lets the
    # adapter fall back to its default scraper set.
    q = JobSearchQuery(
        query=query.get("query") or "",
        location=query.get("location"),
        is_remote=bool(query.get("is_remote")),
        results_per_site=rps,
        hours_old=query.get("hours_old"),
        country=query.get("country") or "usa",
        employment_type=query.get("employment_type"),
        distance=query.get("distance"),
        extra={"sites": [s for s in sites if s in jobspy_sites]},
    )
    return q, requested


def dry_run_saved_search(saved_search_id: int, results_cap: int = 5) -> dict:
    """Run a saved search's query WITHOUT persisting — a cheap preview so the
    user can see what a search would pull before scheduling it. Counts how
    many would be inserted vs. skipped as duplicates against the live vault,
    and returns the first few hits. Never writes job rows."""
    row = get_conn().execute(
        "SELECT * FROM saved_search WHERE id = ?", (int(saved_search_id),)
    ).fetchone()
    if not row:
        return {"ok": False, "detail": f"saved_search {saved_search_id} not found"}
    rec = row_to_dict(row)
    from ..services.job_sources.pipeline import (
        search_all, _find_cross_source_duplicate, _CROSS_SOURCE_DEDUP_WINDOW_S,
    )
    q, requested = _build_saved_search_query(rec, results_cap=results_cap)
    sres = search_all(q, requested)
    records = sres.get("records") or []
    conn = get_conn()
    seen: set[str] = set()
    would_insert = 0
    duplicates = 0
    cutoff = time.time() - _CROSS_SOURCE_DEDUP_WINDOW_S
    for rrec in records:
        h = rrec.hash()
        if h in seen:
            duplicates += 1
            continue
        seen.add(h)
        existing = conn.execute("SELECT 1 FROM job_posting WHERE hash = ?", (h,)).fetchone()
        if existing or _find_cross_source_duplicate(conn, rrec, cutoff) is not None:
            duplicates += 1
        else:
            would_insert += 1
    top = [{"title": r.title, "company": r.company, "url": r.apply_url or r.company_url}
           for r in records[:5]]
    return {
        "ok": True,
        "saved_search_id": int(saved_search_id),
        "would_insert": would_insert,
        "duplicates": duplicates,
        "discovered": len(records),
        "top": top,
        "per_source": sres.get("per_source"),
        "errors": sres.get("errors"),
    }


def _execute_saved_search(saved_search_id: int, rec: dict) -> dict:
    """Body of one saved-search run. Raises on failure — _run_saved_search
    owns the error bookkeeping."""
    from ..services.job_sources.pipeline import persist, search_all

    q, requested = _build_saved_search_query(rec)
    sres = search_all(q, requested)
    pres = persist(sres.get("records") or [])

    # score
    scored = 0
    try:
        from ..matching import scorer  # type: ignore
        if hasattr(scorer, "score_job"):
            for jid in pres.get("ids") or []:
                try:
                    # llm_polish=False: a 20-job batch must not make 20
                    # local-LLM round-trips just to prettify explanations.
                    scorer.score_job(int(jid), llm_polish=False)
                    scored += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("score_job(%s) failed: %s", jid, exc)
    except Exception:
        pass

    with tx() as conn:
        conn.execute(
            "UPDATE saved_search SET last_run_at = ?, last_error = NULL, last_error_ts = NULL "
            "WHERE id = ?",
            (time.time(), int(saved_search_id)),
        )
    try:
        audit("saved_search_run", "saved_search", int(saved_search_id),
              inserted=pres.get("inserted"), scored=scored, per_source=sres.get("per_source"))
    except Exception:
        pass
    return {
        "ok": True,
        "saved_search_id": int(saved_search_id),
        "inserted": pres.get("inserted"),
        "duplicates": pres.get("duplicates"),
        "scored": scored,
        "per_source": sres.get("per_source"),
        "errors": sres.get("errors"),
    }


def run_saved_search_now(saved_search_id: int) -> dict:
    return _run_saved_search(int(saved_search_id))


def _run_saved_search_recorded(saved_search_id: int) -> dict:
    """Scheduled entrypoint — records the run in scheduler_job_run under the
    per-search job id. Manual run_now bypasses recording (it's user-driven)."""
    sid = int(saved_search_id)
    return _record_run(f"saved_search_{sid}", lambda: _run_saved_search(sid))


def _register_one_saved_search(sid: int, label: str, query: Any, frequency_hours: int) -> bool:
    s = _get_scheduler()
    if s is None:
        return False
    job_id = f"saved_search_{int(sid)}"
    try:
        if s.get_job(job_id):
            s.remove_job(job_id)
    except Exception:
        pass
    try:
        s.add_job(
            _run_saved_search_recorded,
            trigger=IntervalTrigger(hours=max(1, int(frequency_hours))),
            args=[int(sid)],
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("could not register %s: %s", job_id, exc)
        return False


def register_saved_searches() -> int:
    s = _get_scheduler()
    if s is None:
        return 0
    n = 0
    for ss in list_saved_searches():
        if not ss.get("enabled"):
            continue
        if _register_one_saved_search(int(ss["id"]), ss.get("label") or "", ss.get("query_json"), int(ss.get("frequency_hours") or 24)):
            n += 1
    return n


def unregister_all_saved_search_jobs() -> int:
    """Remove every APScheduler job whose ID starts with `saved_search_`.

    Called from DELETE /api/data after the saved_search table is wiped so
    no cron jobs fire against missing DB rows.
    """
    s = _get_scheduler()
    if s is None:
        return 0
    removed = 0
    try:
        for job in list(s.get_jobs()):
            if (job.id or "").startswith("saved_search_"):
                try:
                    s.remove_job(job.id)
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    return removed


# ---------- inbox sweep ----------

def run_inbox_sweep() -> dict:
    out: dict = {"gmail": None, "imap": None}
    try:
        from . import gmail as _gmail
        out["gmail"] = _gmail.ingest_all()
    except Exception as exc:  # noqa: BLE001
        out["gmail"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    try:
        from . import imap as _imap
        out["imap"] = _imap.ingest_all()
    except Exception as exc:  # noqa: BLE001
        out["imap"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    try:
        audit("inbox_sweep_run", "system", **out)
    except Exception:
        pass
    return out


def _register_inbox_sweep() -> None:
    s = _get_scheduler()
    if s is None:
        return
    try:
        if s.get_job(_INBOX_JOB_ID):
            s.remove_job(_INBOX_JOB_ID)
    except Exception:
        pass
    try:
        s.add_job(
            functools.partial(_record_run, _INBOX_JOB_ID, run_inbox_sweep),
            trigger=CronTrigger(hour=7, minute=0),
            id=_INBOX_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not register inbox sweep: %s", exc)


# ---------- followups ----------

def run_followups() -> dict:
    try:
        from ..applications.pipeline import find_followups_due
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"applications.pipeline unavailable: {exc}"}
    due = find_followups_due()
    for app in due:
        try:
            audit(
                "followup_due",
                "application",
                int(app["id"]),
                company=app.get("job_company"),
                title=app.get("job_title"),
            )
        except Exception:
            pass
    return {"ok": True, "due_count": len(due), "due": [int(a["id"]) for a in due]}


def _register_followups() -> None:
    s = _get_scheduler()
    if s is None:
        return
    try:
        if s.get_job(_FOLLOWUP_JOB_ID):
            s.remove_job(_FOLLOWUP_JOB_ID)
    except Exception:
        pass
    try:
        s.add_job(
            functools.partial(_record_run, _FOLLOWUP_JOB_ID, run_followups),
            trigger=CronTrigger(hour=8, minute=0),
            id=_FOLLOWUP_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not register followups: %s", exc)


# ---------- daily digest ----------

def run_daily_digest() -> dict:
    """Defensive wrapper around digest.run_digest so a missing optional dep
    can never crash the scheduler tick.
    """
    try:
        from . import digest as _digest
    except Exception as exc:  # noqa: BLE001
        log.warning("digest module unavailable: %s", exc)
        return {"ok": False, "detail": f"digest_unavailable: {exc}"}
    try:
        out = _digest.run_digest(since_hours=24) or {}
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("digest run failed: %s", exc)
        return {"ok": False, "detail": str(exc)}


def _register_daily_digest() -> None:
    """Register the daily digest job at 07:00 (server tz = UTC)."""
    s = _get_scheduler()
    if s is None:
        return
    try:
        if s.get_job(_DIGEST_JOB_ID):
            s.remove_job(_DIGEST_JOB_ID)
    except Exception:
        pass
    try:
        s.add_job(
            functools.partial(_record_run, _DIGEST_JOB_ID, run_daily_digest),
            trigger=CronTrigger(hour=7, minute=0),
            id=_DIGEST_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not register daily digest: %s", exc)


# ---------- status ----------

def status() -> dict:
    s = _get_scheduler()
    jobs: list[dict] = []
    saved: dict[int, dict] = {}
    try:
        conn = get_conn()
        for r in conn.execute(
            "SELECT id, label, enabled, last_run_at, last_error, last_error_ts "
            "FROM saved_search ORDER BY id"
        ).fetchall():
            saved[int(r["id"])] = {
                "id": int(r["id"]),
                "label": r["label"],
                "enabled": bool(r["enabled"]),
                "last_run_at": r["last_run_at"],
                "last_error": r["last_error"],
                "last_error_ts": r["last_error_ts"],
            }
    except Exception:
        pass
    # Latest run per job_id from scheduler_job_run (job health for the UI).
    last_runs: dict[str, dict] = {}
    try:
        for r in get_conn().execute(
            "SELECT job_id, started_at, finished_at, status, error FROM scheduler_job_run "
            "WHERE id IN (SELECT MAX(id) FROM scheduler_job_run GROUP BY job_id)"
        ).fetchall():
            dur = None
            if r["finished_at"] and r["started_at"]:
                dur = int((float(r["finished_at"]) - float(r["started_at"])) * 1000)
            last_runs[r["job_id"]] = {
                "last_run_at": r["started_at"],
                "last_status": r["status"],
                "last_error": r["error"],
                "last_duration_ms": dur,
            }
    except Exception:
        pass
    if s is not None:
        try:
            for j in s.get_jobs():
                nrt = getattr(j, "next_run_time", None)
                # datetime -> ISO string; unscheduled (paused) -> None. Never
                # a raw datetime — status() is returned straight as JSON.
                nrt_iso = nrt.isoformat() if hasattr(nrt, "isoformat") else (str(nrt) if nrt else None)
                entry = {
                    "id": j.id,
                    "next_run": nrt_iso,
                    "next_run_time": nrt_iso,
                    "trigger": str(getattr(j, "trigger", "")),
                }
                entry.update(last_runs.get(j.id, {}))
                if str(j.id or "").startswith("saved_search_"):
                    try:
                        sid = int(str(j.id).rsplit("_", 1)[1])
                    except (ValueError, IndexError):
                        sid = None
                    info = saved.get(sid) if sid is not None else None
                    if info:
                        entry["last_run_at"] = info["last_run_at"]
                        entry["last_error"] = info["last_error"]
                        entry["last_error_ts"] = info["last_error_ts"]
                jobs.append(entry)
        except Exception:
            pass
    return {
        "apscheduler_installed": _HAS_APS,
        "running": is_running(),
        "jobs": jobs,
        "saved_searches": list(saved.values()),
    }


# ---------- audit log retention ----------

_AUDIT_RETENTION_JOB_ID = "jhh.audit_retention"


def run_audit_retention() -> dict:
    """Delete audit_log rows older than JHH_AUDIT_RETENTION_DAYS (default 90).

    Audit_log grows unbounded otherwise. After ~100k actions it bloats the
    SQLite file and slows inserts.
    """
    import os
    try:
        days = max(1, int(os.environ.get("JHH_AUDIT_RETENTION_DAYS", "90")))
    except ValueError:
        days = 90
    cutoff = time.time() - days * 86400
    conn = get_conn()
    with tx() as c:
        cur = c.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
        deleted = int(cur.rowcount or 0)
    if deleted > 0:
        # Self-audit ONLY when something was deleted (avoid feedback loops).
        try:
            audit("audit_retention_purged", "audit_log", None,
                  deleted=deleted, retention_days=days)
        except Exception:
            pass
    return {"ok": True, "deleted": deleted, "days": days,
            "retention_days": days, "cutoff_ts": cutoff}


def register_audit_retention() -> bool:
    """Daily job at 03:30 UTC. Idempotent via replace_existing."""
    s = _get_scheduler()
    if s is None:
        return False
    try:
        s.add_job(
            functools.partial(_record_run, _AUDIT_RETENTION_JOB_ID, run_audit_retention),
            CronTrigger(hour=3, minute=30, timezone="UTC"),
            id=_AUDIT_RETENTION_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("audit retention scheduler register failed: %s", exc)
        return False


# ---------- email / calendar retention ----------

_EMAIL_CAL_RETENTION_JOB_ID = "jhh.email_calendar_retention"


def run_email_calendar_retention() -> dict:
    """Delete email_event/calendar_event rows past their retention windows.

    JHH_EMAIL_RETENTION_DAYS (default 180) ages out email_event by
    received_at; JHH_CALENDAR_RETENTION_DAYS (default 365) ages out
    calendar_event by start_time. Rows without a timestamp are kept —
    NULL never compares < cutoff.
    """
    import os

    def _days(env: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(env, str(default))))
        except ValueError:
            return default

    email_days = _days("JHH_EMAIL_RETENTION_DAYS", 180)
    calendar_days = _days("JHH_CALENDAR_RETENTION_DAYS", 365)
    now = time.time()
    email_cutoff = now - email_days * 86400
    calendar_cutoff = now - calendar_days * 86400
    with tx() as c:
        cur = c.execute("DELETE FROM email_event WHERE received_at < ?", (email_cutoff,))
        email_deleted = int(cur.rowcount or 0)
        cur = c.execute("DELETE FROM calendar_event WHERE start_time < ?", (calendar_cutoff,))
        calendar_deleted = int(cur.rowcount or 0)
    if email_deleted or calendar_deleted:
        # Self-audit ONLY when something was deleted (avoid noise).
        try:
            audit("email_calendar_retention_purged", "system", None,
                  email_deleted=email_deleted, calendar_deleted=calendar_deleted,
                  email_retention_days=email_days, calendar_retention_days=calendar_days)
        except Exception:
            pass
    return {"ok": True, "email_deleted": email_deleted, "calendar_deleted": calendar_deleted,
            "email_retention_days": email_days, "calendar_retention_days": calendar_days,
            "email_cutoff_ts": email_cutoff, "calendar_cutoff_ts": calendar_cutoff}


def register_email_calendar_retention() -> bool:
    """Daily job at 04:00 UTC. Idempotent via replace_existing."""
    s = _get_scheduler()
    if s is None:
        return False
    try:
        s.add_job(
            functools.partial(_record_run, _EMAIL_CAL_RETENTION_JOB_ID, run_email_calendar_retention),
            CronTrigger(hour=4, minute=0, timezone="UTC"),
            id=_EMAIL_CAL_RETENTION_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("email/calendar retention scheduler register failed: %s", exc)
        return False


# ---------- application deadline reminders ----------

_DEADLINE_JOB_ID = "jhh.deadline_reminders"
_DEADLINE_WINDOW_S = 48 * 3600


def run_deadline_reminders() -> dict:
    """One-shot reminders for application deadlines inside the next 48h.

    Picks every live application (status not archived/rejected) whose
    deadline_at is <= now+48h — including already-overdue ones that were
    never reminded — and whose reminder_sent_at is NULL. For each match it
    inserts a `notification` row (kind='deadline_reminder', target the
    application), writes a `deadline_reminder` audit entry, and stamps
    reminder_sent_at so the reminder fires exactly once per deadline.
    PATCHing a new deadline_at resets reminder_sent_at, re-arming the job.
    """
    now = time.time()
    horizon = now + _DEADLINE_WINDOW_S
    conn = get_conn()
    rows = conn.execute(
        "SELECT a.id, a.job_id, a.deadline_at, "
        "j.title AS job_title, j.company AS job_company "
        "FROM application a LEFT JOIN job_posting j ON j.id = a.job_id "
        "WHERE a.deadline_at IS NOT NULL AND a.reminder_sent_at IS NULL "
        "AND a.deadline_at <= ? "
        "AND a.status NOT IN ('archived','rejected') "
        "ORDER BY a.deadline_at ASC",
        (horizon,),
    ).fetchall()
    reminded: list[int] = []
    for r in rows:
        app_id = int(r["id"])
        deadline_at = float(r["deadline_at"])
        hours_left = (deadline_at - now) / 3600.0
        label = " — ".join(
            p for p in (r["job_company"], r["job_title"]) if p
        ) or f"application {app_id}"
        if hours_left >= 0:
            title = f"Application deadline in {hours_left:.1f}h: {label}"
            body = (
                f"The application deadline for {label} is in {hours_left:.1f} "
                f"hours (epoch {deadline_at:.0f}). Submit before it closes."
            )
        else:
            title = f"Application deadline passed: {label}"
            body = (
                f"The application deadline for {label} passed "
                f"{-hours_left:.1f} hours ago (epoch {deadline_at:.0f})."
            )
        try:
            with tx() as c:
                c.execute(
                    "INSERT INTO notification "
                    "(ts, kind, title, body, read, target_type, target_id) "
                    "VALUES (?, ?, ?, ?, 0, 'application', ?)",
                    (now, "deadline_reminder", title, body, app_id),
                )
                c.execute(
                    "UPDATE application SET reminder_sent_at = ? "
                    "WHERE id = ? AND reminder_sent_at IS NULL",
                    (now, app_id),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("deadline reminder for application %s failed: %s", app_id, exc)
            continue
        try:
            audit("deadline_reminder", "application", app_id,
                  deadline_at=deadline_at, hours_left=round(hours_left, 2),
                  company=r["job_company"], title=r["job_title"])
        except Exception:
            pass
        reminded.append(app_id)
    return {"ok": True, "count": len(reminded), "reminded": reminded,
            "horizon_ts": horizon}


def register_deadline_reminders() -> bool:
    """Every 6 hours. Idempotent via replace_existing."""
    s = _get_scheduler()
    if s is None:
        return False
    try:
        s.add_job(
            functools.partial(_record_run, _DEADLINE_JOB_ID, run_deadline_reminders),
            IntervalTrigger(hours=6),
            id=_DEADLINE_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("deadline reminders scheduler register failed: %s", exc)
        return False


# ---------- nightly DB maintenance ----------

_DB_MAINTENANCE_JOB_ID = "jhh.db_maintenance"


def run_db_maintenance() -> dict:
    """PRAGMA optimize + WAL checkpoint so the SQLite file stays compact and
    the query planner stats stay fresh on long-running installs."""
    conn = get_conn()
    optimized = False
    checkpoint: list[int] | None = None
    error: str | None = None
    try:
        conn.execute("PRAGMA optimize")
        optimized = True
    except Exception as exc:  # noqa: BLE001
        error = f"optimize: {exc}"
        log.warning("PRAGMA optimize failed: %s", exc)
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # (busy, log_pages, checkpointed_pages)
        checkpoint = [int(v) for v in row] if row else None
    except Exception as exc:  # noqa: BLE001
        error = (error + "; " if error else "") + f"wal_checkpoint: {exc}"
        log.warning("wal_checkpoint failed: %s", exc)
    out: dict = {"ok": error is None, "optimized": optimized, "wal_checkpoint": checkpoint}
    if error:
        out["detail"] = error
    try:
        audit("db_maintenance_run", "system", None, **out)
    except Exception:
        pass
    return out


def register_db_maintenance() -> bool:
    """Nightly job at 04:30 UTC. Idempotent via replace_existing."""
    s = _get_scheduler()
    if s is None:
        return False
    try:
        s.add_job(
            functools.partial(_record_run, _DB_MAINTENANCE_JOB_ID, run_db_maintenance),
            CronTrigger(hour=4, minute=30, timezone="UTC"),
            id=_DB_MAINTENANCE_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("db maintenance scheduler register failed: %s", exc)
        return False
