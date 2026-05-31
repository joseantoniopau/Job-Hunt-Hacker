"""High-level Career Vault operations: sources, claims, retrieval, summary."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from ..db import audit, get_conn, row_to_dict, tx
from ..utils.text import content_hash, normalize
from . import vector_store

log = logging.getLogger("jhh.evidence")


def _now() -> float:
    return time.time()


def _hash_for(source_type: str, url: Optional[str], filename: Optional[str],
              raw_text: str) -> str:
    parts = [source_type or "", url or "", filename or "", raw_text or ""]
    return content_hash(*parts)


def add_source(source_type: str, *,
               title: Optional[str] = None,
               filename: Optional[str] = None,
               url: Optional[str] = None,
               raw_text: str = "",
               parsed_json: Optional[dict] = None) -> int:
    """Insert an evidence_source row (dedup by content_hash). Returns id."""
    raw_text = raw_text or ""
    h = _hash_for(source_type, url, filename, raw_text)
    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM evidence_source WHERE content_hash = ?", (h,)
    ).fetchone()
    if existing:
        log.info("evidence dedup hit: source_type=%s id=%d", source_type, existing["id"])
        return int(existing["id"])

    now = _now()
    parsed_blob = json.dumps(parsed_json) if parsed_json is not None else None
    with tx() as c:
        cur = c.execute(
            "INSERT INTO evidence_source "
            "(source_type, title, filename, url, raw_text, parsed_json, "
            "content_hash, ingestion_status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_type, title, filename, url, raw_text, parsed_blob,
             h, "parsed", now, now),
        )
        source_id = int(cur.lastrowid)

    # vector for retrieval (best effort)
    try:
        if raw_text.strip():
            vector_store.add("evidence", source_id, raw_text[:4000])
    except Exception as e:  # noqa: BLE001
        log.warning("evidence embedding failed for %d: %s", source_id, e)

    audit("evidence_added", "evidence_source", source_id,
          source_type=source_type, title=title, url=url, filename=filename)
    return source_id


def add_claims(source_id: int, claims: list[dict]) -> list[int]:
    """Insert claims and add per-claim embeddings. Returns inserted ids.

    Dedupes by (source_id, claim_type, normalized_claim) so re-ingesting
    the same source doesn't pile up duplicate claims. add_source already
    dedupes by content_hash, but autopilot can re-run extraction on a
    cached source and would otherwise multiply the vault by N every run.
    """
    if not claims:
        return []
    now = _now()
    ids: list[int] = []
    conn = get_conn()

    # Pre-load existing (claim_type, normalized_claim) pairs for this source
    # so we can skip duplicates without doing a per-row lookup.
    existing: set[tuple[str, str]] = set()
    try:
        for r in conn.execute(
            "SELECT claim_type, normalized_claim FROM career_claim WHERE source_id = ?",
            (int(source_id),),
        ).fetchall():
            existing.add(((r[0] or "").lower(), (r[1] or "").lower()))
    except Exception:
        pass

    skipped = 0
    # Remember (claim_id, original_claim_dict) only for rows we actually
    # inserted, so the embedding loop below operates on the right pairs.
    inserted_pairs: list[tuple[int, dict]] = []
    with tx() as c:
        for claim in claims:
            ctype = (claim.get("claim_type") or "responsibility").lower()
            norm = (claim.get("normalized_claim")
                    or normalize(claim.get("claim_text") or "")).lower()
            key = (ctype, norm)
            if not norm or key in existing:
                skipped += 1
                continue
            cur = c.execute(
                "INSERT INTO career_claim "
                "(source_id, claim_type, claim_text, normalized_claim, "
                "date_start, date_end, employer, project, skill, tool, "
                "confidence, evidence_strength, user_verified, "
                "allowed_for_resume, contradiction_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    claim.get("claim_type") or "responsibility",
                    claim.get("claim_text") or "",
                    claim.get("normalized_claim") or normalize(claim.get("claim_text") or ""),
                    claim.get("date_start"),
                    claim.get("date_end"),
                    claim.get("employer"),
                    claim.get("project"),
                    claim.get("skill"),
                    claim.get("tool"),
                    float(claim.get("confidence") or 0.5),
                    claim.get("evidence_strength") or "medium",
                    1 if claim.get("user_verified") else 0,
                    1 if claim.get("allowed_for_resume", True) else 0,
                    claim.get("contradiction_status") or "none",
                    now,
                ),
            )
            cid = int(cur.lastrowid)
            ids.append(cid)
            inserted_pairs.append((cid, claim))
            existing.add(key)

    for claim_id, claim in inserted_pairs:
        try:
            vector_store.add("claim", claim_id, claim.get("claim_text") or "")
        except Exception as e:  # noqa: BLE001
            log.warning("claim embedding failed for %d: %s", claim_id, e)

    audit("claims_added", "evidence_source", source_id,
          inserted=len(ids), skipped_duplicates=skipped)
    return ids


def list_sources() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT s.*, "
        "(SELECT COUNT(*) FROM career_claim WHERE source_id = s.id) AS claim_count "
        "FROM evidence_source s ORDER BY created_at DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        out.append(d)
    return out


def get_source(source_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM evidence_source WHERE id = ?", (source_id,)
    ).fetchone()
    if row is None:
        return None
    d = row_to_dict(row)
    d["claims"] = list_claims(source_id=source_id)
    return d


def list_claims(source_id: Optional[int] = None,
                claim_type: Optional[str] = None,
                allowed_only: bool = False,
                verified_only: bool = False) -> list[dict]:
    conn = get_conn()
    where = []
    params: list[Any] = []
    if source_id is not None:
        where.append("source_id = ?")
        params.append(source_id)
    if claim_type:
        where.append("claim_type = ?")
        params.append(claim_type)
    if allowed_only:
        where.append("allowed_for_resume = 1")
    if verified_only:
        where.append("user_verified = 1")
    sql = "SELECT * FROM career_claim"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(r) for r in rows]


def update_claim(claim_id: int, fields: dict) -> dict:
    """Update verified/allowed/text/etc. on a claim. Returns the updated row."""
    if not fields:
        existing = get_conn().execute(
            "SELECT * FROM career_claim WHERE id = ?", (claim_id,)
        ).fetchone()
        if not existing:
            raise ValueError(f"claim {claim_id} not found")
        return row_to_dict(existing)

    allowed = {
        "user_verified", "allowed_for_resume", "claim_text",
        "normalized_claim", "confidence", "evidence_strength",
        "contradiction_status", "date_start", "date_end",
        "employer", "project", "skill", "tool",
    }
    sets = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("user_verified", "allowed_for_resume"):
            vals.append(1 if v else 0)
        elif k == "confidence" and v is not None:
            vals.append(float(v))
        else:
            vals.append(v)
        sets.append(f"{k} = ?")

    if not sets:
        existing = get_conn().execute(
            "SELECT * FROM career_claim WHERE id = ?", (claim_id,)
        ).fetchone()
        if not existing:
            raise ValueError(f"claim {claim_id} not found")
        return row_to_dict(existing)

    vals.append(claim_id)
    with tx() as c:
        c.execute(f"UPDATE career_claim SET {', '.join(sets)} WHERE id = ?", vals)

    # If claim text changed, refresh the embedding
    if "claim_text" in fields and fields.get("claim_text"):
        try:
            from . import vector_store as vs
            vs.remove("claim", claim_id)
            vs.add("claim", claim_id, fields["claim_text"])
        except Exception as e:  # noqa: BLE001
            log.warning("embedding refresh failed for claim %d: %s", claim_id, e)

    audit("claim_updated", "career_claim", claim_id, fields=list(fields.keys()))
    row = get_conn().execute(
        "SELECT * FROM career_claim WHERE id = ?", (claim_id,)
    ).fetchone()
    return row_to_dict(row)


def delete_claim(claim_id: int) -> int:
    with tx() as c:
        cur = c.execute("DELETE FROM career_claim WHERE id = ?", (claim_id,))
        n = cur.rowcount
    try:
        vector_store.remove("claim", claim_id)
    except Exception:
        pass
    audit("claim_deleted", "career_claim", claim_id)
    return int(n)


def delete_source(source_id: int) -> int:
    # Collect claim ids for embedding cleanup
    conn = get_conn()
    claim_rows = conn.execute(
        "SELECT id FROM career_claim WHERE source_id = ?", (source_id,)
    ).fetchall()
    claim_ids = [int(r["id"]) for r in claim_rows]
    with tx() as c:
        cur = c.execute("DELETE FROM evidence_source WHERE id = ?", (source_id,))
        n = cur.rowcount
    # FK cascades claims; clean embeddings
    try:
        vector_store.remove("evidence", source_id)
        for cid in claim_ids:
            vector_store.remove("claim", cid)
    except Exception:
        pass
    audit("evidence_deleted", "evidence_source", source_id, claim_count=len(claim_ids))
    return int(n)


def retrieve_for_job(job_text: str, top: int = 15) -> list[dict]:
    """Semantic search over claims; returns claims with source title/url."""
    job_text = (job_text or "").strip()
    if not job_text:
        return []
    results = vector_store.search(job_text, owner_type="claim", top=top)
    if not results:
        return []
    conn = get_conn()
    out: list[dict] = []
    for r in results:
        claim_id = int(r["owner_id"])
        claim_row = conn.execute(
            "SELECT * FROM career_claim WHERE id = ?", (claim_id,)
        ).fetchone()
        if claim_row is None:
            continue
        claim = row_to_dict(claim_row)
        src = conn.execute(
            "SELECT id, source_type, title, url, filename "
            "FROM evidence_source WHERE id = ?",
            (claim["source_id"],),
        ).fetchone()
        claim["evidence"] = row_to_dict(src) if src else None
        claim["match_score"] = r["score"]
        out.append(claim)
    return out


def summary() -> dict:
    """Counts by type, verified vs unverified, recent additions."""
    conn = get_conn()

    def scalar(sql: str, *params) -> int:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    sources_total = scalar("SELECT COUNT(*) FROM evidence_source")
    claims_total = scalar("SELECT COUNT(*) FROM career_claim")
    verified = scalar("SELECT COUNT(*) FROM career_claim WHERE user_verified = 1")
    allowed = scalar("SELECT COUNT(*) FROM career_claim WHERE allowed_for_resume = 1")
    suspected = scalar(
        "SELECT COUNT(*) FROM career_claim WHERE contradiction_status = 'suspected'"
    )

    by_type: dict[str, int] = {}
    for r in conn.execute(
        "SELECT claim_type, COUNT(*) AS n FROM career_claim GROUP BY claim_type"
    ).fetchall():
        by_type[r["claim_type"]] = int(r["n"])

    by_source_type: dict[str, int] = {}
    for r in conn.execute(
        "SELECT source_type, COUNT(*) AS n FROM evidence_source GROUP BY source_type"
    ).fetchall():
        by_source_type[r["source_type"]] = int(r["n"])

    recent_sources = []
    for r in conn.execute(
        "SELECT id, source_type, title, url, filename, created_at "
        "FROM evidence_source ORDER BY created_at DESC LIMIT 5"
    ).fetchall():
        recent_sources.append(row_to_dict(r))

    recent_claims = []
    for r in conn.execute(
        "SELECT id, claim_type, claim_text, source_id, created_at "
        "FROM career_claim ORDER BY created_at DESC LIMIT 10"
    ).fetchall():
        recent_claims.append(row_to_dict(r))

    return {
        "sources_total": sources_total,
        "claims_total": claims_total,
        "verified_claims": verified,
        "unverified_claims": claims_total - verified,
        "allowed_for_resume": allowed,
        "suspected_contradictions": suspected,
        "claims_by_type": by_type,
        "sources_by_type": by_source_type,
        "recent_sources": recent_sources,
        "recent_claims": recent_claims,
    }
