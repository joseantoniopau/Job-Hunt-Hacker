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


# --------------------------------------------------------------------------
# Referral finder — rank connections for referral likelihood at a company.
# --------------------------------------------------------------------------

# Lower number = stronger referral signal.
_KIND_PRIORITY = {"current": 0, "past": 1, "fuzzy": 2, "mention": 3}

_OPEN_JOB_STATUSES = ("new", "saved")


def _fuzzy_norm(s: Optional[str]) -> str:
    """Lowercase, strip ALL punctuation/underscores to single spaces.

    'Stripe, Inc.' -> 'stripe inc'; 'Ex-Google engineer' -> 'ex google engineer'.
    """
    s = (s or "").lower()
    s = re.sub(r"[\W_]+", " ", s, flags=re.UNICODE)
    return s.strip()


def _fuzzy_contains(a: str, b: str) -> bool:
    """Punctuation/case-insensitive company match: equal after normalization,
    or one is a whole-token substring of the other ('google' ~ 'google llc',
    'acme' ~ 'the acme corporation'). Tokens shorter than 3 chars never
    substring-match (avoids 'AI' matching half the rolodex) but may still
    match by equality.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < 3:
        return False
    return f" {shorter} " in f" {longer} "


def _load_network() -> list[dict]:
    """All connections with their connection_company rows attached, loaded in
    two queries so per-company classification is pure in-memory work.
    """
    conn = get_conn()
    rows = conn.execute("SELECT * FROM connection").fetchall()
    by_id: dict[int, dict] = {}
    for r in rows:
        d = dict(r)
        by_id[d["id"]] = {"connection": d, "companies": []}
    cc_rows = conn.execute(
        "SELECT connection_id, company, role FROM connection_company"
    ).fetchall()
    for r in cc_rows:
        entry = by_id.get(r["connection_id"])
        if entry is not None:
            entry["companies"].append({"company": r["company"], "role": r["role"]})
    return list(by_id.values())


def _classify_match(entry: dict, target_company: str) -> Optional[tuple[str, Optional[str]]]:
    """Best (match_kind, matched_company_text) for one connection vs a target
    company, or None when there is no signal at all.

    current  — connection.company exactly matches (case/space-insensitive).
    past     — a connection_company row exactly matches.
    fuzzy    — case/punct-insensitive whole-token substring match on the
               primary company or any connection_company row.
    mention  — the company appears in the connection's notes or role text
               (alumni / "knows people there" signal).
    """
    target_norm = _norm(target_company)
    target_fz = _fuzzy_norm(target_company)
    if not target_norm:
        return None
    c = entry["connection"]
    primary = (c.get("company") or "").strip()
    if primary and _norm(primary) == target_norm:
        return ("current", primary)
    for row in entry["companies"]:
        co = (row.get("company") or "").strip()
        if co and _norm(co) == target_norm:
            return ("past", co)
    for co in [primary] + [(row.get("company") or "").strip() for row in entry["companies"]]:
        if co and _fuzzy_contains(_fuzzy_norm(co), target_fz):
            return ("fuzzy", co)
    haystack = _fuzzy_norm(" ".join(filter(None, [c.get("notes"), c.get("role")])))
    if target_fz and haystack and f" {target_fz} " in f" {haystack} ":
        return ("mention", None)
    return None


def _profile_name() -> Optional[str]:
    conn = get_conn()
    row = conn.execute("SELECT name FROM user_profile WHERE id = 1").fetchone()
    name = ((row["name"] if row else None) or "").strip()
    return name or None


def compose_referral_message(
    connection: dict,
    company: str,
    match_kind: str,
    matched_company: Optional[str] = None,
    user_name: Optional[str] = None,
    job_title: Optional[str] = None,
) -> str:
    """Template-composed referral ask. Evidence-grounded: every proper noun in
    the output comes from stored data (connection name, company text, the
    user's profile name, the job title). Nothing is invented and no
    placeholder tokens are ever emitted; optional facts are simply omitted.
    """
    conn_name = (connection.get("name") or "").strip()
    first = conn_name.split()[0] if conn_name else "there"
    parts = [f"Hi {first},"]
    if user_name:
        parts.append(f"it's {user_name}.")
    if match_kind == "current":
        context = f"I saw you're at {company}"
    elif match_kind == "past":
        context = f"I saw you previously worked at {company}"
    elif match_kind == "fuzzy":
        if matched_company and _norm(matched_company) != _norm(company):
            context = f"I saw {matched_company} on your profile, which looks connected to {company}"
        else:
            context = f"I saw {company} on your profile"
    else:  # mention — the company only appears in their notes/role text
        context = f"I remembered {company} coming up in your background"
    parts.append(context + ".")
    if job_title:
        parts.append(
            f"I'm applying for the {job_title} role at {company} and would really "
            "appreciate a referral or an intro to the right person."
        )
    else:
        parts.append(
            f"I'm exploring opportunities at {company} and would really "
            "appreciate a referral or an intro to the right person."
        )
    parts.append("Happy to send over my resume and the job link. Thanks!")
    if user_name:
        parts.append(f"— {user_name}")
    return " ".join(parts)


def find_referrals(
    company: Optional[str] = None,
    job_id: Optional[int] = None,
    limit: int = 100,
) -> dict:
    """Rank the user's connections for referral likelihood at a company.

    Either `company` or `job_id` must be provided; when only `job_id` is
    given the company comes from that job posting, and the job title is used
    to ground the suggested message.

    Returns {"company", "job": {id,title,company}|None, "count",
    "results": [{connection, match_kind, matched_company, last_contacted_at,
    suggested_message}]} ordered current > past > fuzzy > mention, then most
    recently contacted first.
    """
    conn = get_conn()
    job: Optional[dict] = None
    if job_id is not None:
        row = conn.execute(
            "SELECT id, title, company FROM job_posting WHERE id = ?", (int(job_id),)
        ).fetchone()
        if row is None:
            raise LookupError(f"job {job_id} not found")
        job = dict(row)
    target = (company or (job or {}).get("company") or "").strip()
    if not target:
        raise ValueError("company is required (pass ?company= or a job_id whose posting has one)")

    user_name = _profile_name()
    job_title = (job or {}).get("title")
    results: list[dict] = []
    for entry in _load_network():
        match = _classify_match(entry, target)
        if not match:
            continue
        kind, matched_company = match
        c = entry["connection"]
        results.append({
            "connection": c,
            "match_kind": kind,
            "matched_company": matched_company,
            "last_contacted_at": c.get("last_contacted_at"),
            "suggested_message": compose_referral_message(
                c, target, kind, matched_company, user_name, job_title
            ),
        })
    results.sort(key=lambda r: (
        _KIND_PRIORITY[r["match_kind"]],
        -(r["last_contacted_at"] or 0),
        -(r["connection"].get("updated_at") or 0),
        r["connection"]["id"],
    ))
    results = results[: max(int(limit), 0)]
    return {"company": target, "job": job, "count": len(results), "results": results}


def companies_with_connections() -> list[dict]:
    """Companies from open jobs (job_posting status new/saved) where the user
    has at least one connection — lets the UI badge jobs 'referral available'.

    Returns [{company, job_ids, job_count, connection_count,
    match_kinds: {kind: count}}] sorted by connection_count desc.
    """
    conn = get_conn()
    placeholders = ",".join("?" for _ in _OPEN_JOB_STATUSES)
    rows = conn.execute(
        f"""SELECT id, company FROM job_posting
            WHERE status IN ({placeholders})
              AND company IS NOT NULL AND TRIM(company) != ''""",
        _OPEN_JOB_STATUSES,
    ).fetchall()
    grouped: dict[str, dict] = {}
    for r in rows:
        key = _norm(r["company"])
        entry = grouped.setdefault(key, {"company": (r["company"] or "").strip(), "job_ids": []})
        entry["job_ids"].append(int(r["id"]))

    network = _load_network()
    out: list[dict] = []
    for entry in grouped.values():
        kinds: dict[str, int] = {}
        count = 0
        for net in network:
            match = _classify_match(net, entry["company"])
            if match:
                count += 1
                kinds[match[0]] = kinds.get(match[0], 0) + 1
        if count:
            out.append({
                "company": entry["company"],
                "job_ids": sorted(entry["job_ids"]),
                "job_count": len(entry["job_ids"]),
                "connection_count": count,
                "match_kinds": kinds,
            })
    out.sort(key=lambda d: (-d["connection_count"], d["company"].lower()))
    return out


def referral_job_flags(job_ids: list[int]) -> dict[int, bool]:
    """has_referral flag per requested job id: True when the job's company
    matches >=1 connection (any match kind). Unknown ids and jobs without a
    company resolve to False so the UI can treat the map as total.
    """
    ids: list[int] = []
    seen: set[int] = set()
    for j in job_ids:
        jid = int(j)
        if jid not in seen:
            seen.add(jid)
            ids.append(jid)
    if not ids:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, company FROM job_posting WHERE id IN ({placeholders})",
        tuple(ids),
    ).fetchall()
    companies = {int(r["id"]): (r["company"] or "").strip() for r in rows}
    network = _load_network()
    cache: dict[str, bool] = {}
    flags: dict[int, bool] = {}
    for jid in ids:
        co = companies.get(jid, "")
        if not co:
            flags[jid] = False
            continue
        key = _norm(co)
        if key not in cache:
            cache[key] = any(_classify_match(net, co) for net in network)
        flags[jid] = cache[key]
    return flags


__all__ = [
    "add_connection",
    "list_connections",
    "get_connection",
    "update_connection",
    "delete_connection",
    "who_could_refer_at",
    "suggest_outreach",
    "find_referrals",
    "compose_referral_message",
    "companies_with_connections",
    "referral_job_flags",
]
