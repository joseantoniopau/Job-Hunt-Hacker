"""Career vault endpoints — list/update claims, contradiction scan, retrieval.

Also hosts the always-on "quick update" + LLM re-ingest surface that lets
the user push a new LinkedIn/GitHub/portfolio URL or pasted evidence into
the vault from anywhere in the app, and re-extract claims for any
existing source with the strict, source-span-verified LLM pipeline.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..db import audit, get_conn, row_to_dict, tx
from ..models.schemas import ClaimUpdate, OK
from ..services import (
    career_vault,
    contradiction_detector,
    demo_seed,
    evidence_extractor,
    llm_vault_reingest,
    url_ingestion,
)

log = logging.getLogger("jhh.evidence")

router = APIRouter(prefix="/api/vault", tags=["vault"])


class RetrieveRequest(BaseModel):
    text: str
    top: int = 15


class ReingestRequest(BaseModel):
    source_ids: Optional[list[int]] = None


class QuickUpdateRequest(BaseModel):
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    paste_text: Optional[str] = None
    paste_label: Optional[str] = None
    paste_source_type: Optional[str] = Field(
        default=None,
        description="Override paste type — defaults to 'text' for pasted blobs.",
    )


class DemoSeedRequest(BaseModel):
    confirm: bool = False


@router.get("/summary")
def summary() -> dict:
    return {"ok": True, "data": career_vault.summary()}


# ---------------------------------------------------------------------------
# ONBOARDING DEMO MODE
# ---------------------------------------------------------------------------

@router.post("/demo-seed")
def demo_seed_create(body: DemoSeedRequest) -> dict:
    """Seed the onboarding demo vault.

    Request: `{"confirm": true}` — the explicit flag guards against
    accidental seeding from API explorers (400 without it).

    Only allowed when the vault is effectively empty (zero evidence
    sources AND no user-entered profile name) — otherwise 409.

    Response: `{"ok": true, "data": {profile_fields_set: [str],
    source_ids: [int], claims_inserted: int, job_ids: [int],
    jobs_scored: int, score_errors: [str], application_ids: [int]}}`.
    Seeds a fictional profile (Alex Rivera / demo@example.invalid), two
    demo evidence sources, ~15-20 provenance-backed claims, six scored
    demo job postings, and two applications in different pipeline stages.
    All rows are tagged (source/source_type = 'demo') for exact cleanup.
    """
    if not body.confirm:
        raise HTTPException(400, 'pass {"confirm": true} to seed demo data')
    try:
        result = demo_seed.seed_demo()
    except demo_seed.DemoSeedConflict as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "data": result}


@router.delete("/demo-seed")
def demo_seed_delete() -> dict:
    """Wipe exactly the demo rows seeded by POST /api/vault/demo-seed.

    Deletes evidence sources with source_type='demo' (claims + embeddings
    follow), job postings with source='demo' (matches/applications cascade),
    and resets any profile field whose value still equals the demo value.
    Idempotent — zero counts when no demo data exists.

    Response: `{"ok": true, "data": {sources_deleted: int,
    claims_deleted: int, jobs_deleted: int, applications_deleted: int,
    profile_fields_reset: [str]}}`.
    """
    return {"ok": True, "data": demo_seed.delete_demo()}


@router.get("/demo-status")
def demo_status() -> dict:
    """Whether demo data is currently present.

    Response: `{"ok": true, "active": bool, "data": {active: bool,
    sources: int, claims: int, jobs: int, applications: int}}`.
    """
    status = demo_seed.demo_status()
    return {"ok": True, "active": status["active"], "data": status}


@router.get("/claims")
def list_claims(type: Optional[str] = None,
                allowed_only: bool = False,
                verified_only: bool = False,
                # UI-friendly aliases so `?verified=true&allowed_for_resume=true` also works
                verified: Optional[bool] = None,
                allowed_for_resume: Optional[bool] = None,
                source_id: Optional[int] = None) -> dict:
    if verified is not None:
        verified_only = bool(verified)
    if allowed_for_resume is not None:
        allowed_only = bool(allowed_for_resume)
    rows = career_vault.list_claims(
        source_id=source_id,
        claim_type=type,
        allowed_only=allowed_only,
        verified_only=verified_only,
    )
    return {"ok": True, "data": rows}


@router.patch("/claims/{claim_id}")
def patch_claim(claim_id: int, body: ClaimUpdate) -> dict:
    fields = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    try:
        updated = career_vault.update_claim(claim_id, fields)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True, "data": updated}


@router.delete("/claims/{claim_id}")
def delete_claim(claim_id: int) -> OK:
    n = career_vault.delete_claim(claim_id)
    if not n:
        raise HTTPException(404, "claim not found")
    return OK(detail=f"deleted claim {claim_id}")


@router.post("/contradictions/scan")
def scan_contradictions() -> dict:
    findings = contradiction_detector.find_contradictions()
    return {"ok": True, "data": {
        "count": len(findings),
        "findings": findings,
    }}


@router.post("/retrieve")
def retrieve(body: RetrieveRequest) -> dict:
    rows = career_vault.retrieve_for_job(body.text, top=body.top)
    return {"ok": True, "data": rows}


# ---------------------------------------------------------------------------
# LLM RE-INGEST + ALWAYS-ON UPDATE SURFACE
# ---------------------------------------------------------------------------

def _llm_available() -> bool:
    """Return True if a non-template LLM provider is wired up.

    We deliberately treat the TemplateProvider as "no LLM" — its output is
    formulaic and would mostly fail the strict source_span check anyway,
    so the deterministic extractor is a better fallback in that case.
    """
    try:
        from ..llm import get_llm
        from ..llm.template_provider import TemplateProvider
    except Exception:
        return False
    try:
        return not isinstance(get_llm(), TemplateProvider)
    except Exception:
        return False


def _ingest_url_source(url: str, source_type: str) -> dict:
    """Fetch a URL and add it as an evidence_source. Returns a small dict
    with `source_id` + status info. Does NOT extract claims (the caller
    decides whether to run LLM or deterministic extraction).
    """
    fetched = url_ingestion.fetch_url(url)
    if "error" in fetched:
        return {"ok": False, "error": fetched["error"], "url": url}
    text = (fetched.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "no readable text at url", "url": url}
    source_id = career_vault.add_source(
        source_type=source_type,
        title=fetched.get("title") or url,
        url=fetched.get("url") or url,
        raw_text=text,
        parsed_json={"content_type": fetched.get("content_type"),
                     "fetched_at": fetched.get("fetched_at")},
    )
    return {
        "ok": True,
        "source_id": int(source_id),
        "title": fetched.get("title") or url,
        "url": fetched.get("url") or url,
        "chars": len(text),
    }


def _extract_claims_for_source(source_id: int, raw_text: str,
                               source_type: str) -> dict:
    """LLM-first claim extraction with deterministic fallback. Used by
    every "ingest then extract" endpoint so behavior is consistent."""
    if _llm_available():
        try:
            return llm_vault_reingest.reingest_source_with_llm(int(source_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM reingest failed for %s, falling back: %s",
                        source_id, exc)
    # Deterministic fallback — also OK as a starting point if LLM is off.
    claims = evidence_extractor.extract_claims(int(source_id), raw_text or "",
                                               source_type)
    inserted = career_vault.add_claims(int(source_id), claims)
    return {
        "ok": True,
        "source_id": int(source_id),
        "claims_old_count": 0,
        "claims_inserted": len(inserted),
        "claims_dropped_unverified": 0,
        "llm_run_id": None,
        "elapsed_ms": 0,
        "error": None,
        "deterministic": True,
    }


@router.post("/reingest")
def reingest_many(body: ReingestRequest) -> dict:
    """Re-extract claims with the LLM for some or all sources.

    Body: `{"source_ids": [1, 2, 3]}` to target specific sources, or
    `{}` / `{"source_ids": null}` to re-ingest every source.
    """
    if not _llm_available():
        raise HTTPException(
            503,
            "LLM provider not configured — set ANTHROPIC_API_KEY, "
            "OPENAI_API_KEY, or OLLAMA_BASE_URL.",
        )
    if body.source_ids:
        results = []
        total_old = total_new = total_dropped = errors = 0
        for sid in body.source_ids:
            try:
                r = llm_vault_reingest.reingest_source_with_llm(int(sid))
            except Exception as exc:  # noqa: BLE001
                r = {
                    "ok": False, "source_id": int(sid),
                    "claims_old_count": 0, "claims_inserted": 0,
                    "claims_dropped_unverified": 0, "llm_run_id": None,
                    "elapsed_ms": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(r)
            total_old += int(r.get("claims_old_count") or 0)
            total_new += int(r.get("claims_inserted") or 0)
            total_dropped += int(r.get("claims_dropped_unverified") or 0)
            if not r.get("ok"):
                errors += 1
        return {
            "ok": True,
            "data": {
                "sources_processed": len(body.source_ids),
                "results": results,
                "totals": {
                    "claims_old": total_old,
                    "claims_inserted": total_new,
                    "claims_dropped_unverified": total_dropped,
                    "errors": errors,
                },
            },
        }
    summary = llm_vault_reingest.reingest_all_sources_with_llm()
    return {"ok": True, "data": summary}


@router.post("/sources/{source_id}/reingest")
def reingest_one(source_id: int) -> dict:
    """Re-extract claims with the LLM for a single source."""
    if not _llm_available():
        raise HTTPException(
            503,
            "LLM provider not configured — set ANTHROPIC_API_KEY, "
            "OPENAI_API_KEY, or OLLAMA_BASE_URL.",
        )
    result = llm_vault_reingest.reingest_source_with_llm(int(source_id))
    if not result.get("ok"):
        # 404 if the source itself didn't exist; otherwise surface the error
        # detail so the UI can show "LLM call failed: ..." instead of a
        # generic 500.
        if (result.get("error") or "").startswith("source not found"):
            raise HTTPException(404, result["error"])
        raise HTTPException(502, result.get("error") or "reingest failed")
    return {"ok": True, "data": result}


@router.get("/sources-with-claim-counts")
def sources_with_claim_counts() -> dict:
    """List every evidence_source plus its current claim_count, so the
    always-on UI can render a single table of "re-ingest this source"
    rows with the old count next to the button."""
    sources = career_vault.list_sources()
    return {"ok": True, "data": {"sources": sources, "count": len(sources)}}


@router.post("/quick-update")
def quick_update(body: QuickUpdateRequest) -> dict:
    """Always-on update path. Caller may supply any combination of:

      * `linkedin_url`, `github_url`, `portfolio_url` — saved to the user
        profile; the URL is then fetched and ingested as a fresh
        evidence_source (replacing the equivalent type would create stale
        sources, so we leave the previous one in place — the caller can
        delete it from the vault if they want).
      * `paste_text` + optional `paste_label` — added as a `'text'`
        evidence_source.

    For every source we touch we run the LLM re-ingest (with deterministic
    fallback). Returns the updated profile + the list of (source_id,
    llm_run_id, kind) tuples.
    """
    touched: list[dict] = []
    errors: list[str] = []
    profile_fields_updated: list[str] = []

    # 1) URL fields — update profile + re-fetch + re-ingest
    url_map = {
        "linkedin": (body.linkedin_url, "linkedin"),
        "github":   (body.github_url, "github"),
        "portfolio": (body.portfolio_url, "portfolio"),
    }
    profile_updates: dict[str, str] = {}
    for kind, (url, source_type) in url_map.items():
        if not url or not url.strip():
            continue
        url_clean = url.strip()
        profile_updates[f"{kind if kind != 'portfolio' else 'portfolio'}_url"] = url_clean
        ingest_result = _ingest_url_source(url_clean, source_type)
        if not ingest_result.get("ok"):
            errors.append(f"{kind}: {ingest_result.get('error')}")
            touched.append({
                "kind": kind,
                "source_id": None,
                "ok": False,
                "error": ingest_result.get("error"),
            })
            continue
        sid = ingest_result["source_id"]
        # Pull raw_text so the extractor fallback knows what to extract.
        row = get_conn().execute(
            "SELECT raw_text FROM evidence_source WHERE id = ?", (sid,)
        ).fetchone()
        raw_text = (row["raw_text"] if row else "") or ""
        extract = _extract_claims_for_source(sid, raw_text, source_type)
        touched.append({
            "kind": kind,
            "source_id": sid,
            "title": ingest_result.get("title"),
            "ok": True,
            "chars": ingest_result.get("chars"),
            "llm_run_id": extract.get("llm_run_id"),
            "claims_inserted": extract.get("claims_inserted"),
            "claims_dropped_unverified": extract.get("claims_dropped_unverified"),
            "deterministic": extract.get("deterministic", False),
        })

    # Apply URL changes to the profile (singleton row id=1).
    if profile_updates:
        cols = []
        vals: list = []
        for k, v in profile_updates.items():
            cols.append(f"{k} = ?")
            vals.append(v)
        cols.append("updated_at = ?")
        vals.append(time.time())
        sql = f"UPDATE user_profile SET {', '.join(cols)} WHERE id = 1"
        # Autocommit connection — make the multi-column profile write atomic.
        with tx() as c:
            c.execute(sql, vals)
        profile_fields_updated = sorted(profile_updates.keys())
        audit("profile_update", "user_profile", 1,
              fields=profile_fields_updated, source="vault_quick_update")

    # 2) Paste text — ingest as a text source + extract
    paste = (body.paste_text or "").strip()
    if paste:
        source_type = (body.paste_source_type or "text").strip() or "text"
        title = (body.paste_label or "").strip() or f"Quick paste ({len(paste)} chars)"
        source_id = career_vault.add_source(
            source_type=source_type,
            title=title,
            raw_text=paste,
        )
        extract = _extract_claims_for_source(source_id, paste, source_type)
        touched.append({
            "kind": "paste",
            "source_id": int(source_id),
            "title": title,
            "ok": True,
            "chars": len(paste),
            "llm_run_id": extract.get("llm_run_id"),
            "claims_inserted": extract.get("claims_inserted"),
            "claims_dropped_unverified": extract.get("claims_dropped_unverified"),
            "deterministic": extract.get("deterministic", False),
        })

    if not touched:
        raise HTTPException(
            400,
            "supply at least one of linkedin_url, github_url, "
            "portfolio_url, paste_text",
        )

    # Final read-back of the profile so the UI doesn't need a second hop.
    prof = row_to_dict(
        get_conn().execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    )

    return {
        "ok": True,
        "data": {
            "profile": prof,
            "profile_fields_updated": profile_fields_updated,
            "touched": touched,
            "errors": errors,
            "llm_used": _llm_available(),
        },
    }
