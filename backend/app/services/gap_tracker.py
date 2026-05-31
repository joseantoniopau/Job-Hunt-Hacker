"""Skill-gap trend tracking.

Records every "missing keyword" that the scorer (or any caller) reports
against a job. Enables answering: which skills are blocking me most
often? Are those gaps trending up or down? Which jobs surfaced them?
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from ..db import audit, get_conn, tx

log = logging.getLogger("jhh.services.gap_tracker")


def _normalize(kw: str) -> str:
    """Trim + lowercase keyword for grouping. Keep original casing in
    a separate column if we ever want display-friendly variants."""
    return (kw or "").strip().lower()


def record_gaps(job_id: int | None, missing: list[str]) -> int:
    """Insert one gap_event row per missing keyword. Returns the number
    of rows inserted. Empty/blank keywords are skipped silently."""
    if not missing:
        return 0
    now = time.time()
    cleaned: list[str] = []
    for kw in missing:
        n = _normalize(kw)
        if n:
            cleaned.append(n)
    if not cleaned:
        return 0
    inserted = 0
    with tx() as conn:
        for kw in cleaned:
            try:
                conn.execute(
                    "INSERT INTO gap_event (ts, job_id, missing_keyword) "
                    "VALUES (?, ?, ?)",
                    (now, int(job_id) if job_id is not None else None, kw),
                )
                inserted += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("gap insert failed kw=%r: %s", kw, exc)
                continue
    try:
        audit("gap_recorded", "gap_event", int(job_id) if job_id is not None else None,
              count=inserted, keywords=cleaned)
    except Exception:
        pass
    return inserted


def top_gaps(days: int = 30, limit: int = 10) -> list[dict]:
    """Return the most-cited missing keywords over the last `days` days.

    Each entry: {keyword, mentions, last_seen_at, sample_job_ids[<=5]}.
    Ordered by mentions desc, then last_seen_at desc.
    """
    cutoff = time.time() - max(int(days), 0) * 86400
    conn = get_conn()
    rows = conn.execute(
        "SELECT missing_keyword, COUNT(*) AS mentions, MAX(ts) AS last_seen_at "
        "FROM gap_event WHERE ts >= ? "
        "GROUP BY missing_keyword "
        "ORDER BY mentions DESC, last_seen_at DESC "
        "LIMIT ?",
        (cutoff, int(limit)),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        kw = r["missing_keyword"]
        sample = conn.execute(
            "SELECT DISTINCT job_id FROM gap_event "
            "WHERE missing_keyword = ? AND ts >= ? AND job_id IS NOT NULL "
            "ORDER BY ts DESC LIMIT 5",
            (kw, cutoff),
        ).fetchall()
        out.append({
            "keyword": kw,
            "mentions": int(r["mentions"]),
            "last_seen_at": float(r["last_seen_at"]) if r["last_seen_at"] is not None else None,
            "sample_job_ids": [int(s[0]) for s in sample if s[0] is not None],
        })
    return out


def trend(days: int = 30) -> dict:
    """Return a daily count of gap events over the last `days` days plus
    summary totals: {by_day, total, unique_keywords}.
    """
    cutoff = time.time() - max(int(days), 0) * 86400
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, missing_keyword FROM gap_event WHERE ts >= ?",
        (cutoff,),
    ).fetchall()
    by_day: dict[str, int] = {}
    keywords: set[str] = set()
    for r in rows:
        ts = float(r["ts"])
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        by_day[day] = by_day.get(day, 0) + 1
        keywords.add(r["missing_keyword"])
    return {
        "by_day": dict(sorted(by_day.items())),
        "total": len(rows),
        "unique_keywords": len(keywords),
    }
