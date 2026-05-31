"""SQLite-backed adapter result cache.

Keyed off (adapter_name, normalized JobSearchQuery), TTL governed by
JHH_ADAPTER_CACHE_TTL_S (default 3600s). The table is created lazily on
first use so this module doesn't have to live in db.py's SCHEMA list.

Why SQLite (rather than a process-local dict): the FastAPI app runs as a
single uvicorn process today, but persisting through a restart matters --
LinkedIn / Indeed responses are precious. A 5-minute cache survives the
typical "kill server, restart, re-run search" debugging loop.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict
from typing import Any

from ...db import get_conn, tx
from .base import JobRecord, JobSearchQuery

log = logging.getLogger("jhh.sources.cache")


def _default_ttl() -> int:
    raw = os.environ.get("JHH_ADAPTER_CACHE_TTL_S", "3600").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 3600


_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS adapter_cache (
    key TEXT PRIMARY KEY,
    body BLOB,
    fetched_at REAL,
    ttl_s INTEGER
)
"""

_table_ready = False
_table_lock = threading.Lock()


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if _table_ready:
            return
        try:
            with tx() as conn:
                conn.execute(_TABLE_DDL)
            _table_ready = True
        except Exception as exc:  # noqa: BLE001
            log.warning("adapter_cache create failed: %s", exc)


def _cache_key(adapter_name: str, q: JobSearchQuery) -> str:
    extra = q.extra if isinstance(q.extra, dict) else {}
    sites_csv = ",".join(sorted(str(s) for s in (extra.get("sites") or [])))
    parts = "|".join([
        adapter_name or "",
        (q.query or "").strip().lower(),
        (q.location or "").strip().lower(),
        sites_csv,
        str(q.hours_old if q.hours_old is not None else ""),
        (q.country or "").strip().lower(),
        str(q.results_per_site or 0),
        (q.employment_type or "").strip().lower(),
    ])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def _records_to_blob(records: list[JobRecord]) -> bytes:
    return json.dumps([asdict(r) for r in records], default=str).encode("utf-8")


def _blob_to_records(blob: Any) -> list[JobRecord]:
    if blob is None:
        return []
    if isinstance(blob, (bytes, bytearray, memoryview)):
        try:
            text = bytes(blob).decode("utf-8")
        except Exception:
            return []
    else:
        text = str(blob)
    try:
        items = json.loads(text)
    except Exception:
        return []
    out: list[JobRecord] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            out.append(JobRecord(**it))
        except TypeError:
            # Schema drift: filter to known fields only.
            allowed = {
                k: v for k, v in it.items()
                if k in JobRecord.__dataclass_fields__  # type: ignore[attr-defined]
            }
            try:
                out.append(JobRecord(**allowed))
            except Exception:
                continue
    return out


# ---------------------------------------------------------------- public ----

def get(adapter_name: str, q: JobSearchQuery) -> list[JobRecord] | None:
    """Return cached records on a fresh hit; None on miss / expired."""
    _ensure_table()
    key = _cache_key(adapter_name, q)
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT body, fetched_at, ttl_s FROM adapter_cache WHERE key = ?",
            (key,),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        log.debug("cache get failed: %s", exc)
        return None
    if row is None:
        return None
    body, fetched_at, ttl_s = row[0], row[1], row[2]
    try:
        fetched_at = float(fetched_at or 0)
        ttl_s = int(ttl_s or 0)
    except Exception:
        return None
    if ttl_s <= 0:
        return None
    if (time.time() - fetched_at) > ttl_s:
        return None
    return _blob_to_records(body)


def set(adapter_name: str, q: JobSearchQuery, records: list[JobRecord], ttl: int | None = None) -> None:
    """Write records to the cache. Silent on failure (cache is best-effort)."""
    _ensure_table()
    key = _cache_key(adapter_name, q)
    blob = _records_to_blob(records or [])
    eff_ttl = int(ttl) if ttl is not None else _default_ttl()
    try:
        with tx() as conn:
            conn.execute(
                "INSERT INTO adapter_cache (key, body, fetched_at, ttl_s) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET body=excluded.body, "
                "fetched_at=excluded.fetched_at, ttl_s=excluded.ttl_s",
                (key, blob, time.time(), eff_ttl),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("cache set failed: %s", exc)


def purge_expired() -> int:
    """Drop expired rows; returns count removed."""
    _ensure_table()
    try:
        with tx() as conn:
            cur = conn.execute(
                "DELETE FROM adapter_cache WHERE (fetched_at + ttl_s) < ?",
                (time.time(),),
            )
            return int(cur.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("cache purge failed: %s", exc)
        return 0


def clear_all() -> int:
    """Truncate every cache entry; returns count removed."""
    _ensure_table()
    try:
        with tx() as conn:
            cur = conn.execute("DELETE FROM adapter_cache")
            return int(cur.rowcount or 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("cache clear failed: %s", exc)
        return 0
