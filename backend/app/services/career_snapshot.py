"""Career snapshot — LLM-generated narrative of who the user is, what they do,
where they are in their career, and where to go next.

This is the first thing the user should see after onboarding. It reads the
profile + every verified vault claim + the user's URL ingestions and
produces a plain-English writeup PLUS structured recommendations the rest
of the app uses to drive search.

Honesty rule: the LLM may only reference facts that appear in the EVIDENCE
PACK. Job recommendations must be derived from observed skills/titles/
industries, not invented job-market trends.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..config import settings
from ..db import audit, get_conn
from ..llm import get_llm
from ..llm.json_repair import extract_json as safe_json_parse
from ..llm.observability import observed_complete

log = logging.getLogger("jhh.career_snapshot")


SYSTEM_PROMPT = """You are a career-narrative writer for a job-hunt skill.
Given a candidate's profile and vault of verified claims, produce a SHORT
plain-English snapshot describing:
  - who they are (basic info)
  - what they do for a living
  - where they are in their career
  - what their next 2–3 likely career moves are
  - which job titles + keywords to search for

ABSOLUTE RULES:
  - Use ONLY facts from the EVIDENCE PACK. Never invent a job title,
    employer, skill, certification, or year of experience that is not
    explicitly listed.
  - If you don't have enough evidence for a section, say so honestly
    (e.g. "career stage unclear: evidence vault has only resume claims;
    add LinkedIn or GitHub to refine").
  - Distinguish EMPLOYERS (eBay, Google) from TITLES (Software Engineer).
  - Job recommendations must be specific role TITLES (3-7 of them),
    each one derivable from a real claim in the evidence pack.

OUTPUT JSON ONLY in this shape:
{
  "basic_info": {"name": str|null, "location": str|null, "current_role": str|null},
  "what_they_do": "2-3 sentence plain-English description",
  "career_stage": "early|mid|senior|staff|principal|exec|unclear",
  "career_stage_reasoning": "1-2 sentence justification with claim references",
  "strengths": ["..."],
  "next_steps": [
    {"move": "Title or transition (e.g. 'Senior IR Engineer at a fintech')",
     "rationale": "Why this fits the candidate, citing evidence"}
  ],
  "job_recommendations": [
    {"title": "Job title to search for",
     "keywords": ["key", "tech", "or", "skill"],
     "rationale": "Which evidence points support this"}
  ],
  "narrative": "1 paragraph (4-6 sentences) describing the candidate in their own context"
}
"""


def _build_evidence_pack() -> str:
    """Compile a tight EVIDENCE PACK string the LLM can reason over.

    Pulls: profile basics, target_titles, target_keywords, all evidence
    sources (just type + size markers), every verified vault claim up to
    a char cap.
    """
    conn = get_conn()
    parts: list[str] = []
    # Profile
    p_row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    if p_row:
        p = dict(p_row)
        parts.append("=== PROFILE ===")
        for k in ("name", "email", "location", "linkedin_url", "github_url", "portfolio_url"):
            if p.get(k):
                parts.append(f"{k}: {p[k]}")
        for k in ("target_titles", "target_keywords", "industries",
                  "seniority_targets", "employment_types", "preferred_locations"):
            val = p.get(k)
            if val:
                try:
                    val = json.loads(val) if isinstance(val, str) else val
                except Exception:
                    pass
                if val:
                    parts.append(f"{k}: {val}")
        if p.get("minimum_salary"):
            parts.append(f"minimum_salary: {p['minimum_salary']} {p.get('currency','USD')}")

    # Evidence sources (types only — not the raw text; the claims carry meaning)
    es_rows = conn.execute(
        "SELECT id, source_type, COALESCE(filename, url, '') AS label, "
        "       length(raw_text) AS chars FROM evidence_source ORDER BY id"
    ).fetchall()
    if es_rows:
        parts.append("\n=== EVIDENCE SOURCES INGESTED ===")
        for r in es_rows:
            parts.append(f"  #{r['id']} {r['source_type']} ({r['chars']} chars) — {r['label']}")

    # Vault claims — these ARE the substance
    claim_rows = conn.execute(
        "SELECT id, source_id, claim_type, claim_text, "
        "       COALESCE(skill, '') AS skill, "
        "       COALESCE(tool,  '') AS tool, "
        "       COALESCE(employer, '') AS employer "
        "FROM career_claim ORDER BY id"
    ).fetchall()
    if claim_rows:
        parts.append("\n=== VERIFIED VAULT CLAIMS ===")
        for r in claim_rows:
            extras = []
            if r["claim_type"]:
                extras.append(f"type={r['claim_type']}")
            if r["skill"]:
                extras.append(f"skill={r['skill']}")
            if r["tool"]:
                extras.append(f"tool={r['tool']}")
            if r["employer"]:
                extras.append(f"employer={r['employer']}")
            extras_s = " | ".join(extras) if extras else ""
            parts.append(f"  [claim #{r['id']} src#{r['source_id']}] {r['claim_text']}"
                         + (f"  [{extras_s}]" if extras_s else ""))

    pack = "\n".join(parts)
    # Cap to keep prompt tokens reasonable
    if len(pack) > 12000:
        pack = pack[:12000] + "\n…[truncated]"
    return pack


def generate_snapshot() -> dict:
    """Generate a career snapshot using the active LLM provider.

    Falls back to a deterministic stub when no LLM is configured so the UI
    still has SOMETHING to render — the user can refresh once they wire up
    an LLM provider.
    """
    started = time.time()
    pack = _build_evidence_pack()
    if not pack or "VERIFIED VAULT CLAIMS" not in pack:
        return {
            "ok": False,
            "error": "no_evidence",
            "detail": "Upload a resume or paste evidence first; the vault is empty.",
        }

    provider = get_llm()
    if getattr(provider, "name", "template") == "template":
        # Honest deterministic fallback so the UI isn't empty
        snapshot = _deterministic_snapshot(pack)
        snapshot["llm_run_id"] = None
        snapshot["generated_by"] = "template"
        _persist_snapshot(snapshot)
        return {"ok": True, "data": snapshot, "elapsed_ms": int((time.time() - started) * 1000)}

    user_prompt = (
        "Generate the career snapshot.\n\n"
        f"EVIDENCE PACK:\n{pack}\n\n"
        "Remember: JSON output only; never invent facts; cite evidence by [claim #ID] where possible."
    )
    try:
        output, run_id = observed_complete(
            provider,
            stage="career_snapshot",
            system=SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=2000,
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot LLM call failed: %s", exc)
        snapshot = _deterministic_snapshot(pack)
        snapshot["llm_run_id"] = None
        snapshot["generated_by"] = "template_fallback"
        snapshot["error"] = f"{type(exc).__name__}: {exc}"
        _persist_snapshot(snapshot)
        return {"ok": True, "data": snapshot, "elapsed_ms": int((time.time() - started) * 1000)}

    parsed = safe_json_parse(output) if output else None
    if not isinstance(parsed, dict):
        log.warning("snapshot output not parseable: %s", (output or "")[:200])
        snapshot = _deterministic_snapshot(pack)
        snapshot["llm_run_id"] = run_id
        snapshot["generated_by"] = "template_fallback"
        snapshot["error"] = "LLM output did not parse as JSON"
        _persist_snapshot(snapshot)
        return {"ok": True, "data": snapshot, "elapsed_ms": int((time.time() - started) * 1000)}

    snapshot = {
        "basic_info": parsed.get("basic_info") or {},
        "what_they_do": parsed.get("what_they_do") or "",
        "career_stage": parsed.get("career_stage") or "unclear",
        "career_stage_reasoning": parsed.get("career_stage_reasoning") or "",
        "strengths": parsed.get("strengths") or [],
        "next_steps": parsed.get("next_steps") or [],
        "job_recommendations": parsed.get("job_recommendations") or [],
        "narrative": parsed.get("narrative") or "",
        "llm_run_id": run_id,
        "generated_by": "llm",
    }
    _persist_snapshot(snapshot)
    return {"ok": True, "data": snapshot, "elapsed_ms": int((time.time() - started) * 1000)}


def _persist_snapshot(snap: dict) -> int:
    """Insert the snapshot, mark prior rows as not-latest."""
    conn = get_conn()
    conn.execute("UPDATE career_snapshot SET is_latest = 0 WHERE is_latest = 1")
    cur = conn.execute(
        """INSERT INTO career_snapshot
           (created_at, basic_info_json, what_they_do, career_stage,
            career_stage_reasoning, strengths_json, next_steps_json,
            job_recommendations_json, narrative, llm_run_id, is_latest)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (time.time(),
         json.dumps(snap.get("basic_info") or {}),
         snap.get("what_they_do") or "",
         snap.get("career_stage") or "unclear",
         snap.get("career_stage_reasoning") or "",
         json.dumps(snap.get("strengths") or []),
         json.dumps(snap.get("next_steps") or []),
         json.dumps(snap.get("job_recommendations") or []),
         snap.get("narrative") or "",
         snap.get("llm_run_id")),
    )
    snap_id = int(cur.lastrowid)
    snap["id"] = snap_id
    audit("career_snapshot_generated", "career_snapshot", snap_id,
          generated_by=snap.get("generated_by"))
    return snap_id


def _deterministic_snapshot(pack: str) -> dict:
    """Cheap template snapshot when no LLM is reachable. Pulls just enough
    from the pack so the user sees something coherent."""
    conn = get_conn()
    p_row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    p = dict(p_row) if p_row else {}
    try:
        titles = json.loads(p.get("target_titles") or "[]")
    except Exception:
        titles = []
    try:
        keywords = json.loads(p.get("target_keywords") or "[]")
    except Exception:
        keywords = []
    n_claims = conn.execute("SELECT COUNT(*) AS n FROM career_claim").fetchone()["n"]
    return {
        "basic_info": {
            "name": p.get("name") or None,
            "location": p.get("location") or None,
            "current_role": titles[0] if titles else None,
        },
        "what_they_do": (
            f"Based on {n_claims} verified vault claim(s), this candidate works as "
            f"{titles[0] if titles else 'a professional'} "
            f"with skills in {', '.join(keywords[:5]) if keywords else 'their stated domain'}. "
            "Connect an LLM provider for a richer narrative."
        ),
        "career_stage": "unclear",
        "career_stage_reasoning": "Deterministic fallback — connect an LLM for proper stage detection.",
        "strengths": keywords[:6],
        "next_steps": [
            {"move": titles[0] if titles else "Next role at current level",
             "rationale": "Based on stated target_titles"}
            for _ in range(1)
        ],
        "job_recommendations": [
            {"title": t, "keywords": keywords[:3],
             "rationale": "From profile target_titles"} for t in titles[:5]
        ],
        "narrative": (
            f"Profile imported; vault contains {n_claims} verified claim(s). "
            "Connect an LLM and click GENERATE SNAPSHOT for a personalized narrative."
        ),
    }


def get_latest_snapshot() -> dict | None:
    row = get_conn().execute(
        """SELECT id, created_at, basic_info_json, what_they_do, career_stage,
                  career_stage_reasoning, strengths_json, next_steps_json,
                  job_recommendations_json, narrative, llm_run_id
           FROM career_snapshot WHERE is_latest = 1
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    try:
        basic = json.loads(row["basic_info_json"] or "{}")
        strengths = json.loads(row["strengths_json"] or "[]")
        next_steps = json.loads(row["next_steps_json"] or "[]")
        recs = json.loads(row["job_recommendations_json"] or "[]")
    except Exception:
        basic, strengths, next_steps, recs = {}, [], [], []
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "basic_info": basic,
        "what_they_do": row["what_they_do"],
        "career_stage": row["career_stage"],
        "career_stage_reasoning": row["career_stage_reasoning"],
        "strengths": strengths,
        "next_steps": next_steps,
        "job_recommendations": recs,
        "narrative": row["narrative"],
        "llm_run_id": row["llm_run_id"],
    }
