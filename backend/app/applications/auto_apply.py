"""Auto-apply: prepares packets autonomously, but never submits.

Heavy gating: kill switch, daily cap, min-score floor, per-source policy.
Output is always a "packet ready for human review" — Phase 7 may add per-
platform consented submission, but it's not in this implementation.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..config import settings
from ..db import audit, get_conn, row_to_dict
from ..services.job_sources.pipeline import list_jobs
from . import compliance, packet_builder, pipeline

log = logging.getLogger("jhh.auto_apply")

_DEFAULT_CANDIDATE_LIMIT = 25


def _has_existing_application(job_id: int) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM application WHERE job_id = ? "
        "AND status NOT IN ('archived','rejected') LIMIT 1",
        (int(job_id),),
    ).fetchone()
    return row is not None


def _candidate_jobs(job_ids: list[int] | None) -> list[dict]:
    if job_ids:
        out: list[dict] = []
        conn = get_conn()
        for jid in job_ids:
            row = conn.execute(
                "SELECT j.*, m.overall_score FROM job_posting j "
                "LEFT JOIN job_match m ON m.job_id = j.id WHERE j.id = ?",
                (int(jid),),
            ).fetchone()
            if row:
                out.append(row_to_dict(row))
        return out
    # default: top N by score, status=new
    return list_jobs(
        limit=_DEFAULT_CANDIDATE_LIMIT,
        status="new",
        min_score=int(settings.auto_apply_min_score),
    )


def attempt(job_ids: list[int] | None = None) -> dict:
    if not settings.auto_apply_enabled:
        return {"ok": False, "reason": "auto_apply_disabled", "prepared": 0, "skipped": [], "capped": False}
    if compliance.kill_switch_active():
        return {"ok": False, "reason": "kill_switch_active", "prepared": 0, "skipped": [], "capped": False}

    today_count = compliance.today_applied_count()
    cap = int(settings.auto_apply_daily_cap)
    min_score = int(settings.auto_apply_min_score)

    prepared: list[dict] = []
    skipped: list[dict] = []
    capped = False

    jobs = _candidate_jobs(job_ids)
    for job in jobs:
        if today_count >= cap:
            capped = True
            skipped.append({"job_id": job.get("id"), "reason": f"daily_cap_reached ({cap})"})
            break

        jid = int(job["id"])

        # policy check
        allowed, reason = compliance.is_auto_apply_allowed(job.get("source") or "")
        if not allowed:
            skipped.append({"job_id": jid, "reason": reason})
            continue

        # score check
        score = job.get("overall_score")
        if score is None or float(score) < min_score:
            skipped.append({"job_id": jid, "reason": f"score_below_threshold ({score} < {min_score})"})
            continue

        # dedupe: don't reprepare for jobs we've already touched
        if _has_existing_application(jid):
            skipped.append({"job_id": jid, "reason": "application_already_exists"})
            continue

        # build packet
        try:
            res = packet_builder.build(jid)
        except Exception as exc:  # noqa: BLE001
            log.warning("packet build failed for job %s: %s", jid, exc)
            skipped.append({"job_id": jid, "reason": f"packet_build_error: {type(exc).__name__}: {exc}"})
            continue
        if not res.get("ok"):
            skipped.append({"job_id": jid, "reason": res.get("error") or "packet_build_failed"})
            continue

        app_id = pipeline.create_application(
            job_id=jid,
            status="auto_packet_ready",
            mode="auto",
            notes="Auto-prepared packet — REQUIRES HUMAN REVIEW BEFORE SUBMISSION.",
        )
        try:
            audit(
                "auto_apply_packet_prepared",
                "application",
                int(app_id),
                job_id=jid,
                packet_dir=res.get("packet_dir"),
                score=score,
            )
        except Exception:
            pass
        today_count += 1
        prepared.append({
            "job_id": jid,
            "application_id": app_id,
            "packet_dir": res.get("packet_dir"),
            "score": score,
        })

    return {
        "ok": True,
        "prepared": len(prepared),
        "prepared_items": prepared,
        "skipped": skipped,
        "capped": capped,
        "today_count": today_count,
        "cap": cap,
        "min_score": min_score,
    }


def queue(limit: int = 100) -> list[dict]:
    """List applications awaiting human review."""
    return pipeline.list_applications(status="auto_packet_ready", limit=limit)


def status() -> dict:
    snap = compliance.status_snapshot()
    snap["queue_size"] = len(queue(limit=1000))
    return snap
