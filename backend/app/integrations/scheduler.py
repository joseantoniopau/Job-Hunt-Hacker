"""APScheduler wrapper. Idempotent registration.

Without APScheduler installed, exposes the same API but as no-ops.
"""
from __future__ import annotations

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
        s.start(paused=False)
        _started = True
        # standing jobs
        _register_inbox_sweep()
        _register_followups()
        _register_daily_digest()
        register_audit_retention()
        register_saved_searches()
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
    """Execute one saved search: search + persist + score new ids."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM saved_search WHERE id = ?", (int(saved_search_id),)).fetchone()
    if not row:
        return {"ok": False, "detail": f"saved_search {saved_search_id} not found"}
    rec = row_to_dict(row)
    if not rec.get("enabled"):
        return {"ok": False, "detail": "disabled"}
    query = rec.get("query_json") or {}
    if isinstance(query, str):
        try:
            query = json.loads(query)
        except Exception:
            query = {}

    try:
        from ..services.job_sources.pipeline import persist, search_all
        from ..services.job_sources.base import JobSearchQuery
        from ..services.job_sources import REGISTRY
    except Exception as exc:  # noqa: BLE001
        log.warning("search pipeline unavailable: %s", exc)
        return {"ok": False, "detail": f"pipeline_unavailable: {exc}"}

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

    q = JobSearchQuery(
        query=query.get("query") or "",
        location=query.get("location"),
        is_remote=bool(query.get("is_remote")),
        results_per_site=int(query.get("results_per_site") or 25),
        hours_old=query.get("hours_old"),
        country=query.get("country") or "usa",
        employment_type=query.get("employment_type"),
        distance=query.get("distance"),
        extra={"sites": sites},
    )
    sres = search_all(q, requested)
    pres = persist(sres.get("records") or [])

    # score
    scored = 0
    try:
        from ..matching import scorer  # type: ignore
        if hasattr(scorer, "score_job"):
            for jid in pres.get("ids") or []:
                try:
                    scorer.score_job(int(jid))
                    scored += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("score_job(%s) failed: %s", jid, exc)
    except Exception:
        pass

    with tx() as conn:
        conn.execute(
            "UPDATE saved_search SET last_run_at = ? WHERE id = ?",
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
            _run_saved_search,
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
            run_inbox_sweep,
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
            run_followups,
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
            run_daily_digest,
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
    if s is not None:
        try:
            for j in s.get_jobs():
                jobs.append({
                    "id": j.id,
                    "next_run": str(getattr(j, "next_run_time", "")),
                    "trigger": str(getattr(j, "trigger", "")),
                })
        except Exception:
            pass
    return {
        "apscheduler_installed": _HAS_APS,
        "running": is_running(),
        "jobs": jobs,
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
            run_audit_retention,
            CronTrigger(hour=3, minute=30, timezone="UTC"),
            id=_AUDIT_RETENTION_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("audit retention scheduler register failed: %s", exc)
        return False
