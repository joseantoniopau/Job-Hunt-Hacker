"""Networking / connections — track who in the user's network could refer
them, then surface relevant connections for a given company or job.

This is the rolodex layer a great recruiter brings to the table.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from ..db import audit, get_conn, tx

log = logging.getLogger("jhh.services.networking")


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def add_connection(
    name: str,
    relationship: Optional[str] = None,
    company: Optional[str] = None,
    role: Optional[str] = None,
    contact: Optional[str] = None,
    notes: Optional[str] = None,
    additional_companies: Optional[list[dict]] = None,
) -> int:
    """Insert a connection. If `additional_companies` is provided (list of
    `{company, role}` dicts), also seed the many-to-many connection_company
    table — that lets one connection cover several past/present employers.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    now = time.time()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO connection
               (name, relationship, company, role, contact, notes,
                last_contacted_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (name, relationship, company, role, contact, notes, now, now),
        )
        cid = int(cur.lastrowid)
        if company:
            conn.execute(
                "INSERT INTO connection_company (connection_id, company, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (cid, company, role, now),
            )
        for entry in (additional_companies or []):
            if not isinstance(entry, dict):
                continue
            co = (entry.get("company") or "").strip()
            if not co:
                continue
            conn.execute(
                "INSERT INTO connection_company (connection_id, company, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (cid, co, entry.get("role"), now),
            )
    try:
        audit("connection_added", "connection", cid, name=name, company=company)
    except Exception:
        pass
    return cid


def list_connections(
    company: Optional[str] = None,
    limit: int = 500,
    offset: int = 0,
) -> list[dict]:
    conn = get_conn()
    if company:
        norm = _norm(company)
        rows = conn.execute(
            """SELECT DISTINCT c.* FROM connection c
               LEFT JOIN connection_company cc ON cc.connection_id = c.id
               WHERE LOWER(TRIM(c.company)) = ? OR LOWER(TRIM(cc.company)) = ?
               ORDER BY c.updated_at DESC, c.id DESC
               LIMIT ? OFFSET ?""",
            (norm, norm, int(limit), int(offset)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM connection ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ).fetchall()
    return [dict(r) for r in rows]


def get_connection(connection_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM connection WHERE id = ?", (int(connection_id),)
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    other_rows = conn.execute(
        "SELECT company, role FROM connection_company WHERE connection_id = ?",
        (int(connection_id),),
    ).fetchall()
    out["additional_companies"] = [dict(r) for r in other_rows]
    return out


_UPDATABLE_FIELDS = {"name", "relationship", "company", "role", "contact",
                     "notes", "last_contacted_at"}


def update_connection(connection_id: int, fields: dict) -> dict:
    cid = int(connection_id)
    existing = get_connection(cid)
    if not existing:
        raise LookupError(f"connection {cid} not found")
    clean: dict = {}
    for k, v in (fields or {}).items():
        if k in _UPDATABLE_FIELDS:
            clean[k] = v
    if not clean:
        return existing
    clean["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in clean)
    vals = list(clean.values()) + [cid]
    with tx() as conn:
        conn.execute(f"UPDATE connection SET {sets} WHERE id = ?", vals)
    try:
        audit("connection_updated", "connection", cid, fields=list(clean.keys()))
    except Exception:
        pass
    return get_connection(cid) or {}


def delete_connection(connection_id: int) -> bool:
    cid = int(connection_id)
    with tx() as conn:
        cur = conn.execute("DELETE FROM connection WHERE id = ?", (cid,))
        deleted = cur.rowcount > 0
    if deleted:
        try:
            audit("connection_deleted", "connection", cid)
        except Exception:
            pass
    return deleted


def who_could_refer_at(company: str) -> list[dict]:
    """All connections who currently / formerly work at the given company.
    Matches primary `company` field OR any row in `connection_company`.
    """
    target = _norm(company)
    if not target:
        return []
    conn = get_conn()
    rows = conn.execute(
        """SELECT DISTINCT c.*,
                  CASE WHEN LOWER(TRIM(c.company)) = ? THEN 'current' ELSE 'past_or_related' END
                       AS match_type
           FROM connection c
           LEFT JOIN connection_company cc ON cc.connection_id = c.id
           WHERE LOWER(TRIM(c.company)) = ?
              OR LOWER(TRIM(cc.company)) = ?
           ORDER BY match_type DESC, c.updated_at DESC""",
        (target, target, target),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        out.append(d)
    return out


def suggest_outreach(top_jobs: Optional[list[int]] = None, max_jobs: int = 10) -> list[dict]:
    """For the user's top prepared/saved jobs (or the provided job ids), list
    connections who could refer at each company.
    """
    conn = get_conn()
    if top_jobs:
        ids = [int(j) for j in top_jobs]
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""SELECT j.id, j.title, j.company, j.apply_url
                FROM job_posting j WHERE j.id IN ({placeholders})""",
            tuple(ids),
        ).fetchall()
    else:
        # default: top jobs we've prepared / saved
        rows = conn.execute(
            """SELECT j.id, j.title, j.company, j.apply_url
               FROM job_posting j
               JOIN application a ON a.job_id = j.id
               WHERE a.status IN ('saved', 'prepared')
               ORDER BY a.applied_at DESC NULLS LAST, j.discovered_at DESC
               LIMIT ?""",
            (int(max_jobs),),
        ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        company = d.get("company") or ""
        connections = who_could_refer_at(company) if company else []
        out.append({
            "job_id": d.get("id"),
            "title": d.get("title"),
            "company": company,
            "apply_url": d.get("apply_url"),
            "connections": connections,
            "connection_count": len(connections),
        })
    return out


__all__ = [
    "add_connection",
    "list_connections",
    "get_connection",
    "update_connection",
    "delete_connection",
    "who_could_refer_at",
    "suggest_outreach",
]
