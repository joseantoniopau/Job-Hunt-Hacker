"""GET/PATCH/DELETE /api/jobs ... plus JD change tracking (snapshots)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import audit, get_conn, tx
from ..services.job_sources.pipeline import (
    get_job,
    list_jobs,
    update_status,
)

log = logging.getLogger("jhh.routers.jobs")

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class StatusUpdate(BaseModel):
    status: str


class RescoreRequest(BaseModel):
    job_ids: list[int]


class SnapshotCheckBody(BaseModel):
    """Optional body for POST /api/jobs/{id}/snapshot-check.

    description: when provided, it is treated as the freshly-fetched JD text —
    compared against the latest snapshot baseline and, if it changed, recorded
    as a new snapshot AND written back to job_posting.description. When omitted
    the check compares the job row's CURRENT description against the latest
    snapshot (drift detection after some other code updated the row).
    """
    description: Optional[str] = None


# --------- JD change tracking helpers ---------

_WORD_RE = re.compile(r"[A-Za-z0-9_+#./-]+")


def _content_hash(text: str | None) -> str:
    """Stable sha256 of the description, whitespace-trimmed."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _keywords(text: str | None) -> set[str]:
    """Lowercased words longer than 4 chars — the 'keyword' universe for the
    naive set-diff in change summaries."""
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) > 4}


def _diff_summary(old: str | None, new: str | None) -> dict:
    """Naive diff stats between two descriptions: char-length delta plus a
    set diff of words >4 chars (capped at 50 each side)."""
    old = old or ""
    new = new or ""
    old_kw = _keywords(old)
    new_kw = _keywords(new)
    return {
        "chars_added": max(0, len(new) - len(old)),
        "chars_removed": max(0, len(old) - len(new)),
        "old_len": len(old),
        "new_len": len(new),
        "added_keywords": sorted(new_kw - old_kw)[:50],
        "removed_keywords": sorted(old_kw - new_kw)[:50],
    }


def snapshot_job_if_changed(job_id: int, new_description: str | None = None) -> dict:
    """Record a job_posting_snapshot row when the job's description changed.

    Behavior:
      * First call for a job records a baseline snapshot of the current
        description (change_summary = {"initial": true}) and reports
        changed=False — drift can only be measured against a baseline.
      * new_description=None  -> compare the job row's current description
        hash against the latest snapshot's content_hash (snapshot-check mode).
      * new_description given -> compare it against the latest snapshot; on
        change, record the snapshot and update job_posting.description so the
        row stays current. This is the hook for refresh/persist flows that
        re-fetch a JD for an already-known job.

    Returns {job_id, changed, baseline_created, snapshot_id, change_summary,
    content_hash}. Raises LookupError when the job doesn't exist.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT id, description FROM job_posting WHERE id = ?", (int(job_id),)
    ).fetchone()
    if not row:
        raise LookupError(f"job {job_id} not found")
    current_desc = row["description"] or ""
    now = time.time()
    out: dict = {
        "job_id": int(job_id),
        "changed": False,
        "baseline_created": False,
        "snapshot_id": None,
        "change_summary": None,
    }
    summary: dict | None = None
    with tx() as c:
        latest = c.execute(
            "SELECT id, content_hash, description FROM job_posting_snapshot "
            "WHERE job_id = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
            (int(job_id),),
        ).fetchone()
        if latest is None:
            cur = c.execute(
                "INSERT INTO job_posting_snapshot "
                "(job_id, content_hash, description, captured_at, change_summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(job_id), _content_hash(current_desc), current_desc, now,
                 json.dumps({"initial": True})),
            )
            out["baseline_created"] = True
            out["snapshot_id"] = int(cur.lastrowid)
            baseline_hash = _content_hash(current_desc)
            baseline_desc = current_desc
        else:
            baseline_hash = latest["content_hash"] or ""
            baseline_desc = latest["description"] or ""
        candidate = current_desc if new_description is None else str(new_description)
        cand_hash = _content_hash(candidate)
        out["content_hash"] = cand_hash
        if cand_hash == baseline_hash:
            return out
        summary = _diff_summary(baseline_desc, candidate)
        cur = c.execute(
            "INSERT INTO job_posting_snapshot "
            "(job_id, content_hash, description, captured_at, change_summary) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(job_id), cand_hash, candidate, now, json.dumps(summary)),
        )
        out["changed"] = True
        out["snapshot_id"] = int(cur.lastrowid)
        out["change_summary"] = summary
        if new_description is not None and candidate != current_desc:
            c.execute(
                "UPDATE job_posting SET description = ? WHERE id = ?",
                (candidate, int(job_id)),
            )
    try:
        audit("job_posting_changed", "job_posting", int(job_id),
              snapshot_id=out["snapshot_id"],
              chars_added=(summary or {}).get("chars_added"),
              chars_removed=(summary or {}).get("chars_removed"))
    except Exception:
        pass
    return out


def _annotate_posting_changed(rows: list[dict]) -> list[dict]:
    """Set posting_changed=true on job dicts whose latest non-baseline
    snapshot was captured AFTER the user's most recent live application
    activity for that job (applied_at, else the application's creation ts
    from audit_json[0].ts). Jobs without an application are never flagged."""
    for r in rows:
        if isinstance(r, dict):
            r["posting_changed"] = False
    ids = [int(r["id"]) for r in rows if isinstance(r, dict) and r.get("id") is not None]
    if not ids:
        return rows
    conn = get_conn()
    ph = ",".join("?" * len(ids))
    changed_at: dict[int, float] = {}
    try:
        for s in conn.execute(
            f"SELECT job_id, MAX(captured_at) AS last_change "
            f"FROM job_posting_snapshot "
            f"WHERE job_id IN ({ph}) "
            f"AND COALESCE(json_extract(change_summary, '$.initial'), 0) != 1 "
            f"GROUP BY job_id",
            ids,
        ).fetchall():
            changed_at[int(s["job_id"])] = float(s["last_change"] or 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("posting_changed snapshot probe failed: %s", exc)
        return rows
    if not changed_at:
        return rows
    app_ref: dict[int, float] = {}
    try:
        for a in conn.execute(
            f"SELECT job_id, "
            f"MAX(COALESCE(applied_at, json_extract(audit_json, '$[0].ts'), 0)) AS ref_ts "
            f"FROM application "
            f"WHERE job_id IN ({ph}) AND status NOT IN ('archived','rejected') "
            f"GROUP BY job_id",
            ids,
        ).fetchall():
            app_ref[int(a["job_id"])] = float(a["ref_ts"] or 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("posting_changed application probe failed: %s", exc)
        return rows
    for r in rows:
        if not isinstance(r, dict):
            continue
        jid = r.get("id")
        if jid in app_ref and changed_at.get(jid, 0.0) > app_ref[jid]:
            r["posting_changed"] = True
    return rows


def _alias_job(row: dict) -> dict:
    """Add UI-friendly aliases so the frontend can rely on stable field
    names (score, url, currency, is_remote, created_at) regardless of the
    underlying DB column names.

    Score scale convention: the scorer persists overall_score on a 0-1
    scale; UI consumes 0-100. We multiply at the API boundary.
    """
    if not row:
        return row
    out = dict(row)
    if "overall_score" in out and "score" not in out:
        v = out["overall_score"]
        out["score"] = int(round(float(v) * 100)) if v is not None else None
    if "apply_url" in out and "url" not in out:
        out["url"] = out["apply_url"]
    if "currency" in out and "salary_currency" not in out:
        out["salary_currency"] = out["currency"]
    if "discovered_at" in out and "created_at" not in out:
        out["created_at"] = out["discovered_at"]
    if "remote_type" in out:
        rt = (out.get("remote_type") or "").lower()
        out["is_remote"] = rt == "remote"
    return out


@router.get("")
def list_endpoint(
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    min_score: Optional[int] = None,
) -> dict:
    rows = list_jobs(
        limit=limit, status=status, source=source, min_score=min_score, offset=offset
    )
    rows = [_alias_job(r) for r in rows]
    rows = _annotate_posting_changed(rows)
    return {"ok": True, "data": rows, "count": len(rows)}


@router.get("/{job_id}")
def get_endpoint(job_id: int) -> dict:
    row = get_job(job_id)
    if not row:
        raise HTTPException(404, f"job {job_id} not found")
    data = _alias_job(row)
    _annotate_posting_changed([data])
    return {"ok": True, "data": data}


@router.post("/{job_id}/snapshot-check")
def snapshot_check_endpoint(job_id: int, body: Optional[SnapshotCheckBody] = None) -> dict:
    """Compare the job's description against its latest snapshot and record a
    new snapshot when it changed.

    Request: optional JSON {"description": "<freshly fetched JD text>"}. With a
    body, the supplied text is the candidate (and is written back to the job
    row on change); without one, the job row's current description is checked
    against the latest snapshot. The first call per job records a baseline.

    Response: {ok, data: {job_id, changed, baseline_created, snapshot_id,
    change_summary, content_hash}} — change_summary carries the naive diff
    stats (chars_added/chars_removed/added_keywords/removed_keywords).
    """
    try:
        out = snapshot_job_if_changed(
            job_id, body.description if body is not None else None
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "data": out}


@router.get("/{job_id}/snapshots")
def list_snapshots_endpoint(job_id: int) -> dict:
    """JD snapshot history for a job, newest first.

    Response: {ok, data: [{id, job_id, content_hash, captured_at,
    change_summary (parsed dict or null), initial (bool), description}],
    count}.
    """
    conn = get_conn()
    if not conn.execute(
        "SELECT 1 FROM job_posting WHERE id = ?", (int(job_id),)
    ).fetchone():
        raise HTTPException(404, f"job {job_id} not found")
    rows = conn.execute(
        "SELECT id, job_id, content_hash, description, captured_at, change_summary "
        "FROM job_posting_snapshot WHERE job_id = ? "
        "ORDER BY captured_at DESC, id DESC",
        (int(job_id),),
    ).fetchall()
    data: list[dict] = []
    for r in rows:
        d = dict(r)
        cs = d.get("change_summary")
        if isinstance(cs, str) and cs.strip():
            try:
                d["change_summary"] = json.loads(cs)
            except Exception:
                pass
        d["initial"] = bool(
            isinstance(d.get("change_summary"), dict)
            and d["change_summary"].get("initial")
        )
        data.append(d)
    return {"ok": True, "data": data, "count": len(data)}


@router.patch("/{job_id}/status")
def patch_status(job_id: int, body: StatusUpdate) -> dict:
    ok = update_status(job_id, body.status)
    if not ok:
        raise HTTPException(404, f"job {job_id} not found")
    return {"ok": True, "detail": f"status set to {body.status}"}


@router.delete("/{job_id}")
def delete_endpoint(job_id: int) -> dict:
    ok = update_status(job_id, "archived")
    if not ok:
        raise HTTPException(404, f"job {job_id} not found")
    return {"ok": True, "detail": "archived"}


class BulkStatusRequest(BaseModel):
    job_ids: list[int]
    status: str


@router.post("/bulk-status")
def bulk_status(body: BulkStatusRequest) -> dict:
    """Move a batch of jobs to a status (typically 'dismissed' or 'saved').

    Used by the Dashboard highlight flow: user selects N rows and clicks
    DISMISS SELECTED or SAVE SELECTED.
    """
    ok, errors = [], {}
    for jid in body.job_ids:
        if update_status(int(jid), body.status):
            ok.append(int(jid))
        else:
            errors[int(jid)] = "not found"
    return {"ok": True, "data": {"updated": ok, "errors": errors,
                                 "status": body.status}}


@router.post("/refresh")
async def refresh_jobs(top_n: int = 25, hours_old: int = 168) -> dict:
    """Re-run the user's saved-search query but EXCLUDE jobs the user has
    already dismissed. Used by the Dashboard REFRESH button.

    The exclusion works at persist time: any record whose apply_url or
    (source, external_id) tuple matches a dismissed job_posting row is
    skipped during insert. Dismissed rows themselves are not re-fetched.
    """
    from ..db import get_conn
    from ..services.job_sources import REGISTRY
    from ..services.job_sources.pipeline import search_all, persist
    from ..services.job_sources.base import JobSearchQuery
    from ..services.search_plan import build_search_plan

    conn = get_conn()
    # Shared plan: employer-name filtering, remote preference, and the
    # no-home-city-on-remote rule all live in build_search_plan.
    plan = build_search_plan()
    queries = plan.queries

    # Pre-build dismissed exclusion set (composite key sources used at persist)
    dismissed_rows = conn.execute(
        "SELECT source, external_id, apply_url FROM job_posting WHERE status = 'dismissed'"
    ).fetchall()
    dismissed_keys = set()
    dismissed_urls = set()
    for r in dismissed_rows:
        if r["external_id"]:
            dismissed_keys.add((r["source"], r["external_id"]))
        if r["apply_url"]:
            dismissed_urls.add(r["apply_url"])

    sites = list(REGISTRY.keys())
    merged: list[dict] = []
    seen: set = set()
    per_source_agg: dict = {}
    errors_agg: dict = {}
    excluded = 0
    per_q = max(5, int(top_n) // max(1, len(queries)))
    for q in queries:
        sr = search_all(
            JobSearchQuery(
                query=q,
                location=plan.location,
                is_remote=plan.is_remote,
                results_per_site=per_q,
                hours_old=int(hours_old),
            ),
            sites=sites,
        )
        for rec in sr.get("records") or []:
            src = rec.get("source")
            ext = rec.get("external_id")
            url = rec.get("apply_url") or rec.get("url")
            if (src, ext) in dismissed_keys or (url and url in dismissed_urls):
                excluded += 1
                continue
            key = (src, ext or url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(rec)
        for k, v in (sr.get("per_source") or {}).items():
            per_source_agg[k] = per_source_agg.get(k, 0) + int(v or 0)
        for k, v in (sr.get("errors") or {}).items():
            errors_agg.setdefault(k, str(v))

    pr = persist(merged)

    # Score the new rows immediately — without this, refreshed jobs land on
    # the dashboard unscored and sort to the bottom regardless of fit.
    scored = 0
    try:
        from ..matching import scorer as _scorer
        if hasattr(_scorer, "score_job"):
            for jid in pr.get("ids") or []:
                try:
                    # No per-job LLM polish in bulk — keeps REFRESH fast.
                    _scorer.score_job(int(jid), llm_polish=False)
                    scored += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("refresh score_job(%s) failed: %s", jid, exc)
    except Exception:
        pass

    return {"ok": True, "data": {
        "queries": queries,
        "discovered": len(merged),
        "inserted": int(pr.get("inserted", 0)),
        "scored": scored,
        "excluded_dismissed": excluded,
        "per_source": per_source_agg,
        "errors": errors_agg,
    }}


@router.post("/rescore")
def rescore(body: RescoreRequest) -> dict:
    scored: list[int] = []
    errors: dict[int, str] = {}
    try:
        from ..matching import scorer  # type: ignore
    except Exception:
        scorer = None  # type: ignore
    if scorer is None or not hasattr(scorer, "score_job"):
        return {"ok": True, "data": {"scored": [], "skipped": body.job_ids,
                                     "detail": "scorer module not available"}}
    for jid in body.job_ids:
        try:
            scorer.score_job(int(jid))
            scored.append(int(jid))
        except Exception as exc:  # noqa: BLE001
            log.warning("rescore failed for job %s: %s", jid, exc)
            errors[int(jid)] = f"{type(exc).__name__} (see server log)"
    return {"ok": True, "data": {"scored": scored, "errors": errors}}
