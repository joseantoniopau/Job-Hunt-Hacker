"""Daily "new jobs" digest — assembled from job_posting + job_match, bucketed
by the user's saved searches.

Surface:

  * ``assemble_digest(since_hours=24)`` -> structured dict
  * ``render_digest_text(digest)``      -> plaintext (email/Slack)
  * ``render_digest_html(digest)``      -> HTML (email body)
  * ``run_digest()`` -> end-to-end scheduler tick (writes files + posts to Slack)

The digest groups newly-discovered jobs under the saved-search whose query
best matches them (substring keyword check against title + company + location).
Jobs that don't match any saved search bucket go under "(unattributed)" so
the user still sees them.

Files are written into ``data/digests/digest-<iso-date>.html`` and ``.txt``.
We never auto-send email; we just write a ``.eml`` envelope so the user
can forward it manually if they want.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from ..config import settings
from ..db import audit, get_conn, row_to_dict

log = logging.getLogger("jhh.integrations.digest")


# ---------------- assembly ----------------

def _decode_query(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _load_saved_searches() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, label, query_json, enabled FROM saved_search ORDER BY id ASC"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("saved_search table unavailable: %s", exc)
        return []
    out: list[dict] = []
    for r in rows:
        d = row_to_dict(r) or {}
        if not d.get("enabled"):
            continue
        d["query"] = _decode_query(d.get("query_json"))
        out.append(d)
    return out


def _query_keywords(query: dict) -> list[str]:
    raw = (query.get("query") or "").lower()
    # Split on common boolean / punctuation separators; keep tokens >= 3 chars.
    tokens = [t for t in re.split(r"[\s,;|()/+]+", raw) if len(t) >= 3]
    # Drop boolean noise so we don't match every job containing "and" / "or"
    noise = {"and", "the", "for", "with", "any", "all", "not"}
    return [t for t in tokens if t not in noise]


def _matches_saved_search(job: dict, ss_keywords: list[str]) -> bool:
    if not ss_keywords:
        return False
    haystack = " ".join([
        (job.get("title") or "").lower(),
        (job.get("company") or "").lower(),
        (job.get("location") or "").lower(),
        (job.get("description") or "").lower()[:1500],
    ])
    return any(kw in haystack for kw in ss_keywords)


def _load_new_jobs(since_hours: int) -> list[dict]:
    """Fetch jobs discovered within the last ``since_hours``, joined to score."""
    cutoff = time.time() - max(1, int(since_hours)) * 3600
    sql = (
        "SELECT j.id, j.title, j.company, j.location, j.source, j.apply_url, "
        "j.discovered_at, j.description, m.overall_score "
        "FROM job_posting j "
        "LEFT JOIN job_match m ON m.job_id = j.id "
        "WHERE j.discovered_at IS NOT NULL AND j.discovered_at >= ? "
        "ORDER BY COALESCE(m.overall_score, 0) DESC, j.discovered_at DESC"
    )
    conn = get_conn()
    rows = conn.execute(sql, (cutoff,)).fetchall()
    out: list[dict] = []
    for r in rows:
        d = row_to_dict(r) or {}
        # Pre-clip description so it doesn't bloat the digest payload.
        if d.get("description"):
            d["description"] = (d["description"] or "")[:400]
        out.append(d)
    return out


def assemble_digest(since_hours: int = 24) -> dict:
    """Build the digest payload for the last ``since_hours`` of discovery."""
    saved = _load_saved_searches()
    jobs = _load_new_jobs(since_hours)

    by_search: dict[str, list[dict]] = {}
    attributed: set[int] = set()
    if saved:
        # Compile keyword lists once
        kw_by_label: list[tuple[str, list[str]]] = []
        for ss in saved:
            label = (ss.get("label") or f"search_{ss.get('id')}").strip()
            kws = _query_keywords(ss.get("query") or {})
            kw_by_label.append((label, kws))
            by_search.setdefault(label, [])
        for j in jobs:
            for label, kws in kw_by_label:
                if _matches_saved_search(j, kws):
                    by_search[label].append({
                        "job_id": int(j["id"]),
                        "title": j.get("title") or "",
                        "company": j.get("company") or "",
                        "location": j.get("location") or "",
                        "source": j.get("source") or "",
                        "score": round(float(j.get("overall_score") or 0.0), 2),
                        "url": j.get("apply_url") or "",
                    })
                    attributed.add(int(j["id"]))
        # Drop empty buckets so the digest stays tidy
        by_search = {label: items for label, items in by_search.items() if items}

    # Any job not bucketed into a saved search goes into "(unattributed)"
    unattributed = [j for j in jobs if int(j["id"]) not in attributed]
    if unattributed:
        by_search.setdefault("(unattributed)", [])
        for j in unattributed:
            by_search["(unattributed)"].append({
                "job_id": int(j["id"]),
                "title": j.get("title") or "",
                "company": j.get("company") or "",
                "location": j.get("location") or "",
                "source": j.get("source") or "",
                "score": round(float(j.get("overall_score") or 0.0), 2),
                "url": j.get("apply_url") or "",
            })

    top_10 = sorted(
        [
            {
                "job_id": int(j["id"]),
                "title": j.get("title") or "",
                "company": j.get("company") or "",
                "location": j.get("location") or "",
                "source": j.get("source") or "",
                "score": round(float(j.get("overall_score") or 0.0), 2),
                "url": j.get("apply_url") or "",
            }
            for j in jobs
        ],
        key=lambda r: (-float(r.get("score") or 0.0), r.get("title") or ""),
    )[:10]

    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "since_hours": int(since_hours),
        "total_new_jobs": len(jobs),
        "saved_searches": [
            {"id": int(ss.get("id")), "label": ss.get("label") or ""}
            for ss in saved
        ],
        "by_search": by_search,
        "top_10_overall": top_10,
    }


# ---------------- rendering ----------------

def render_digest_text(digest: dict) -> str:
    lines: list[str] = []
    gen = digest.get("generated_at") or ""
    n = int(digest.get("total_new_jobs") or 0)
    since = int(digest.get("since_hours") or 24)
    lines.append(f"Job Hunt Hacker — daily digest ({gen})")
    lines.append("=" * 60)
    lines.append(f"New jobs in the last {since}h: {n}")
    if n == 0:
        lines.append("")
        lines.append("Nothing new today. Take the win.")
        return "\n".join(lines)

    lines.append("")
    lines.append("TOP 10 OVERALL")
    lines.append("-" * 60)
    for row in digest.get("top_10_overall") or []:
        score = row.get("score")
        score_s = f"{score:>5}" if score is not None else "  ?  "
        title = row.get("title") or ""
        company = row.get("company") or ""
        loc = row.get("location") or ""
        url = row.get("url") or ""
        lines.append(f"  [{score_s}] {title} @ {company}  ({loc})")
        if url:
            lines.append(f"           {url}")
    lines.append("")

    lines.append("BY SAVED SEARCH")
    lines.append("-" * 60)
    by_search = digest.get("by_search") or {}
    for label, jobs in by_search.items():
        lines.append(f"  # {label} ({len(jobs)})")
        for row in jobs[:10]:
            score = row.get("score")
            score_s = f"{score:>5}" if score is not None else "  ?  "
            lines.append(
                f"    - [{score_s}] {row.get('title') or ''} @ {row.get('company') or ''}"
            )
            if row.get("url"):
                lines.append(f"        {row['url']}")
        if len(jobs) > 10:
            lines.append(f"    … and {len(jobs) - 10} more")
        lines.append("")
    return "\n".join(lines)


def render_digest_html(digest: dict) -> str:
    n = int(digest.get("total_new_jobs") or 0)
    since = int(digest.get("since_hours") or 24)
    gen = html.escape(str(digest.get("generated_at") or ""))

    def _row(r: dict) -> str:
        title = html.escape(r.get("title") or "")
        company = html.escape(r.get("company") or "")
        loc = html.escape(r.get("location") or "")
        url = r.get("url") or ""
        score = r.get("score")
        score_s = f"{score}" if score is not None else "?"
        if url:
            title_html = f'<a href="{html.escape(url)}">{title}</a>'
        else:
            title_html = title
        return (
            f"<li><strong>[{score_s}]</strong> {title_html} "
            f"<em>@ {company}</em> <small>({loc})</small></li>"
        )

    parts: list[str] = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        "<title>Job Hunt Hacker — daily digest</title>",
        "<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;",
        "max-width:780px;margin:24px auto;padding:0 16px;color:#111}",
        "h1{margin-bottom:0}h2{margin-top:32px;border-bottom:1px solid #ddd;",
        "padding-bottom:4px}ul{padding-left:20px}li{margin:6px 0}",
        "small{color:#666}.empty{color:#666;font-style:italic}</style>",
        "</head><body>",
        f"<h1>Job Hunt Hacker — daily digest</h1>",
        f"<p><small>Generated {gen} &middot; window: last {since}h</small></p>",
        f"<p><strong>{n}</strong> new jobs discovered.</p>",
    ]

    if n == 0:
        parts.append("<p class='empty'>Nothing new today. Take the win.</p>")
        parts.append("</body></html>")
        return "\n".join(parts)

    top = digest.get("top_10_overall") or []
    if top:
        parts.append("<h2>Top 10 overall</h2><ul>")
        parts.extend(_row(r) for r in top)
        parts.append("</ul>")

    by_search = digest.get("by_search") or {}
    if by_search:
        parts.append("<h2>By saved search</h2>")
        for label, jobs in by_search.items():
            parts.append(f"<h3>{html.escape(label)} ({len(jobs)})</h3><ul>")
            parts.extend(_row(r) for r in jobs[:10])
            parts.append("</ul>")

    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------- end-to-end tick ----------------

def _digests_dir() -> Path:
    d = Path(settings.data_dir) / "digests"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_eml(html_body: str, text_body: str, when: _dt.date) -> Optional[Path]:
    """Write a multipart .eml file the user can forward. Never auto-sends."""
    try:
        import email.message as _em
        msg = _em.EmailMessage()
        msg["Subject"] = f"Job Hunt Hacker — digest {when.isoformat()}"
        msg["From"] = "jhh@localhost"
        msg["To"] = "you@localhost"
        msg.set_content(text_body or "(empty)")
        msg.add_alternative(html_body or "<html/>", subtype="html")
        path = _digests_dir() / f"digest-{when.isoformat()}.eml"
        path.write_bytes(bytes(msg))
        return path
    except Exception as exc:  # noqa: BLE001
        log.warning("digest .eml write failed: %s", exc)
        return None


def _safe_slack_post(summary: str) -> bool:
    try:
        from . import slack as _slack
        if not _slack.is_configured():
            return False
        return bool(_slack.post(summary))
    except Exception as exc:  # noqa: BLE001
        log.warning("slack notify failed: %s", exc)
        return False


def run_digest(since_hours: int = 24) -> dict:
    """End-to-end: assemble, render, write files, optionally notify Slack.

    Skips file writes when no new jobs were discovered. Returns a summary
    dict suitable for audit + scheduler reporting.
    """
    digest = assemble_digest(int(since_hours))
    n = int(digest.get("total_new_jobs") or 0)
    today = _dt.date.today()

    if n == 0:
        try:
            audit("digest_skipped", "system", since_hours=int(since_hours), total_new_jobs=0)
        except Exception:
            pass
        return {"ok": True, "skipped": True, "reason": "no_new_jobs",
                "total_new_jobs": 0, "since_hours": int(since_hours)}

    text_body = render_digest_text(digest)
    html_body = render_digest_html(digest)

    base = _digests_dir()
    html_path = base / f"digest-{today.isoformat()}.html"
    txt_path = base / f"digest-{today.isoformat()}.txt"
    try:
        html_path.write_text(html_body, encoding="utf-8")
        txt_path.write_text(text_body, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("digest file write failed: %s", exc)

    eml_path = _write_eml(html_body, text_body, today)

    slack_summary = (
        f":briefcase: Job Hunt Hacker digest — {n} new jobs in the last "
        f"{int(since_hours)}h. Top: "
        + ", ".join(
            f"{r.get('title') or '?'} @ {r.get('company') or '?'} "
            f"({r.get('score')})"
            for r in (digest.get("top_10_overall") or [])[:3]
        )
    )
    slack_ok = _safe_slack_post(slack_summary)

    try:
        audit(
            "digest_run", "system",
            since_hours=int(since_hours),
            total_new_jobs=n,
            html_path=str(html_path),
            txt_path=str(txt_path),
            eml_path=str(eml_path) if eml_path else None,
            slack_posted=slack_ok,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "skipped": False,
        "total_new_jobs": n,
        "since_hours": int(since_hours),
        "html_path": str(html_path),
        "txt_path": str(txt_path),
        "eml_path": str(eml_path) if eml_path else None,
        "slack_posted": slack_ok,
    }


__all__ = [
    "assemble_digest",
    "render_digest_text",
    "render_digest_html",
    "run_digest",
]
