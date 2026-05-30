"""POST /api/autopilot/start — the "I'm lazy, just find me a job" path.

Hands the user a working pipeline from a single dropped resume (or LinkedIn
URL or paste). Chains: parse → infer profile → save profile → ingest
evidence → run search → score every job → tailor top N → build packets for
top M → register a daily saved-search so new postings flow in automatically.

The user can walk away. When they come back the Pipeline tab is populated.

Defaults are deliberately opinionated. Power users can override per call:
    POST /api/autopilot/start
        multipart:
            resume_file       — file upload (optional)
            linkedin_text     — paste (optional)
            linkedin_url      — string (optional)
            github_url        — string (optional)
            portfolio_url     — string (optional)
            tailor_top        — int, default 5
            packet_top        — int, default 3
            search_results    — int, default 25
            search_hours_old  — int, default 168 (1 week)
            min_score         — int, default 0 (score everything)
            daily_recurrence  — bool, default True (creates a saved search)
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, File, Form, UploadFile

from ..db import audit, get_conn

log = logging.getLogger("jhh.autopilot")

router = APIRouter(prefix="/api", tags=["autopilot"])


@router.post("/autopilot/start")
async def autopilot_start(
    resume_file: UploadFile | None = File(default=None),
    linkedin_text: str | None = Form(default=None),
    linkedin_url: str | None = Form(default=None),
    github_url: str | None = Form(default=None),
    portfolio_url: str | None = Form(default=None),
    tailor_top: int = Form(default=5),
    packet_top: int = Form(default=3),
    search_results: int = Form(default=25),
    search_hours_old: int = Form(default=168),
    min_score: int = Form(default=0),
    daily_recurrence: bool = Form(default=True),
    sites: str = Form(default="indeed,glassdoor,google,greenhouse,lever,ashby,remotive,wwr"),
) -> dict:
    started = time.time()
    summary: dict[str, Any] = {
        "steps": [],
        "started_at": started,
        "profile": {"inferred_fields": [], "saved": False},
        "vault": {"sources_added": 0, "claims_extracted": 0},
        "search": {"discovered": 0, "inserted": 0, "errors": {}},
        "scoring": {"scored": 0, "errors": 0},
        "tailoring": {"tailored": 0, "errors": 0, "resumes": []},
        "packets": {"built": 0, "paths": []},
        "saved_search": {"created": False, "id": None, "label": ""},
        "elapsed_ms": 0,
    }

    if resume_file is None and not (linkedin_text or linkedin_url):
        summary["steps"].append({"name": "input_check", "status": "error",
                                 "detail": "supply a resume file or LinkedIn input"})
        summary["elapsed_ms"] = int((time.time() - started) * 1000)
        return {"ok": False, "data": summary, "error": "no input"}

    # 1. Infer profile -----------------------------------------------------
    # Merge logic: never clobber a field the user already set (e.g. picked
    # employment_types / remote_preference / salary in the Autopilot modal).
    # Inferred fields fill in the blanks only.
    try:
        from . import profile as profile_router
        from ..db import row_to_dict
        from ..models.schemas import UserProfileIn

        # Read existing profile FIRST so we know what the user already set
        existing_row = get_conn().execute(
            "SELECT * FROM user_profile WHERE id = 1"
        ).fetchone()
        existing = row_to_dict(existing_row) or {}

        inf = await profile_router.infer_profile(
            resume_file=resume_file,
            linkedin_text=linkedin_text,
            linkedin_url=linkedin_url,
            github_url=github_url,
            portfolio_url=portfolio_url,
        )
        if not inf.get("ok"):
            raise RuntimeError("infer returned ok=false")
        inferred_data = inf.get("data") or {}
        inferred_fields = inf.get("inferred_fields") or []

        # Build the final body: existing wins for non-empty fields, inferred fills the gaps
        merged: dict[str, Any] = {}
        for k, v in (inferred_data or {}).items():
            if v in (None, "", [], {}):
                continue
            existing_v = existing.get(k)
            if existing_v in (None, "", [], {}):
                merged[k] = v
            # If user already set this field, keep theirs — don't overwrite

        # Always carry over the user's existing values too (so the PUT is whole-row)
        for k, v in existing.items():
            if k in ("id", "created_at", "updated_at"):
                continue
            if k not in merged and v not in (None, "", [], {}):
                merged[k] = v

        body = UserProfileIn(**{k: v for k, v in merged.items()
                                if k in UserProfileIn.model_fields})
        profile_router.put_profile(body)
        summary["profile"]["inferred_fields"] = inferred_fields
        summary["profile"]["saved"] = True
        summary["profile"]["merged_field_count"] = len(merged)
        summary["steps"].append({"name": "profile_inferred", "status": "ok",
                                 "detail": f"{len(inferred_fields)} fields inferred, merged with user picks ({len(merged)} total)"})
    except Exception as exc:  # noqa: BLE001
        log.warning("autopilot profile step failed: %s", exc)
        summary["steps"].append({"name": "profile_inferred", "status": "error",
                                 "detail": f"{type(exc).__name__}: {exc}"})

    # 2. Ingest evidence ---------------------------------------------------
    # The resume file is consumed by profile inference (it read() the bytes),
    # so we re-read by re-opening if possible OR by re-uploading via the
    # bytes if the underlying SpooledTemporaryFile rewound. UploadFile.read()
    # consumes, but we ALSO want the bytes for evidence ingestion. We saved
    # nothing in infer; redo here via a fresh seek if available.
    try:
        from ..services import career_vault
        from ..services.evidence_extractor import extract_claims
        sources_added = 0
        claims_extracted = 0

        # Resume bytes — re-read from the upload's file pointer.
        if resume_file is not None:
            try:
                await resume_file.seek(0)
            except Exception:
                pass
            data = await resume_file.read()
            text = _bytes_to_text(data, resume_file.filename or "resume")
            if text.strip():
                sid = career_vault.add_source(
                    "resume",
                    title=resume_file.filename or "resume",
                    filename=resume_file.filename,
                    raw_text=text,
                )
                claims = extract_claims(sid, text, "resume")
                career_vault.add_claims(sid, claims)
                sources_added += 1
                claims_extracted += len(claims)

        if linkedin_text and linkedin_text.strip():
            sid = career_vault.add_source(
                "linkedin",
                title="LinkedIn paste",
                raw_text=linkedin_text.strip(),
            )
            claims = extract_claims(sid, linkedin_text.strip(), "linkedin")
            career_vault.add_claims(sid, claims)
            sources_added += 1
            claims_extracted += len(claims)

        summary["vault"]["sources_added"] = sources_added
        summary["vault"]["claims_extracted"] = claims_extracted
        summary["steps"].append({"name": "vault_populated", "status": "ok",
                                 "detail": f"{sources_added} sources, {claims_extracted} claims"})
    except Exception as exc:  # noqa: BLE001
        log.warning("autopilot vault step failed: %s", exc)
        summary["steps"].append({"name": "vault_populated", "status": "error",
                                 "detail": f"{type(exc).__name__}: {exc}"})

    # 3. Run search using inferred target_titles --------------------------
    targets: list[str] = []
    location_pref = ""
    try:
        from ..db import row_to_dict
        prof_row = get_conn().execute("SELECT target_titles, preferred_locations, location FROM user_profile WHERE id=1").fetchone()
        prof = row_to_dict(prof_row) or {}
        targets = prof.get("target_titles") or []
        if isinstance(targets, str):
            targets = [t.strip() for t in targets.split(",") if t.strip()]
        location_pref = (prof.get("preferred_locations") or [prof.get("location") or ""])[0] if (prof.get("preferred_locations") or prof.get("location")) else ""
    except Exception:
        pass

    primary_query = targets[0] if targets else "engineer"
    sites_list = [s.strip() for s in (sites or "").split(",") if s.strip()]

    try:
        from ..services.job_sources.pipeline import search_all, persist
        from ..services.job_sources.base import JobSearchQuery
        q = JobSearchQuery(
            query=primary_query,
            location=location_pref or None,
            is_remote=not bool(location_pref),
            results_per_site=int(search_results),
            hours_old=int(search_hours_old),
        )
        sr = search_all(q, sites=sites_list)
        pr = persist(sr.get("records", []))
        summary["search"]["discovered"] = len(sr.get("records", []))
        summary["search"]["inserted"] = int(pr.get("inserted", 0))
        summary["search"]["per_source"] = sr.get("per_source", {})
        summary["search"]["errors"] = sr.get("errors", {})
        summary["search"]["new_ids"] = pr.get("ids", [])
        summary["steps"].append({"name": "search_complete", "status": "ok",
                                 "detail": f"discovered={summary['search']['discovered']} new={summary['search']['inserted']}"})
    except Exception as exc:  # noqa: BLE001
        log.warning("autopilot search step failed: %s", exc)
        summary["steps"].append({"name": "search_complete", "status": "error",
                                 "detail": f"{type(exc).__name__}: {exc}"})

    # 4. Score every job (new + existing un-scored) ------------------------
    try:
        from ..matching.scorer import score_job
        scored = 0
        errors = 0
        rows = get_conn().execute(
            "SELECT j.id FROM job_posting j LEFT JOIN job_match m ON m.job_id=j.id "
            "WHERE j.status NOT IN ('archived') ORDER BY j.id DESC LIMIT 100"
        ).fetchall()
        for row in rows:
            try:
                score_job(int(row[0]))
                scored += 1
            except Exception:
                errors += 1
        summary["scoring"]["scored"] = scored
        summary["scoring"]["errors"] = errors
        summary["steps"].append({"name": "scoring_complete", "status": "ok",
                                 "detail": f"scored={scored} errors={errors}"})
    except Exception as exc:  # noqa: BLE001
        log.warning("autopilot scoring step failed: %s", exc)
        summary["steps"].append({"name": "scoring_complete", "status": "error",
                                 "detail": f"{type(exc).__name__}: {exc}"})

    # 5. Pick the top jobs by score for tailoring + packets ---------------
    top_jobs: list[dict] = []
    try:
        sql = (
            "SELECT j.id, j.title, j.company, m.overall_score "
            "FROM job_posting j JOIN job_match m ON m.job_id=j.id "
            "WHERE j.status NOT IN ('archived') "
            "AND m.overall_score >= ? "
            "ORDER BY m.overall_score DESC LIMIT ?"
        )
        rows = get_conn().execute(sql, (float(min_score) / 100.0 if min_score > 1 else float(min_score),
                                        max(tailor_top, packet_top))).fetchall()
        for r in rows:
            top_jobs.append({"id": int(r[0]), "title": r[1], "company": r[2],
                             "score": float(r[3])})
    except Exception as exc:  # noqa: BLE001
        log.debug("autopilot top-job query failed: %s", exc)

    # 6. Tailor resumes for top N -----------------------------------------
    try:
        from ..tailoring.resume_tailor import tailor_resume
        tailored = []
        errors = 0
        for j in top_jobs[: int(tailor_top)]:
            try:
                t = tailor_resume(j["id"])
                tailored.append({"job_id": j["id"], "resume_id": t.get("id"),
                                 "company": j["company"], "title": j["title"],
                                 "score": j["score"]})
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.debug("tailor failed for job %d: %s", j["id"], exc)
        summary["tailoring"]["tailored"] = len(tailored)
        summary["tailoring"]["errors"] = errors
        summary["tailoring"]["resumes"] = tailored
        summary["steps"].append({"name": "tailoring_complete", "status": "ok",
                                 "detail": f"tailored={len(tailored)} of {min(len(top_jobs), tailor_top)}"})
    except Exception as exc:  # noqa: BLE001
        log.warning("autopilot tailoring step failed: %s", exc)
        summary["steps"].append({"name": "tailoring_complete", "status": "error",
                                 "detail": f"{type(exc).__name__}: {exc}"})

    # 7. Build packets for top M ------------------------------------------
    try:
        from ..applications.packet_builder import build as build_packet
        from ..applications.pipeline import create_application
        paths = []
        for j in top_jobs[: int(packet_top)]:
            try:
                # Ensure an application row exists at status=prepared
                app_id = create_application(job_id=j["id"], status="prepared",
                                            mode="assisted",
                                            notes="auto-prepared by Autopilot")
                pkt = build_packet(j["id"])
                if isinstance(pkt, dict):
                    paths.append({"job_id": j["id"], "company": j["company"],
                                  "title": j["title"], "score": j["score"],
                                  "application_id": app_id,
                                  "packet_dir": pkt.get("packet_dir")})
            except Exception as exc:  # noqa: BLE001
                log.debug("packet build failed for job %d: %s", j["id"], exc)
        summary["packets"]["built"] = len(paths)
        summary["packets"]["paths"] = paths
        summary["steps"].append({"name": "packets_built", "status": "ok",
                                 "detail": f"packets={len(paths)} of {min(len(top_jobs), packet_top)}"})
    except Exception as exc:  # noqa: BLE001
        log.warning("autopilot packets step failed: %s", exc)
        summary["steps"].append({"name": "packets_built", "status": "error",
                                 "detail": f"{type(exc).__name__}: {exc}"})

    # 8. Register a recurring saved search for tomorrow + onward ----------
    if daily_recurrence:
        try:
            from ..integrations import scheduler as sched
            label = f"Autopilot: {primary_query} ({'remote' if not location_pref else location_pref})"
            query_blob = {
                "query": primary_query,
                "location": location_pref or None,
                "is_remote": not bool(location_pref),
                "sites": sites_list,
                "results_per_site": int(search_results),
                "hours_old": int(search_hours_old),
            }
            conn = get_conn()
            cur = conn.execute(
                "INSERT INTO saved_search (label, query_json, frequency_hours, enabled, created_at) "
                "VALUES (?, ?, ?, 1, ?)",
                (label, json.dumps(query_blob), 24, time.time()),
            )
            sid = int(cur.lastrowid)
            try:
                sched.register_saved_searches()
            except Exception as exc:  # noqa: BLE001
                log.debug("scheduler register hint failed: %s", exc)
            summary["saved_search"]["created"] = True
            summary["saved_search"]["id"] = sid
            summary["saved_search"]["label"] = label
            summary["steps"].append({"name": "saved_search_registered", "status": "ok",
                                     "detail": f"id={sid} runs every 24h"})
        except Exception as exc:  # noqa: BLE001
            log.warning("autopilot saved-search step failed: %s", exc)
            summary["steps"].append({"name": "saved_search_registered", "status": "error",
                                     "detail": f"{type(exc).__name__}: {exc}"})

    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    audit("autopilot_run", "system", None,
          discovered=summary["search"]["discovered"],
          tailored=summary["tailoring"]["tailored"],
          packets=summary["packets"]["built"])
    return {"ok": True, "data": summary}


@router.get("/autopilot/status")
def autopilot_status() -> dict:
    """Quick snapshot for the UI: is there a live autopilot saved search?
    Used by the Landing page to show a green "ON" pill if active."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id, label, frequency_hours, last_run_at FROM saved_search "
        "WHERE enabled = 1 AND label LIKE 'Autopilot:%' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {"ok": True, "data": {"active": False}}
    return {"ok": True, "data": {
        "active": True,
        "saved_search_id": int(row[0]),
        "label": row[1],
        "frequency_hours": int(row[2] or 24),
        "last_run_at": float(row[3]) if row[3] else None,
    }}


# ----- helpers -----

def _bytes_to_text(data: bytes, filename: str) -> str:
    """Best-effort: extract text from bytes via the document parser. Falls
    back to UTF-8 decode for plaintext."""
    import tempfile
    from pathlib import Path
    from ..services.document_parser import parse_file

    suffix = ""
    if "." in filename:
        suffix = "." + filename.rsplit(".", 1)[1].lower()
    with tempfile.NamedTemporaryFile(prefix="jhh_auto_", suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        out = parse_file(tmp_path)
        if isinstance(out, dict):
            return str(out.get("text") or "")
        return ""
    except Exception:
        try:
            return data.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass
