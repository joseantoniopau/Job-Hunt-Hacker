"""LLM-driven re-ingest of vault evidence sources.

This is the strict, "honesty rules" complement to the deterministic
`evidence_extractor.extract_claims`. It:

  1. Asks the LLM to extract structured claims from the source's `raw_text`.
  2. REQUIRES every claim include a `source_span` that is a literal
     substring of the raw_text (case-insensitive `in` check). Claims whose
     `source_span` cannot be located in raw_text are DROPPED.
  3. Wipes existing `career_claim` rows for the source in a single
     transaction and writes the verified claims in their place.

Used by:
  * `/api/vault/reingest` and `/api/vault/sources/{id}/reingest`
  * `/api/vault/quick-update` (after URL/text ingest)
  * The autopilot pipeline (upgrading deterministic claims after resume
    ingest).

Returns a small dict so the UI can show "old_count -> new_count + dropped".
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from ..db import audit, get_conn, row_to_dict, tx
from ..llm import get_llm
from ..llm.json_repair import extract_json
from ..llm.observability import observed_complete
from ..utils.text import normalize
from . import vector_store

log = logging.getLogger("jhh.vault.reingest")


# How much raw_text we send the model. Big enough to cover a full resume +
# a LinkedIn profile, small enough to stay under context limits and keep
# round-trips fast.
_MAX_TEXT_CHARS = 14000

# Claim types we accept from the LLM. Mirrors evidence_extractor.CLAIM_TYPES
# but adds the friendlier rubric labels used in the prompt schema.
_ACCEPTED_CLAIM_TYPES = {
    "role", "accomplishment", "skill", "tool", "certification",
    "degree", "project", "publication", "metric", "responsibility",
    "leadership", "achievement", "credential",
}

# Map prompt-schema labels back to the canonical career_claim.claim_type.
_CLAIM_TYPE_ALIAS = {
    "achievement": "accomplishment",
    "credential": "certification",
}


_SYSTEM_PROMPT = (
    "You are a career-claim extractor. Extract structured CLAIMS from a "
    "candidate's resume / LinkedIn / portfolio text.\n\n"
    "HARD RULES (non-negotiable):\n"
    "  1. Output JSON only. No prose, no markdown fence required but "
    "tolerated.\n"
    "  2. Each claim MUST include a `source_span` that is the EXACT "
    "substring from the input text proving the claim. Never paraphrase "
    "the source_span. If you cannot find a literal substring proving the "
    "claim, do NOT include the claim.\n"
    "  3. Never invent verbs, metrics, scopes, tools, or skills that are "
    "not in the text. If a number isn't in the source, you don't get to "
    "use a number.\n"
    "  4. Distinguish EMPLOYERS (eBay, Google, Stripe, Lattice) from "
    "TITLES (Software Engineer, Security Engineer, Staff PM). A title is "
    "what someone does; an employer is who pays them.\n"
    "  5. Prefer specific over generic. \"Shipped fraud-detection model "
    "that cut chargebacks 18%\" beats \"worked on fraud\".\n"
    "  6. One claim per fact. Don't bundle three achievements into one "
    "claim_text just to save tokens.\n"
    "  7. BE EXHAUSTIVE — cover the WHOLE work history. For EVERY position "
    "in the text (every title + employer + date range, including early-"
    "career and short stints), output one `role` claim whose claim_text "
    "names the exact title and employer, with date_start/date_end copied "
    "from the text. Then extract that position's accomplishments as "
    "separate claims. A full profile typically yields 15-40 claims; "
    "stopping after the most recent job is a failure.\n"
)


# ---------------------------------------------------------------------------
# Input cleanup — pasted LinkedIn profiles arrive full of UI chrome ("Home",
# "My Network", "Get started", "Show all 12 posts"...). The noise buries the
# Experience section and measurably degrades extraction recall on smaller
# local models, so we drop pure-chrome lines before prompting. Spans the
# model quotes from the cleaned text still verify against raw_text because
# we only ever remove whole lines and the span check is whitespace-
# normalized.
# ---------------------------------------------------------------------------

_CHROME_EXACT = {
    "home", "my network", "jobs", "messaging", "notifications", "me",
    "for business", "advertise", "enhance profile", "add section",
    "open to", "cover photo", "contact info", "activity",
    "create a post", "analytics", "private to you", "suggested for you",
    "stand out to employers", "get started", "add services", "more",
    "follow", "connect", "message", "pending", "view profile",
    "people also viewed", "people you may know", "promoted",
}
_CHROME_PREFIX_RE = re.compile(
    r"^(show all\b|thumbnail for\b|see all\b|loading\b|skip to\b)", re.I)
_CHROME_SUFFIX_RE = re.compile(r"(\blogo|… ?more|…more)\s*$", re.I)


def _strip_profile_chrome(text: str) -> str:
    """Remove whole lines that are platform UI chrome, never content."""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.lower() in _CHROME_EXACT:
            continue
        if _CHROME_PREFIX_RE.match(s) or _CHROME_SUFFIX_RE.search(s):
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Deterministic position parser — a completeness backstop. LLM extraction
# may under-report (skip early-career roles, drop titles/dates), but a
# pasted profile's Experience section has a regular shape:
#
#   grouped (multiple roles at one employer):     simple:
#       Employer                                      Title
#       8 yrs                                         Employer
#       Title                                         Mon YYYY - Mon YYYY · 2 yrs
#       Full-time
#       Mon YYYY - Present · 2 yrs 5 mos
#
# We find date-range lines and walk back to recover title + employer. Any
# position the LLM missed becomes a synthesized `role` claim — built from
# literal text lines, so the no-fabrication contract holds by construction.
# ---------------------------------------------------------------------------

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_DATE_RANGE_RE = re.compile(
    rf"^(?P<start>{_MONTH}\.? \d{{4}})\s*[-–]\s*(?P<end>{_MONTH}\.? \d{{4}}|present)"
    r"(\s*·.*)?$",
    re.I,
)
_DURATION_RE = re.compile(r"^\d+\s*yrs?(\s+\d+\s*mos?)?$|^\d+\s*mos?$", re.I)
_EMPLOYMENT_TYPE_RE = re.compile(
    r"^(full[- ]time|part[- ]time|contract|internship|freelance|"
    r"self[- ]employed|temporary|apprenticeship|seasonal)$", re.I)


def _is_walkback_noise(s: str) -> bool:
    """Lines skipped while walking back from a date line to find the title."""
    return bool(
        _EMPLOYMENT_TYPE_RE.match(s)
        or _DURATION_RE.match(s)
        or "·" in s
        or "," in s          # locations: "Mountain View, California"
    )


def _is_hard_stop(s: str) -> bool:
    """Description prose or another date line ends the walk-back."""
    return len(s) > 70 or bool(_DATE_RANGE_RE.match(s))


def parse_positions(text: str) -> list[dict]:
    """Extract (title, employer, date_start, date_end) for every dated
    position found in a resume / LinkedIn-style text. Heuristic but
    deterministic; returns [] when the text has no recognizable positions."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    positions: list[dict] = []
    current_group: Optional[str] = None

    for i, s in enumerate(lines):
        if not s:
            continue
        # Company-group header: short line whose next non-empty line is a
        # total-duration line ("eBay" / "8 yrs"). Blank lines in between are
        # common in real pastes.
        k = i + 1
        while k < len(lines) and not lines[k]:
            k += 1
        if (k < len(lines) and _DURATION_RE.match(lines[k])
                and len(s) < 60 and not _DATE_RANGE_RE.match(s)
                and not _is_walkback_noise(s)):
            current_group = s
            continue

        m = _DATE_RANGE_RE.match(s)
        if not m:
            continue

        # Walk back to collect up to two candidate lines (title / employer).
        cands: list[str] = []
        j = i - 1
        while j >= 0 and len(cands) < 2:
            prev = lines[j]
            j -= 1
            if not prev:
                continue
            if _is_hard_stop(prev):
                break
            if _is_walkback_noise(prev):
                continue
            if current_group and prev == current_group:
                break
            cands.append(prev)
        if not cands:
            continue

        if len(cands) == 2:
            # simple layout: nearest line is the employer, the one above is
            # the title — and a simple-layout entry means we've left any
            # grouped-employer section.
            title, employer = cands[1], cands[0]
            current_group = None
        else:
            title, employer = cands[0], current_group

        positions.append({
            "title": title.rstrip(" ·"),
            "employer": (employer or "").strip() or None,
            "date_start": m.group("start"),
            "date_end": m.group("end"),
            "span_line": s,
        })
    return positions


def _supplement_missing_positions(
    candidates: list[dict], positions: list[dict], source_id: int,
) -> tuple[list[dict], int]:
    """Append a synthesized `role` claim for every parsed position the LLM
    missed. Returns (claims, added_count). A position counts as covered when
    any role claim mentions its title, or matches its employer + start date."""
    added = 0
    roles = [
        (_norm_for_match(c.get("claim_text") or ""),
         (c.get("date_start") or "").strip().lower(),
         _norm_for_match(c.get("employer") or ""))
        for c in candidates if c.get("claim_type") == "role"
    ]
    for pos in positions:
        title_n = _norm_for_match(pos["title"])
        pos_date = pos["date_start"].strip().lower()
        pos_emp = _norm_for_match(pos.get("employer") or "")
        # Covered when a role claim mentions the title with a compatible
        # start date ("Widget Engineer" must not be claimed covered by
        # "Senior Widget Engineer" from a different period), or pins the
        # same employer + start date.
        covered = any(
            (title_n in text and (not date or date == pos_date))
            or (pos_emp and emp == pos_emp and date == pos_date)
            for text, date, emp in roles
        )
        if covered:
            continue
        text = (f'{pos["title"]} at {pos["employer"]}' if pos.get("employer")
                else pos["title"])
        candidates.append({
            "source_id": source_id,
            "claim_type": "role",
            "claim_text": text,
            "normalized_claim": normalize(text),
            "date_start": pos["date_start"],
            "date_end": pos["date_end"],
            "employer": pos.get("employer"),
            "project": None,
            "skill": None,
            "tool": None,
            # Deterministically parsed from literal title/date lines —
            # slightly below the LLM+span-verified 0.9.
            "confidence": 0.85,
            "evidence_strength": "medium",
            "user_verified": 0,
            "allowed_for_resume": 1,
            "contradiction_status": "none",
            "_source_span": pos["span_line"],
        })
        added += 1
    return candidates, added


def _user_prompt(text: str) -> str:
    schema = (
        '[\n'
        '  {\n'
        '    "verb": "shipped|led|built|reduced|...  (single past-tense verb)",\n'
        '    "metric": "exact metric from text, or empty string",\n'
        '    "scope": "team / org / customer count / dollar scale, from text, or empty",\n'
        '    "tools": ["exact tool names from text"],\n'
        '    "skills": ["exact skills/keywords from text"],\n'
        '    "source_span": "EXACT substring of input text proving this claim",\n'
        '    "claim_type": "achievement|skill|role|credential",\n'
        '    "claim_text": "one-sentence summary written from the source_span only",\n'
        '    "employer": "employer name from text, or empty",\n'
        '    "date_start": "from text, or empty",\n'
        '    "date_end": "from text, or empty"\n'
        '  }\n'
        ']\n'
    )
    return (
        "Extract claims from this source text. Output a JSON list using "
        "this schema:\n\n"
        f"{schema}\n"
        "Cover EVERY position in the work history — one `role` claim per "
        "title + employer + date range, plus its accomplishments.\n\n"
        "INPUT TEXT (use only what's here):\n"
        "---\n"
        f"{(text or '')[:_MAX_TEXT_CHARS]}\n"
        "---\n"
    )


def _coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join([str(x).strip() for x in v if str(x).strip()])
    return str(v).strip()


def _normalize_claim_type(raw: str) -> str:
    c = (raw or "").strip().lower()
    if c in _CLAIM_TYPE_ALIAS:
        c = _CLAIM_TYPE_ALIAS[c]
    if c in _ACCEPTED_CLAIM_TYPES:
        return c
    # Best-effort: many close synonyms can be mapped.
    if c in ("experience", "work", "job", "position"):
        return "role"
    if c in ("award", "honor", "result"):
        return "accomplishment"
    if c in ("language", "framework", "library", "technology", "stack"):
        return "tool"
    if c in ("education", "diploma"):
        return "degree"
    if c in ("paper", "talk", "patent"):
        return "publication"
    if c in ("management", "leader"):
        return "leadership"
    return "responsibility"


_WS_RE = re.compile(r"\s+")


def _norm_for_match(s: str) -> str:
    return _WS_RE.sub(" ", (s or "").strip().lower())


def _span_in_text(span: str, source_text: str) -> bool:
    """The honesty rule: span must be a literal (case-insensitive) substring
    of source_text after whitespace-normalization.

    We normalize because models OFTEN re-flow whitespace even when they
    "quote" — turning `"led a team\nof 4 engineers"` into `"led a team of
    4 engineers"`. Either form should still verify. Anything beyond that
    (paraphrasing, missing tokens, fabricated metrics) gets dropped.
    """
    if not span or not source_text:
        return False
    # Must be long enough that we don't accept "the" as evidence for
    # everything; 6 characters keeps short skill tags ("python", "kafka")
    # while rejecting filler.
    cleaned = span.strip()
    if len(cleaned) < 6:
        return False
    return _norm_for_match(cleaned) in _norm_for_match(source_text)


def _build_claim_row(source_id: int, raw: dict, source_text: str) -> Optional[dict]:
    """Validate one LLM claim dict; return a ready-to-insert row or None.

    Returns None when the claim is unusable (missing span, span not in text,
    no extractable claim_text). Never raises — bad rows are silently dropped
    and the caller reports the count.
    """
    if not isinstance(raw, dict):
        return None
    span = _coerce_str(raw.get("source_span"))
    if not _span_in_text(span, source_text):
        return None

    # Build a one-sentence claim_text from whatever fields the model
    # provided. Prefer an explicit `claim_text`, fall back to the
    # source_span itself (which we already verified is in the text).
    claim_text = _coerce_str(raw.get("claim_text")) or span
    claim_text = _WS_RE.sub(" ", claim_text).strip()
    if not claim_text:
        return None

    claim_type = _normalize_claim_type(_coerce_str(raw.get("claim_type")))
    employer = _coerce_str(raw.get("employer")) or None
    tools = raw.get("tools") if isinstance(raw.get("tools"), list) else []
    skills = raw.get("skills") if isinstance(raw.get("skills"), list) else []
    tool_str = ", ".join([str(t).strip() for t in tools if str(t).strip()]) or None
    skill_str = ", ".join([str(s).strip() for s in skills if str(s).strip()]) or None
    date_start = _coerce_str(raw.get("date_start")) or None
    date_end = _coerce_str(raw.get("date_end")) or None

    return {
        "source_id": source_id,
        "claim_type": claim_type,
        "claim_text": claim_text,
        "normalized_claim": normalize(claim_text),
        "date_start": date_start,
        "date_end": date_end,
        "employer": employer,
        "project": None,
        "skill": skill_str,
        "tool": tool_str,
        # Confidence: LLM-extracted + literal-span-verified ⇒ high.
        "confidence": 0.9,
        "evidence_strength": "strong" if (raw.get("metric") or skill_str or tool_str) else "medium",
        "user_verified": 0,
        "allowed_for_resume": 1,
        "contradiction_status": "none",
        "_source_span": span,
    }


def _replace_claims(source_id: int, new_claims: list[dict]) -> tuple[int, int]:
    """Replace ALL career_claim rows for `source_id` with `new_claims`.

    Returns (old_count, inserted_count). Single transaction. Embeddings for
    the removed claims are best-effort cleaned up afterwards so vector
    search doesn't keep stale references.
    """
    conn = get_conn()
    old_ids: list[int] = []
    with tx() as c:
        rows = c.execute(
            "SELECT id FROM career_claim WHERE source_id = ?", (int(source_id),)
        ).fetchall()
        old_ids = [int(r["id"]) for r in rows]
        c.execute("DELETE FROM career_claim WHERE source_id = ?", (int(source_id),))

        now = time.time()
        inserted = 0
        new_ids_pairs: list[tuple[int, dict]] = []
        for claim in new_claims:
            cur = c.execute(
                "INSERT INTO career_claim "
                "(source_id, claim_type, claim_text, normalized_claim, "
                "date_start, date_end, employer, project, skill, tool, "
                "confidence, evidence_strength, user_verified, "
                "allowed_for_resume, contradiction_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    claim["claim_type"],
                    claim["claim_text"],
                    claim["normalized_claim"],
                    claim.get("date_start"),
                    claim.get("date_end"),
                    claim.get("employer"),
                    claim.get("project"),
                    claim.get("skill"),
                    claim.get("tool"),
                    float(claim.get("confidence") or 0.9),
                    claim.get("evidence_strength") or "medium",
                    int(claim.get("user_verified") or 0),
                    int(claim.get("allowed_for_resume", 1)),
                    claim.get("contradiction_status") or "none",
                    now,
                ),
            )
            inserted += 1
            new_ids_pairs.append((int(cur.lastrowid), claim))

    # Best-effort embedding cleanup + refresh — outside the tx so vector
    # store flakiness can't roll back the claim writes.
    try:
        for old_id in old_ids:
            try:
                vector_store.remove("claim", old_id)
            except Exception:
                pass
        for new_id, claim in new_ids_pairs:
            try:
                vector_store.add("claim", new_id, claim["claim_text"])
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001
        log.debug("vector refresh after reingest failed: %s", e)

    return len(old_ids), inserted


def reingest_source_with_llm(source_id: int) -> dict:
    """Re-extract claims for one evidence_source using the LLM with strict
    source_span verification.

    Returns:
        {
            "ok": bool,
            "source_id": int,
            "claims_old_count": int,
            "claims_inserted": int,
            "claims_dropped_unverified": int,
            "llm_run_id": int | None,
            "elapsed_ms": int,
            "error": str | None,
        }
    """
    started = time.time()
    src_row = get_conn().execute(
        "SELECT id, source_type, raw_text, title FROM evidence_source WHERE id = ?",
        (int(source_id),),
    ).fetchone()
    if src_row is None:
        return {
            "ok": False,
            "source_id": source_id,
            "claims_old_count": 0,
            "claims_inserted": 0,
            "claims_dropped_unverified": 0,
            "llm_run_id": None,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": "source not found",
        }
    raw_text = (src_row["raw_text"] or "").strip()
    if not raw_text:
        return {
            "ok": False,
            "source_id": int(source_id),
            "claims_old_count": 0,
            "claims_inserted": 0,
            "claims_dropped_unverified": 0,
            "llm_run_id": None,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": "source has empty raw_text",
        }

    try:
        llm = get_llm()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "source_id": int(source_id),
            "claims_old_count": 0,
            "claims_inserted": 0,
            "claims_dropped_unverified": 0,
            "llm_run_id": None,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": f"LLM unavailable: {exc}",
        }

    system = _SYSTEM_PROMPT
    # Strip platform UI chrome before prompting — spans quoted from the
    # cleaned text still verify against raw_text (whole-line removal only).
    cleaned_text = _strip_profile_chrome(raw_text)
    user = _user_prompt(cleaned_text)

    try:
        output, llm_run_id = observed_complete(
            llm,
            "vault_reingest",
            system,
            user,
            max_tokens=4000,
            temperature=0.0,
            target_type="evidence_source",
            target_id=int(source_id),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM call failed during vault re-ingest for %s: %s", source_id, exc)
        return {
            "ok": False,
            "source_id": int(source_id),
            "claims_old_count": 0,
            "claims_inserted": 0,
            "claims_dropped_unverified": 0,
            "llm_run_id": None,
            "elapsed_ms": int((time.time() - started) * 1000),
            "error": f"LLM call failed: {type(exc).__name__}: {exc}",
        }

    parsed = extract_json(output or "")
    if isinstance(parsed, dict):
        # Some models wrap the list under {"claims": [...]} despite the
        # prompt asking for a bare list — accept both.
        if isinstance(parsed.get("claims"), list):
            parsed = parsed["claims"]
        elif isinstance(parsed.get("data"), list):
            parsed = parsed["data"]
    if not isinstance(parsed, list):
        log.warning("vault_reingest %s: parser found no list (output head=%r)",
                    source_id, (output or "")[:200])
        parsed = []

    candidates: list[dict] = []
    dropped = 0
    for raw in parsed:
        built = _build_claim_row(int(source_id), raw, raw_text)
        if built is None:
            dropped += 1
            continue
        candidates.append(built)

    # Completeness backstop: any dated position the LLM missed becomes a
    # deterministic `role` claim parsed from literal title/date lines.
    positions = parse_positions(cleaned_text)
    candidates, synthesized = _supplement_missing_positions(
        candidates, positions, int(source_id))
    if synthesized:
        log.info("vault_reingest %s: synthesized %d role claim(s) the LLM "
                 "missed (%d positions parsed)", source_id, synthesized,
                 len(positions))

    # Dedupe by (claim_type, normalized_claim) so the LLM can't pad the count
    # with paraphrases of the same fact.
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for c in candidates:
        key = (c["claim_type"], (c["normalized_claim"] or "")[:160].lower())
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        deduped.append(c)

    old_count, inserted_count = _replace_claims(int(source_id), deduped)

    audit(
        "vault_reingest",
        "evidence_source",
        int(source_id),
        llm_run_id=llm_run_id,
        old_count=old_count,
        inserted=inserted_count,
        dropped_unverified=dropped,
        source_type=src_row["source_type"],
    )

    return {
        "ok": True,
        "source_id": int(source_id),
        "claims_old_count": int(old_count),
        "claims_inserted": int(inserted_count),
        "claims_dropped_unverified": int(dropped),
        "llm_run_id": int(llm_run_id) if llm_run_id and llm_run_id > 0 else None,
        "elapsed_ms": int((time.time() - started) * 1000),
        "error": None,
    }


def reingest_all_sources_with_llm() -> dict:
    """Run `reingest_source_with_llm` for every evidence_source.

    Aggregates per-source results into a summary. Per-source failures don't
    abort the run — we keep going so one bad row can't block re-extracting
    a whole vault.
    """
    started = time.time()
    rows = get_conn().execute(
        "SELECT id FROM evidence_source ORDER BY id ASC"
    ).fetchall()
    src_ids = [int(r["id"]) for r in rows]
    results: list[dict] = []
    total_old = 0
    total_inserted = 0
    total_dropped = 0
    errors = 0
    for sid in src_ids:
        try:
            res = reingest_source_with_llm(sid)
        except Exception as exc:  # noqa: BLE001
            res = {
                "ok": False,
                "source_id": sid,
                "claims_old_count": 0,
                "claims_inserted": 0,
                "claims_dropped_unverified": 0,
                "llm_run_id": None,
                "elapsed_ms": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        results.append(res)
        total_old += int(res.get("claims_old_count") or 0)
        total_inserted += int(res.get("claims_inserted") or 0)
        total_dropped += int(res.get("claims_dropped_unverified") or 0)
        if not res.get("ok"):
            errors += 1

    audit(
        "vault_reingest_all",
        "evidence_source",
        None,
        sources_processed=len(src_ids),
        total_inserted=total_inserted,
        total_dropped=total_dropped,
        errors=errors,
    )

    return {
        "ok": True,
        "sources_processed": len(src_ids),
        "results": results,
        "totals": {
            "claims_old": total_old,
            "claims_inserted": total_inserted,
            "claims_dropped_unverified": total_dropped,
            "errors": errors,
        },
        "elapsed_ms": int((time.time() - started) * 1000),
    }
