"""Audit-log read endpoints.

The audit_log table is append-only and the canonical source for
"what did the system do, when". These endpoints expose it for the UI
and ops dashboards. Writes still go through `db.audit()`; this router
is strictly read-only.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from ..db import get_conn

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _parse_when(value: Optional[str]) -> Optional[float]:
    """Accept either a unix epoch seconds string or an ISO-8601 timestamp."""
    if value is None or value == "":
        return None
    # Numeric → treat as epoch seconds.
    try:
        return float(value)
    except ValueError:
        pass
    # ISO-8601: tolerate trailing Z (UTC) since fromisoformat predates 3.11.
    try:
        normalized = value.strip().rstrip("Z")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        raise HTTPException(400, f"invalid timestamp: {value!r}")


def _row_to_record(row: Any) -> dict:
    d = dict(row)
    detail = d.get("detail_json")
    if isinstance(detail, str) and detail.strip():
        try:
            d["detail_json"] = json.loads(detail)
        except Exception:
            # Leave the raw string in place so a corrupt row doesn't 500.
            pass
    return d


@router.get("")
def list_audit(
    limit: int = Query(100, ge=1, le=1000),
    action: Optional[str] = Query(None, description="Filter by action name prefix."),
    since: Optional[str] = Query(None, description="Epoch seconds or ISO-8601."),
    until: Optional[str] = Query(None, description="Epoch seconds or ISO-8601."),
) -> dict:
    conn = get_conn()
    where: list[str] = []
    params: list[Any] = []

    if action:
        where.append("action LIKE ?")
        params.append(f"{action}%")

    s = _parse_when(since)
    if s is not None:
        where.append("ts >= ?")
        params.append(s)

    u = _parse_when(until)
    if u is not None:
        where.append("ts <= ?")
        params.append(u)

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT id, ts, actor, action, target_type, target_id, detail_json "
        "FROM audit_log" + clause + " ORDER BY ts DESC, id DESC LIMIT ?"
    )
    params.append(int(limit))

    rows = conn.execute(sql, params).fetchall()
    return {"ok": True, "data": [_row_to_record(r) for r in rows]}


@router.get("/stats")
def audit_stats() -> dict:
    conn = get_conn()

    total_row = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
    total = int(total_row[0]) if total_row else 0

    by_action: dict[str, int] = {}
    for row in conn.execute(
        "SELECT action, COUNT(*) FROM audit_log GROUP BY action ORDER BY COUNT(*) DESC"
    ).fetchall():
        by_action[str(row[0])] = int(row[1])

    # Group by day for the last 30 days. SQLite stores ts as REAL epoch
    # seconds; using `date(ts, 'unixepoch')` gives an ISO yyyy-mm-dd string.
    cutoff = time.time() - 30 * 86400
    by_day_last_30: dict[str, int] = {}
    for row in conn.execute(
        "SELECT date(ts, 'unixepoch') AS d, COUNT(*) "
        "FROM audit_log WHERE ts >= ? GROUP BY d ORDER BY d ASC",
        (cutoff,),
    ).fetchall():
        if row[0] is None:
            continue
        by_day_last_30[str(row[0])] = int(row[1])

    return {
        "ok": True,
        "data": {
            "total": total,
            "by_action": by_action,
            "by_day_last_30": by_day_last_30,
        },
    }
