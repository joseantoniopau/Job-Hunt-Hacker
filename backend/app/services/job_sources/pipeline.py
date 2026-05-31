"""Pipeline: orchestrate multi-source search, persistence, and read access."""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import time
from dataclasses import asdict
from typing import Any

from ...db import audit, get_conn, row_to_dict, tx
from . import cache as _cache
from .base import JobRecord, JobSearchQuery, REGISTRY
from .retry import wrap_with_retry

log = logging.getLogger("jhh.sources.pipeline")

_ADAPTER_TIMEOUT_S = 30
_MAX_WORKERS = 8

# Per-adapter cache TTLs. JobSpy hits LinkedIn/Indeed which rate-limit
# aggressively, so we cache shorter and re-fetch less.
_CACHE_TTL_OVERRIDES: dict[str, int] = {"jobspy": 300}
_CACHE_TTL_DEFAULT = 3600


def _ttl_for(name: str) -> int:
    return _CACHE_TTL_OVERRIDES.get(name, _CACHE_TTL_DEFAULT)


# ---------------------------------------------------------------- search ----

def _run_one(name: str, adapter: Any, q: JobSearchQuery) -> tuple[str, list[JobRecord], str | None, bool]:
    """Return (name, records, error, cache_hit)."""
    try:
        if not adapter.healthy():
            return name, [], "unhealthy", False
        cached = _cache.get(name, q)
        if cached is not None:
            return name, list(cached), None, True
        retrying = wrap_with_retry(adapter.search)
        recs = retrying(q) or []
        recs = list(recs)
        try:
            _cache.set(name, q, recs, ttl=_ttl_for(name))
        except Exception as exc:  # noqa: BLE001
            log.debug("cache write skipped for %s: %s", name, exc)
        return name, recs, None, False
    except Exception as exc:  # noqa: BLE001
        log.warning("adapter %s raised: %s", name, exc)
        return name, [], f"{type(exc).__name__}: {exc}", False


def _is_source_disabled(source: str) -> bool:
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT enabled FROM source_state WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            return False
        return int(row[0]) == 0
    except Exception:
        return False


def _mark_source(name: str, error: str | None) -> None:
    try:
        with tx() as conn:
            conn.execute(
                "INSERT INTO source_state (source, enabled, last_run_at, last_error) "
                "VALUES (?, 1, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET last_run_at=excluded.last_run_at, "
                "last_error=excluded.last_error",
                (name, time.time(), error),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("source_state update failed for %s: %s", name, exc)


def search_all(q: JobSearchQuery, sites: list[str]) -> dict:
    """Fan out to requested adapters in parallel; return aggregated records.

    Returns: {records, per_source, errors}.
    """
    records: list[JobRecord] = []
    per_source: dict[str, int] = {}
    cache_hits: dict[str, bool] = {}
    errors: dict[str, str] = {}

    requested = [s for s in (sites or []) if s in REGISTRY]
    if not requested:
        # default: every healthy adapter
        requested = [n for n, a in REGISTRY.items() if _safe_healthy(a)]

    if not requested:
        return {"records": [], "per_source": {}, "errors": {}, "cache_hits": {}}

    enabled = [n for n in requested if not _is_source_disabled(n)]
    for n in set(requested) - set(enabled):
        errors[n] = "disabled"

    with cf.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_run_one, name, REGISTRY[name], q): name for name in enabled
        }
        for fut in cf.as_completed(futures):
            name = futures[fut]
            try:
                _, recs, err, cache_hit = fut.result(timeout=_ADAPTER_TIMEOUT_S)
            except cf.TimeoutError:
                errors[name] = "timeout"
                _mark_source(name, "timeout")
                continue
            except Exception as exc:  # noqa: BLE001
                errors[name] = f"{type(exc).__name__}: {exc}"
                _mark_source(name, errors[name])
                continue
            if err:
                errors[name] = err
            per_source[name] = len(recs)
            cache_hits[name] = bool(cache_hit)
            records.extend(recs)
            _mark_source(name, err)

    try:
        audit(
            "search",
            "job_posting",
            None,
            query=asdict(q),
            per_source=per_source,
            cache_hits=cache_hits,
            errors=errors,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("audit failed: %s", exc)

    return {
        "records": records,
        "per_source": per_source,
        "cache_hits": cache_hits,
        "errors": errors,
    }


def _safe_healthy(adapter: Any) -> bool:
    try:
        return bool(adapter.healthy())
    except Exception:
        return False


# ------------------------------------------------------------- persist ----

def persist(records: list[JobRecord]) -> dict:
    """Insert each record; UNIQUE(hash) collapses duplicates."""
    inserted = 0
    duplicates = 0
    ids: list[int] = []
    now = time.time()
    seen: set[str] = set()
    with tx() as conn:
        for rec in records:
            h = rec.hash()
            if h in seen:
                duplicates += 1
                continue
            seen.add(h)
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO job_posting
                    (external_id, source, title, company, location, remote_type,
                     employment_type, salary_min, salary_max, currency,
                     bonus_equity_text, description, requirements, benefits,
                     apply_url, company_url, posted_at, discovered_at, raw_json,
                     hash, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
                    """,
                    (
                        rec.external_id,
                        rec.source,
                        rec.title,
                        rec.company,
                        rec.location,
                        rec.remote_type,
                        rec.employment_type,
                        rec.salary_min,
                        rec.salary_max,
                        rec.currency,
                        rec.bonus_equity_text,
                        rec.description,
                        json.dumps(rec.requirements or []),
                        json.dumps(rec.benefits or []),
                        rec.apply_url,
                        rec.company_url,
                        rec.posted_at,
                        now,
                        json.dumps(rec.raw or {}, default=str),
                        h,
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                    ids.append(int(cur.lastrowid))
                else:
                    duplicates += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("persist failed for %s/%s: %s", rec.source, rec.title, exc)
                continue
    return {"inserted": inserted, "duplicates": duplicates, "ids": ids}


# ----------------------------------------------------------------- read ----

_LIST_BASE_SQL = (
    "SELECT j.*, m.overall_score, m.skills_score, m.salary_score, m.explanation "
    "FROM job_posting j "
    "LEFT JOIN job_match m ON m.job_id = j.id "
)


def list_jobs(
    limit: int = 50,
    status: str | None = None,
    source: str | None = None,
    min_score: int | None = None,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("j.status = ?")
        params.append(status)
    if source:
        where.append("j.source LIKE ?")
        params.append(f"{source}%")
    if min_score is not None:
        where.append("(m.overall_score IS NOT NULL AND m.overall_score >= ?)")
        params.append(float(min_score))
    sql = _LIST_BASE_SQL
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY COALESCE(m.overall_score, 0) DESC, j.discovered_at DESC LIMIT ? OFFSET ?"
    params.extend([int(limit), int(offset)])
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(r) for r in rows]


def get_job(job_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT j.*, m.overall_score, m.skills_score, m.experience_score, "
        "m.salary_score, m.location_score, m.seniority_score, m.keyword_score, "
        "m.evidence_score, m.explanation, m.matched_keywords, m.transferable_keywords, "
        "m.missing_keywords, m.unsupported_keywords, m.red_flags, "
        "m.recommended_resume_strategy "
        "FROM job_posting j LEFT JOIN job_match m ON m.job_id = j.id "
        "WHERE j.id = ?",
        (int(job_id),),
    ).fetchone()
    return row_to_dict(row) if row else None


def update_status(job_id: int, status: str) -> bool:
    cascade_apps = 0
    with tx() as conn:
        cur = conn.execute(
            "UPDATE job_posting SET status = ? WHERE id = ?", (status, int(job_id))
        )
        ok = cur.rowcount > 0
        # Cascade: when a job is archived/deleted, also archive its active
        # applications so they don't render as "ghost" cards in the kanban
        # with a job that no longer exists in search results.
        if ok and status in ("archived", "deleted"):
            cur2 = conn.execute(
                "UPDATE application SET status = 'archived' "
                "WHERE job_id = ? AND status NOT IN ('archived', 'rejected', 'offer')",
                (int(job_id),),
            )
            cascade_apps = cur2.rowcount or 0
    if ok:
        try:
            audit("job_status_update", "job_posting", int(job_id),
                  status=status, cascaded_applications=cascade_apps)
        except Exception:
            pass
    return ok


# -------------------------------------------------------------- refresh ----

def refresh_remoteintech(limit: int | None = None, quiet: bool = False) -> int:
    """Refresh the remoteintech seed; returns the number of entries written.

    Importable for CLI script; uses GitHub Contents API with raw.github fallback.
    """
    from ...config import settings as _settings

    import httpx

    api_url = "https://api.github.com/repos/remoteintech/remote-jobs/contents/src/companies"
    raw_base = "https://raw.githubusercontent.com/remoteintech/remote-jobs/main/src/companies"
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "jhh/0.1"}
    if _settings.github_token:
        headers["Authorization"] = f"Bearer {_settings.github_token}"

    seed_dir = _settings.data_dir / "seed"
    seed_dir.mkdir(parents=True, exist_ok=True)
    out_path = seed_dir / "companies_remoteintech.json"

    entries: list[dict] = []
    with httpx.Client(timeout=30, headers=headers) as client:
        try:
            r = client.get(api_url)
            if r.status_code != 200:
                log.warning("contents API -> %s; keeping existing seed", r.status_code)
                return _count_existing(out_path)
            listing = r.json() or []
        except Exception as exc:  # noqa: BLE001
            log.warning("contents API failed: %s", exc)
            return _count_existing(out_path)

        names = [item.get("name") for item in listing if isinstance(item, dict) and item.get("name", "").endswith(".md")]
        if limit:
            names = names[: int(limit)]
        if not quiet:
            log.info("refreshing %d remoteintech companies", len(names))
        for n in names:
            slug = n[:-3]
            try:
                rr = client.get(f"{raw_base}/{n}")
                if rr.status_code != 200:
                    continue
                parsed = _parse_remoteintech_md(rr.text, slug)
                if parsed:
                    entries.append(parsed)
            except Exception as exc:  # noqa: BLE001
                log.debug("skip %s: %s", n, exc)
                continue

    if not entries:
        return _count_existing(out_path)
    out_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    if not quiet:
        log.info("wrote %d entries to %s", len(entries), out_path)
    return len(entries)


def _count_existing(path) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return len(data)
    except Exception:
        pass
    return 0


def _parse_remoteintech_md(text: str, slug: str) -> dict | None:
    """Parse `---\nkey: value\n---\n# Title\n...` style files."""
    if not text:
        return None
    out: dict[str, Any] = {"slug": slug}
    rest = text
    if text.startswith("---"):
        try:
            _, fm, rest = text.split("---", 2)
        except ValueError:
            fm, rest = "", text
        for line in fm.splitlines():
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            k = k.strip().lower().replace(" ", "_")
            v = v.strip().strip("'").strip('"')
            if not k:
                continue
            if k in ("technologies", "tags", "tech"):
                items = [s.strip() for s in v.strip("[]").split(",") if s.strip()]
                out["technologies"] = items
            else:
                out[k] = v
    # title from first heading
    title = ""
    website = ""
    careers_url = ""
    region = out.get("region") or ""
    remote_policy = ""
    for line in (rest or "").splitlines():
        s = line.strip()
        if not title and s.startswith("# "):
            title = s[2:].strip()
            continue
        ls = s.lower()
        if ls.startswith("- **website**") or ls.startswith("**website**"):
            website = _extract_url(s)
        elif ls.startswith("- **careers**") or ls.startswith("**careers**") or "careers" in ls and "http" in ls and not careers_url:
            careers_url = _extract_url(s) or careers_url
        elif ls.startswith("- **region**") or ls.startswith("**region**"):
            region = _extract_value(s) or region
        elif ls.startswith("- **company size**") or ls.startswith("**company size**"):
            out["company_size"] = _extract_value(s)
        elif ls.startswith("- **remote status**") or ls.startswith("**remote status**"):
            remote_policy = _extract_value(s)
    out["title"] = title or slug
    out["website"] = website or out.get("website", "")
    out["careers_url"] = careers_url or out.get("careers_url", out.get("website", ""))
    out["region"] = region
    out["remote_policy"] = remote_policy or out.get("remote_policy", "")
    out.setdefault("technologies", [])
    return out


def _extract_url(s: str) -> str:
    import re

    m = re.search(r"https?://[^\s)\]]+", s)
    return m.group(0) if m else ""


def _extract_value(s: str) -> str:
    if ":" in s:
        return s.split(":", 1)[1].strip().strip("*").strip()
    return ""
