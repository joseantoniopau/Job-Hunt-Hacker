"""LLM-powered second-pass scoring (semantic rerank) of job matches.

The deterministic scorer in `backend.app.matching.scorer` does the heavy
lifting: keyword overlap, salary fit, location fit, evidence-strength
scoring. That gets us a reliable, explainable shortlist — but it can't
weigh nuance (e.g. "Senior PM at a fintech series-A is closer to your
healthtech 0→1 work than the keyword overlap suggests").

This module sits AFTER the deterministic scorer and asks a local LLM to
re-rank the top-N. The LLM is constrained to score ONLY against the
user's verified Career Evidence Vault — never assume skills or experience
that aren't documented. Output lands in `llm_job_score` as a parallel,
strictly-additive signal. The deterministic score remains authoritative
in the UI until the user explicitly toggles to the semantic ranking.

Public entrypoints:
    rerank_top_n(top_n: int = 30) -> dict
        Batch-rerank the top-N deterministic-scored jobs that don't yet
        have an llm_job_score row. Skips jobs already scored unless the
        caller force-rescores via rerank_one.
    rerank_one(job_id: int) -> dict
        Ad-hoc single-job rescore. Always overwrites the existing row.

Both return a summary dict the router surfaces straight to the UI.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from ..config import settings
from ..db import get_conn, audit, row_to_dict
from ..llm import get_llm
from ..llm.json_repair import extract_json
from ..llm.observability import observed_complete

log = logging.getLogger("jhh.matching.llm_rerank")


# Hard ceiling on the evidence pack — keeps the prompt well under any
# local model's context window even with a fat job description tacked on.
_MAX_EVIDENCE_CHARS = 4000
# Trim the job description: most postings have a long benefits/EEO tail
# that adds no signal but burns context.
_MAX_JOB_DESC_CHARS = 3000
# Top-N claims to include — ordered by user_verified DESC, confidence DESC.
# 20 is enough to surface the substantive evidence without flooding context.
_MAX_CLAIMS = 20
# Hard upper bound on top_n per batch call. Keeps a single rerank batch
# bounded to ~5 minutes worst case at 70B/q5 cold latency.
_MAX_BATCH = 100


# ---- prompt construction ----

_SYSTEM_PROMPT = (
    "You score how well a candidate fits a job using ONLY the EVIDENCE "
    "PACK. NEVER assume skills, certs, or experience not listed. A high "
    "semantic score means the evidence DEMONSTRABLY meets job "
    "requirements. Be honest about gaps. "
    "Score ONLY using facts in EVIDENCE PACK. Never assume skills/certs/"
    "experience not listed. Be honest about gaps.\n\n"
    "OUTPUT RULES (must follow exactly):\n"
    "- Reply with a SINGLE valid JSON object and nothing else.\n"
    "- No markdown fences, no prose before or after.\n"
    "- Every string value MUST be wrapped in double quotes, including "
    "fit_summary.\n"
    "- semantic_score is a number 0.0-1.0 (not a percentage)."
)


# Common LLM mistakes we recover from before giving up.
# These run AFTER json_repair.extract_json fails to parse the raw object.
_JSON_KEY_PATTERN = re.compile(
    r'"(fit_summary|recommended_action)"\s*:\s*([^",\[\}\n][^,\n}]*)',
)


def _quote_unquoted_string_values(s: str) -> str:
    """Wrap unquoted string values for fit_summary / recommended_action.

    Some local models (esp. 70B with tight max_tokens) emit:
        "fit_summary": The candidate's skills...
    instead of:
        "fit_summary": "The candidate's skills..."
    This pattern catches that specific failure mode without trying to be a
    full JSON repair tool (which would be brittle).
    """
    def _repl(m: re.Match) -> str:
        key = m.group(1)
        raw = m.group(2).strip()
        # Strip trailing punctuation that belongs to the JSON structure.
        if raw.endswith(","):
            raw = raw[:-1].strip()
        # Escape embedded double quotes and backslashes for valid JSON.
        raw = raw.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{key}": "{raw}"'
    return _JSON_KEY_PATTERN.sub(_repl, s)


def _robust_json_parse(text: str) -> dict | None:
    """Try `json_repair.extract_json` first; fall back to the unquoted-string
    fix-up for the most common 70B/quantized-model output mistake."""
    parsed = extract_json(text or "")
    if isinstance(parsed, dict):
        return parsed
    # Find the first {...} block and run our repairs over just that slice.
    s = (text or "").strip()
    # Drop any leading markdown fence.
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL | re.IGNORECASE)
    if fence_match:
        s = fence_match.group(1).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = s[start:end + 1]
    repaired = _quote_unquoted_string_values(candidate)
    try:
        out = json.loads(repaired)
        return out if isinstance(out, dict) else None
    except Exception:
        return None


def _as_list(v: Any) -> list[str]:
    """Coerce a profile field (TEXT JSON or comma-list or list) to list[str]."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        pass
    return [t.strip() for t in s.split(",") if t.strip()]


def _load_profile() -> dict:
    """Read the singleton user_profile row as a dict."""
    row = get_conn().execute(
        "SELECT * FROM user_profile WHERE id = 1"
    ).fetchone()
    return row_to_dict(row) or {}


def _load_top_claims(limit: int = _MAX_CLAIMS) -> list[dict]:
    """Top claims by user_verified DESC, confidence DESC, then recency.

    `career_claim` is the table the deterministic scorer pulls from too —
    we use it (not the legacy `vault_claim` name) to stay consistent.
    Only `allowed_for_resume=1` rows are included; the user opted those
    in as honest evidence.
    """
    rows = get_conn().execute(
        """SELECT id, claim_type, claim_text, normalized_claim, employer,
                  project, skill, tool, date_start, date_end,
                  evidence_strength, confidence, user_verified
             FROM career_claim
             WHERE allowed_for_resume = 1
             ORDER BY user_verified DESC, confidence DESC, id DESC
             LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [row_to_dict(r) for r in rows if r]


def _build_evidence_pack() -> str:
    """Compose the EVIDENCE PACK string fed into every job prompt.

    Structure (under 4000 chars by construction):
        TARGET TITLES: ...
        TARGET KEYWORDS: ...
        INDUSTRIES: ...
        SENIORITY TARGETS: ...
        CLAIMS (top N):
          - <type> [<employer>] <claim_text> (<evidence_strength>)
    """
    prof = _load_profile()
    target_titles = _as_list(prof.get("target_titles"))
    target_keywords = _as_list(prof.get("target_keywords"))
    industries = _as_list(prof.get("industries"))
    seniority = _as_list(prof.get("seniority_targets"))

    parts: list[str] = []
    if target_titles:
        parts.append("TARGET TITLES: " + ", ".join(target_titles[:8]))
    if target_keywords:
        parts.append("TARGET KEYWORDS: " + ", ".join(target_keywords[:20]))
    if industries:
        parts.append("INDUSTRIES: " + ", ".join(industries[:8]))
    if seniority:
        parts.append("SENIORITY TARGETS: " + ", ".join(seniority[:6]))

    claims = _load_top_claims(_MAX_CLAIMS)
    if claims:
        parts.append("CLAIMS:")
        for c in claims:
            bits: list[str] = []
            ct = (c.get("claim_type") or "").strip()
            if ct:
                bits.append(f"[{ct}]")
            employer = (c.get("employer") or "").strip()
            if employer:
                bits.append(f"@{employer}")
            text = (c.get("claim_text") or c.get("normalized_claim") or "").strip()
            if text:
                bits.append(text)
            es = (c.get("evidence_strength") or "").strip()
            if es:
                bits.append(f"({es})")
            line = " ".join(bits).strip()
            if line:
                parts.append("  - " + line)

    pack = "\n".join(parts).strip()
    if len(pack) > _MAX_EVIDENCE_CHARS:
        pack = pack[: _MAX_EVIDENCE_CHARS - 16] + "\n…[truncated]"
    return pack or "(no evidence available — user profile and vault are empty)"


def _build_user_prompt(job: dict, evidence_pack: str) -> str:
    """Compose the per-job USER prompt. job is a sqlite Row converted to dict."""
    desc = (job.get("description") or "").strip()
    if len(desc) > _MAX_JOB_DESC_CHARS:
        desc = desc[:_MAX_JOB_DESC_CHARS] + "…[truncated]"
    title = (job.get("title") or "(no title)").strip()
    company = (job.get("company") or "(unknown)").strip()
    location = (job.get("location") or "—").strip()
    return (
        f"JOB TITLE: {title}\n"
        f"COMPANY: {company}\n"
        f"LOCATION: {location}\n"
        f"DESCRIPTION: {desc}\n\n"
        f"USER EVIDENCE PACK:\n{evidence_pack}\n\n"
        "Return JSON: "
        '{"semantic_score":0.0-1.0,'
        '"fit_summary":<1-2 sentences>,'
        '"strengths":[...],'
        '"gaps":[...],'
        '"red_flags":[...],'
        '"recommended_action":"apply"|"tailor_heavily"|"skip"|"explore"}'
    )


# ---- result persistence ----

_VALID_ACTIONS = {"apply", "tailor_heavily", "skip", "explore"}


def _coerce_score(v: Any) -> float | None:
    """Coerce model output to 0.0-1.0 float, or None if unusable."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Models occasionally emit 0-100 even when asked for 0-1. Normalize.
    if f > 1.0 and f <= 100.0:
        f = f / 100.0
    if f < 0.0:
        f = 0.0
    if f > 1.0:
        f = 1.0
    return f


def _coerce_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _coerce_action(v: Any) -> str:
    s = (str(v or "")).strip().lower().replace("-", "_").replace(" ", "_")
    if s in _VALID_ACTIONS:
        return s
    if s.startswith("apply"):
        return "apply"
    if s.startswith("tailor"):
        return "tailor_heavily"
    if s.startswith("skip") or s.startswith("no") or s.startswith("reject"):
        return "skip"
    if s.startswith("explore") or s.startswith("research"):
        return "explore"
    return "explore"


def _persist(job_id: int, parsed: dict, llm_run_id: int) -> dict:
    """Insert-or-replace into llm_job_score. Returns the persisted shape."""
    semantic_score = _coerce_score(parsed.get("semantic_score"))
    fit_summary = (str(parsed.get("fit_summary") or "")).strip()[:1000]
    strengths = _coerce_list(parsed.get("strengths"))
    gaps = _coerce_list(parsed.get("gaps"))
    red_flags = _coerce_list(parsed.get("red_flags"))
    recommended_action = _coerce_action(parsed.get("recommended_action"))
    now = time.time()
    get_conn().execute(
        """INSERT OR REPLACE INTO llm_job_score
           (job_id, semantic_score, fit_summary, strengths_json,
            gaps_json, red_flags_json, recommended_action, llm_run_id,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(job_id),
            semantic_score,
            fit_summary,
            json.dumps(strengths),
            json.dumps(gaps),
            json.dumps(red_flags),
            recommended_action,
            int(llm_run_id) if llm_run_id and llm_run_id > 0 else None,
            now,
        ),
    )
    return {
        "job_id": int(job_id),
        "semantic_score": semantic_score,
        "fit_summary": fit_summary,
        "strengths": strengths,
        "gaps": gaps,
        "red_flags": red_flags,
        "recommended_action": recommended_action,
        "llm_run_id": int(llm_run_id) if llm_run_id and llm_run_id > 0 else None,
        "created_at": now,
    }


# ---- candidate selection ----

def _candidate_jobs(top_n: int, force: bool = False) -> list[dict]:
    """Pull the top-N deterministic-scored jobs.

    When force=False, skip jobs that already have an llm_job_score row.
    When force=True, return all top-N regardless.
    """
    top_n = max(1, min(int(top_n), _MAX_BATCH))
    if force:
        sql = (
            "SELECT j.id, j.title, j.company, j.location, j.description, "
            "       m.overall_score "
            "FROM job_posting j "
            "JOIN job_match m ON m.job_id = j.id "
            "WHERE j.status NOT IN ('archived') "
            "ORDER BY m.overall_score DESC LIMIT ?"
        )
        params = (top_n,)
    else:
        sql = (
            "SELECT j.id, j.title, j.company, j.location, j.description, "
            "       m.overall_score "
            "FROM job_posting j "
            "JOIN job_match m ON m.job_id = j.id "
            "LEFT JOIN llm_job_score s ON s.job_id = j.id "
            "WHERE j.status NOT IN ('archived') AND s.job_id IS NULL "
            "ORDER BY m.overall_score DESC LIMIT ?"
        )
        params = (top_n,)
    rows = get_conn().execute(sql, params).fetchall()
    return [row_to_dict(r) for r in rows if r]


def _fetch_one(job_id: int) -> dict | None:
    row = get_conn().execute(
        "SELECT id, title, company, location, description FROM job_posting WHERE id = ?",
        (int(job_id),),
    ).fetchone()
    return row_to_dict(row) if row else None


# ---- public entrypoints ----

def _rerank_single(provider, job: dict, evidence_pack: str) -> tuple[dict | None, int, str | None]:
    """Score one job. Returns (persisted_dict | None, llm_run_id, error | None)."""
    user_prompt = _build_user_prompt(job, evidence_pack)
    try:
        output, run_id = observed_complete(
            provider,
            stage="llm_rerank",
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            max_tokens=600,
            temperature=0.1,
            target_type="job_posting",
            target_id=int(job["id"]),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_rerank call failed for job %s: %s", job.get("id"), exc)
        return None, -1, f"{type(exc).__name__}: {exc}"

    parsed = _robust_json_parse(output or "")
    if not isinstance(parsed, dict):
        log.warning("llm_rerank parse failed for job %s; output=%r", job.get("id"), (output or "")[:200])
        return None, run_id, "could not parse JSON from LLM output"

    try:
        persisted = _persist(int(job["id"]), parsed, run_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_rerank persist failed for job %s: %s", job.get("id"), exc)
        return None, run_id, f"persist failed: {type(exc).__name__}: {exc}"
    return persisted, run_id, None


def rerank_top_n(top_n: int = 30, force: bool = False) -> dict:
    """Batch second-pass score the top-N deterministic-scored jobs."""
    started = time.time()
    summary: dict[str, Any] = {
        "requested": int(top_n),
        "reranked": 0,
        "errors": 0,
        "skipped_no_provider": False,
        "elapsed_ms": 0,
    }

    # Honor the explicit "no LLM" opt-out. The template provider returns
    # canned strings — they'd parse as garbage and the rerank table would
    # fill with noise. Skip cleanly so the autopilot step is a no-op.
    if (settings.llm_provider or "").lower() == "template":
        summary["skipped_no_provider"] = True
        summary["elapsed_ms"] = int((time.time() - started) * 1000)
        return summary

    try:
        provider = get_llm()
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_rerank could not init provider: %s", exc)
        summary["skipped_no_provider"] = True
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["elapsed_ms"] = int((time.time() - started) * 1000)
        return summary

    if getattr(provider, "name", "") == "template":
        summary["skipped_no_provider"] = True
        summary["elapsed_ms"] = int((time.time() - started) * 1000)
        return summary

    candidates = _candidate_jobs(top_n, force=force)
    summary["candidates"] = len(candidates)
    if not candidates:
        summary["elapsed_ms"] = int((time.time() - started) * 1000)
        return summary

    evidence_pack = _build_evidence_pack()
    summary["evidence_pack_chars"] = len(evidence_pack)

    for job in candidates:
        persisted, _run_id, err = _rerank_single(provider, job, evidence_pack)
        if persisted is not None:
            summary["reranked"] += 1
        else:
            summary["errors"] += 1
            log.debug("rerank error for job %s: %s", job.get("id"), err)

    summary["elapsed_ms"] = int((time.time() - started) * 1000)
    try:
        audit("llm_rerank_batch", "system", None,
              reranked=summary["reranked"], errors=summary["errors"],
              elapsed_ms=summary["elapsed_ms"])
    except Exception:
        pass
    return summary


def rerank_one(job_id: int) -> dict:
    """Ad-hoc one-job semantic rescore. Always overwrites the existing row.

    Returns a dict the router can shape into an API response. On failure
    the dict contains an `error` key but `ok` stays False.
    """
    started = time.time()
    job = _fetch_one(int(job_id))
    if not job:
        return {
            "ok": False,
            "error": f"job {job_id} not found",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    if (settings.llm_provider or "").lower() == "template":
        return {
            "ok": False,
            "skipped_no_provider": True,
            "error": "LLM provider is set to 'template' — set Ollama / Anthropic / OpenAI first.",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    try:
        provider = get_llm()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "skipped_no_provider": True,
            "error": f"provider init failed: {type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    if getattr(provider, "name", "") == "template":
        return {
            "ok": False,
            "skipped_no_provider": True,
            "error": "no real LLM configured (template fallback active)",
            "elapsed_ms": int((time.time() - started) * 1000),
        }

    evidence_pack = _build_evidence_pack()
    persisted, run_id, err = _rerank_single(provider, job, evidence_pack)
    elapsed_ms = int((time.time() - started) * 1000)
    if persisted is None:
        try:
            audit("llm_rerank_one", "job_posting", int(job_id),
                  error=err or "unknown", elapsed_ms=elapsed_ms)
        except Exception:
            pass
        return {
            "ok": False,
            "error": err or "unknown",
            "llm_run_id": run_id if run_id and run_id > 0 else None,
            "elapsed_ms": elapsed_ms,
        }

    try:
        audit("llm_rerank_one", "job_posting", int(job_id),
              semantic_score=persisted.get("semantic_score"),
              elapsed_ms=elapsed_ms)
    except Exception:
        pass

    return {
        "ok": True,
        "data": persisted,
        "evidence_pack_chars": len(evidence_pack),
        "elapsed_ms": elapsed_ms,
    }
