"""GET/PUT /api/profile — singleton user profile.

Also: POST /api/profile/infer — parse an uploaded resume + LinkedIn paste
into a draft UserProfileIn dict WITHOUT saving. The UI uses this to
prefill the Setup form so the user reviews + edits before committing.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile

from ..config import settings
from ..db import get_conn, row_to_dict, audit, tx
from ..models.schemas import UserProfileIn, OK
from ..security.rate_limit import rate_limit
from ..security.uploads import validate_upload
from ..utils.text import dedupe_preserve_order

log = logging.getLogger("jhh.profile")

router = APIRouter(prefix="/api", tags=["profile"])


_LIST_FIELDS = ["target_titles", "target_keywords", "excluded_keywords",
                "preferred_locations", "employment_types", "seniority_targets",
                "industries", "excluded_industries", "preferred_companies",
                "excluded_companies", "visa_preferences"]

_JSON_FIELDS = ["interview_availability_json", "scoring_weights_json"]

_CITY_STATE_RE = re.compile(
    r"\b([A-Z][A-Za-z\.\-' ]+),\s+([A-Z]{2})\b"
)
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[^\s,;)]+", re.I)
_GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[^\s,;)]+", re.I)
_URL_RE = re.compile(r"https?://[^\s,;)]+", re.I)


# ----- existing endpoints -----

@router.get("/profile")
def get_profile() -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    if row is None:
        raise HTTPException(404, "profile row missing")
    return {"ok": True, "data": row_to_dict(row)}


# ----- profile completeness ----------------------------------------------

# Fields that count toward the completeness score. Each materially
# improves match quality or downstream tailoring.
_COMPLETENESS_FIELDS: list[str] = [
    "name", "email", "target_titles", "target_keywords",
    "preferred_locations", "employment_types", "seniority_targets",
    "currency", "mode", "minimum_salary", "location",
]

_COMPLETENESS_HINTS: dict[str, str] = {
    "name": "Add your full name so resumes and emails are signed.",
    "email": "Add a contact email so recruiters can reach you.",
    "target_titles": "List 2-5 job titles you want — drives every search.",
    "target_keywords": "List your top 8-12 skills/keywords to match jobs.",
    "preferred_locations": "Add at least one preferred location (or 'Remote').",
    "employment_types": "Specify employment types (full-time, contract, etc.).",
    "seniority_targets": "Pick the seniority levels you target (e.g. senior, staff).",
    "currency": "Set a salary currency so offers normalize correctly.",
    "mode": "Pick an operating mode (assisted, manual, autopilot).",
    "minimum_salary": "Add a minimum salary so under-paying jobs get filtered.",
    "location": "Add your current location to power local/relocation logic.",
}


def _is_filled(field: str, value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value != 0
    return True


def profile_completeness(row: dict) -> dict:
    """Score the profile against `_COMPLETENESS_FIELDS`. Returns
    {score: 0-100, missing: [...], filled: [...], suggestions: [...]}.
    """
    row = row or {}
    filled: list[str] = []
    missing: list[str] = []
    for f in _COMPLETENESS_FIELDS:
        if _is_filled(f, row.get(f)):
            filled.append(f)
        else:
            missing.append(f)
    total = len(_COMPLETENESS_FIELDS) or 1
    score = int(round(100 * len(filled) / total))
    suggestions = [_COMPLETENESS_HINTS.get(f, f"Set {f}.") for f in missing]
    return {"score": score, "missing": missing, "filled": filled,
            "suggestions": suggestions}


@router.get("/profile/completeness")
def get_profile_completeness() -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    data = row_to_dict(row) or {}
    return {"ok": True, "data": profile_completeness(data)}


@router.put("/profile")
def put_profile(body: UserProfileIn) -> OK:
    """Partial-update semantics: only SET fields the caller actually
    supplied. Using `exclude_unset=True` (not exclude_none) so explicit
    `null` from the UI clears a field, but unsupplied fields are
    untouched. Previously every PUT clobbered every column with whatever
    happened to be in the request body — saving the weekly availability
    grid wiped name/email/target_titles/everything else.

    Side effect: when `linkedin_url`, `github_url`, or `portfolio_url`
    changes from what was previously stored, we auto-enqueue a fetch +
    LLM re-extract for the new URL so the vault always reflects what the
    profile says. Failures (SSRF block, offline, robots.txt) are logged
    but do NOT fail the profile update — the URL is still persisted.
    """
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        return OK(detail="no fields supplied")

    conn = get_conn()
    # Capture pre-change URL values so we know what to auto-ingest.
    prev_row = conn.execute(
        "SELECT linkedin_url, github_url, portfolio_url FROM user_profile WHERE id = 1"
    ).fetchone()
    prev_urls = {
        "linkedin_url": (prev_row["linkedin_url"] or "").strip() if prev_row else "",
        "github_url": (prev_row["github_url"] or "").strip() if prev_row else "",
        "portfolio_url": (prev_row["portfolio_url"] or "").strip() if prev_row else "",
    }

    cols = []
    vals = []
    for k, v in payload.items():
        if k in _LIST_FIELDS:
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v or []))
        elif k in _JSON_FIELDS:
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v or {}))
        else:
            cols.append(f"{k} = ?")
            vals.append(v)
    cols.append("updated_at = ?")
    vals.append(time.time())
    sql = f"UPDATE user_profile SET {', '.join(cols)} WHERE id = 1"
    conn.execute(sql, vals)
    audit("profile_update", "user_profile", 1, fields=sorted(payload.keys()))

    # ----- Always-on URL ingest -----------------------------------------
    url_field_to_type = {
        "linkedin_url": "linkedin",
        "github_url": "github",
        "portfolio_url": "portfolio",
    }
    ingested: list[dict] = []
    for field, source_type in url_field_to_type.items():
        if field not in payload:
            continue
        new_val = (payload.get(field) or "")
        if not isinstance(new_val, str):
            continue
        new_val = new_val.strip()
        if not new_val:
            continue
        if new_val == prev_urls.get(field):
            continue
        try:
            from ..services import url_ingestion, career_vault, evidence_extractor
            fetched = url_ingestion.fetch_url(new_val)
            if "error" in fetched:
                log.info("profile URL ingest skipped (%s): %s",
                         new_val, fetched["error"])
                ingested.append({"field": field, "ok": False,
                                 "error": fetched["error"]})
                continue
            text = (fetched.get("text") or "").strip()
            if not text:
                ingested.append({"field": field, "ok": False,
                                 "error": "no readable text"})
                continue
            source_id = career_vault.add_source(
                source_type=source_type,
                title=fetched.get("title") or new_val,
                url=fetched.get("url") or new_val,
                raw_text=text,
                parsed_json={"content_type": fetched.get("content_type"),
                             "fetched_at": fetched.get("fetched_at")},
            )
            # LLM extract preferred; fall back to deterministic when LLM
            # isn't configured or the call fails.
            llm_run_id = None
            claims_inserted = 0
            tried_llm = False
            try:
                from ..llm import get_llm
                from ..llm.template_provider import TemplateProvider
                from ..services import llm_vault_reingest
                if not isinstance(get_llm(), TemplateProvider):
                    tried_llm = True
                    res = llm_vault_reingest.reingest_source_with_llm(int(source_id))
                    if res.get("ok"):
                        llm_run_id = res.get("llm_run_id")
                        claims_inserted = res.get("claims_inserted")
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM reingest after URL ingest failed: %s", exc)
            if claims_inserted == 0 and not tried_llm:
                claims = evidence_extractor.extract_claims(int(source_id), text, source_type)
                career_vault.add_claims(int(source_id), claims)
                claims_inserted = len(claims)
            ingested.append({
                "field": field,
                "ok": True,
                "source_id": int(source_id),
                "claims_inserted": int(claims_inserted),
                "llm_run_id": llm_run_id,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("auto URL ingest failed for %s: %s", field, exc)
            ingested.append({"field": field, "ok": False,
                             "error": f"{type(exc).__name__}: {exc}"})

    detail = f"profile updated: {len(payload)} field(s)"
    if ingested:
        detail += f"; auto-ingested {sum(1 for r in ingested if r.get('ok'))} URL(s)"
    return OK(detail=detail, data={"ingested_urls": ingested} if ingested else None)


# ----- infer endpoint -----

@router.post("/profile/infer")
@rate_limit("10/minute")
async def infer_profile(
    request: Request = None,  # type: ignore[assignment]
    resume_file: UploadFile | None = File(default=None),
    linkedin_text: str | None = Form(default=None),
    linkedin_html: str | None = Form(default=None),
    linkedin_url: str | None = Form(default=None),
    github_url: str | None = Form(default=None),
    portfolio_url: str | None = Form(default=None),
) -> dict:
    """Parse the supplied resume + LinkedIn text and return a draft
    UserProfileIn. Nothing is persisted. Caller decides what to keep,
    edits it, and POSTs to PUT /api/profile to save.
    """
    # Enforce upload caps on the resume file BEFORE we read bytes — header
    # check is cheap and catches the obvious attack.
    if resume_file is not None and getattr(resume_file, "filename", None):
        validate_upload(resume_file, ("pdf", "docx", "doc", "md", "txt", "html"))
    inferred_fields: dict[str, Any] = {}
    inferred_meta: dict[str, list[str]] = {}   # field_name -> [sources]
    notes: list[str] = []
    sources_used: list[dict] = []

    resume_data: dict[str, Any] = {}
    linkedin_data: dict[str, Any] = {}

    # ---- parse resume if provided ----
    if resume_file is not None:
        try:
            resume_data = await _parse_resume_upload(resume_file)
            sources_used.append({
                "kind": "resume",
                "filename": resume_file.filename or "uploaded",
                "size_bytes": resume_data.get("_size", 0),
                "skills_found": len(resume_data.get("skills", []) or []),
                "experience_entries": len(resume_data.get("experience", []) or []),
            })
        except Exception as exc:  # noqa: BLE001
            notes.append(f"resume parse failed: {exc}")
            log.warning("resume parse failed: %s", exc)

    # ---- parse LinkedIn text or html if provided ----
    if linkedin_text or linkedin_html:
        try:
            linkedin_data = _parse_linkedin(linkedin_text, linkedin_html)
            sections = linkedin_data.get("sections") or {}
            sources_used.append({
                "kind": "linkedin",
                "sections_found": list(sections.keys()),
                "raw_text_chars": len(linkedin_data.get("raw_text", "") or ""),
            })
        except Exception as exc:  # noqa: BLE001
            notes.append(f"linkedin parse failed: {exc}")
            log.warning("linkedin parse failed: %s", exc)

    # ---- merge into profile fields ----
    def _set(field: str, value: Any, source: str) -> None:
        if value in (None, "", [], {}):
            return
        if field not in inferred_fields:
            inferred_fields[field] = value
            inferred_meta[field] = [source]
        else:
            inferred_meta[field].append(source)

    # name / email / phone — resume header is best
    if resume_data:
        _set("name", resume_data.get("name", "") or None, "resume")
        _set("email", resume_data.get("email", "") or None, "resume")
        _set("phone", resume_data.get("phone", "") or None, "resume")

    # location — try resume contacts header first, then linkedin "Contact" section
    loc = _detect_location(resume_data.get("_text") if resume_data else None,
                           linkedin_data.get("raw_text") if linkedin_data else None)
    if loc:
        _set("location", loc, "resume" if resume_data else "linkedin")

    # urls — explicit form fields win, then auto-detect from text
    if linkedin_url:
        _set("linkedin_url", linkedin_url.strip(), "user_input")
    elif resume_data:
        _set("linkedin_url", _first_match(_LINKEDIN_RE, resume_data.get("_text", "")), "resume")

    if github_url:
        _set("github_url", github_url.strip(), "user_input")
    elif resume_data:
        _set("github_url", _first_match(_GITHUB_RE, resume_data.get("_text", "")), "resume")

    if portfolio_url:
        _set("portfolio_url", portfolio_url.strip(), "user_input")
    elif resume_data:
        # Pick the first non-linkedin/github URL from links
        for link in resume_data.get("links", []) or []:
            if "linkedin.com" in link.lower() or "github.com" in link.lower():
                continue
            _set("portfolio_url", link, "resume")
            break

    # target_titles — most recent role title is the obvious default
    titles = _collect_titles(resume_data, linkedin_data)
    if titles:
        _set("target_titles", titles, "resume+linkedin" if resume_data and linkedin_data else
             ("resume" if resume_data else "linkedin"))

    # target_keywords — top skills from resume; supplement from linkedin Skills section
    keywords = _collect_keywords(resume_data, linkedin_data)
    if keywords:
        _set("target_keywords", keywords, "resume+linkedin" if resume_data and linkedin_data else
             ("resume" if resume_data else "linkedin"))

    # seniority_targets — derive from most recent title via seniority_parser
    if titles:
        sen = _detect_seniority_targets(titles[0])
        if sen:
            _set("seniority_targets", sen, "resume")

    # preferred_locations — default to current location if known; add "Remote" mention if LinkedIn open-to-remote signals
    if loc:
        prefs = [loc]
        if linkedin_data and _signals_remote_openness(linkedin_data.get("raw_text", "") or ""):
            prefs.append("Remote")
        _set("preferred_locations", prefs, "resume+linkedin" if loc and linkedin_data else "resume")

    # currency — defaulted via location heuristic
    cur = _guess_currency(loc or "")
    if cur:
        _set("currency", cur, "location")

    # ---- assemble draft, fill remaining schema fields with their defaults ----
    draft = UserProfileIn().model_dump(exclude_none=False)
    # Don't clobber non-empty existing inferred values; keep defaults for the rest
    for k, v in inferred_fields.items():
        draft[k] = v

    if not sources_used:
        notes.append("nothing supplied; returning blank draft")

    meta = {
        "inferred_fields": sorted(inferred_meta.keys()),
        "inferred_meta": inferred_meta,
        "sources_used": sources_used,
        "notes": notes,
    }

    # ---- LLM inference + human-gate proposal ----
    # Run the LLM extractor when a provider is available. The deterministic
    # draft remains the response's primary payload (back-compat); the LLM
    # output is added as a side-channel for the human review gate.
    llm_fields: dict[str, Any] | None = None
    llm_run_id: int | None = None
    llm_error: str | None = None
    differences: dict[str, dict[str, Any]] = {}
    proposal_id: int | None = None

    resume_text = (resume_data or {}).get("_text", "") or ""
    linkedin_raw = (linkedin_data or {}).get("raw_text", "") or ""

    provider_available = (settings.llm_provider or "auto").lower() != "template"
    if provider_available and (resume_text or linkedin_raw):
        try:
            from ..services.llm_profile_inference import infer_with_llm
            llm_result = infer_with_llm(
                resume_text=resume_text,
                linkedin_text=linkedin_raw,
                target_type="profile",
                target_id=1,
            )
            if llm_result.get("ok"):
                llm_fields = llm_result.get("fields") or {}
                llm_run_id = int(llm_result.get("llm_run_id") or -1)
            else:
                llm_error = llm_result.get("error") or "llm inference failed"
                llm_run_id = int(llm_result.get("llm_run_id") or -1)
                notes.append(f"llm inference: {llm_error}")
        except Exception as exc:  # noqa: BLE001
            log.warning("llm inference failed: %s", exc)
            llm_error = str(exc)
            notes.append(f"llm inference exception: {exc}")

    # Source label for the proposal row. Resume wins when both supplied.
    if resume_file is not None:
        proposal_source = "resume"
    elif linkedin_text or linkedin_html:
        proposal_source = "linkedin"
    elif linkedin_url:
        proposal_source = "linkedin_url"
    else:
        proposal_source = "mixed"

    # Build the side-by-side differences map (only fields where the two
    # parsers actually disagree, including the case where one is absent).
    deterministic_fields_for_diff = {k: inferred_fields.get(k) for k in inferred_fields}
    if llm_fields is not None:
        all_keys = set(deterministic_fields_for_diff.keys()) | set(llm_fields.keys())
        for k in sorted(all_keys):
            d = deterministic_fields_for_diff.get(k)
            l = llm_fields.get(k)
            if _values_equal(d, l):
                continue
            differences[k] = {"deterministic": d, "llm": l}

    # Persist a proposal row whenever we have at least one signal worth
    # remembering — even a deterministic-only run. The human reviewer can
    # then audit later, or the autopilot UI can surface it.
    if (inferred_fields or llm_fields) and (resume_text or linkedin_raw):
        try:
            with get_conn() as conn:
                cur = conn.execute(
                    """INSERT INTO profile_proposal
                       (created_at, source, deterministic_json, llm_json,
                        llm_run_id, status)
                       VALUES (?, ?, ?, ?, ?, 'pending')""",
                    (
                        time.time(),
                        proposal_source,
                        json.dumps(inferred_fields, default=str),
                        json.dumps(llm_fields) if llm_fields is not None else None,
                        llm_run_id if llm_run_id and llm_run_id > 0 else None,
                    ),
                )
                proposal_id = int(cur.lastrowid)
            audit("profile_proposal_created", "profile_proposal", proposal_id,
                  source=proposal_source,
                  deterministic_keys=sorted(inferred_fields.keys()),
                  llm_keys=sorted((llm_fields or {}).keys()),
                  differences=sorted(differences.keys()))
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to persist profile_proposal: %s", exc)
            notes.append(f"proposal save failed: {exc}")

    # Embed the meta INSIDE data so the standard `{ok, data:{...}}` envelope
    # holds. Keep the top-level keys for backward-compat with v0.1 callers.
    proposal_meta: dict[str, Any] = {}
    if proposal_id is not None:
        proposal_meta["proposal_id"] = proposal_id
    if llm_fields is not None:
        proposal_meta["llm"] = llm_fields
    if llm_run_id and llm_run_id > 0:
        proposal_meta["llm_run_id"] = llm_run_id
    if llm_error:
        proposal_meta["llm_error"] = llm_error
    if differences:
        proposal_meta["differences"] = differences
    proposal_meta["deterministic"] = dict(inferred_fields)

    return {
        "ok": True,
        "data": {**draft, **meta},
        **meta,
        **proposal_meta,
    }


def _values_equal(a: Any, b: Any) -> bool:
    """Tolerant equality for comparing deterministic vs LLM field values.

    - Lists are compared as lowercased multisets (order-insensitive).
    - Strings compared case-insensitively after stripping.
    - None / empty / missing all collapse to a single 'absent' state.
    """
    a_absent = a is None or a == "" or a == [] or a == {}
    b_absent = b is None or b == "" or b == [] or b == {}
    if a_absent and b_absent:
        return True
    if a_absent or b_absent:
        return False
    if isinstance(a, list) and isinstance(b, list):
        return sorted(str(x).strip().lower() for x in a) == sorted(str(x).strip().lower() for x in b)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


# ----- proposal endpoints (human review gate) ---------------------------

def _proposal_row(pid: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM profile_proposal WHERE id = ?", (int(pid),),
    ).fetchone()
    if not row:
        return None
    d = row_to_dict(row) or {}
    # row_to_dict already auto-decodes *_json columns, but defensively
    # parse here too in case the column was stored as a non-decodable type.
    def _coerce(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (dict, list)):
            return v
        if isinstance(v, str) and v.strip():
            try:
                return json.loads(v)
            except Exception:
                return None
        return None
    d["deterministic"] = _coerce(d.get("deterministic_json")) or {}
    d["llm"] = _coerce(d.get("llm_json")) or {}
    d["accepted_fields"] = _coerce(d.get("accepted_fields_json")) or {}
    # Compute differences server-side so the client doesn't have to.
    det = d.get("deterministic") or {}
    llm = d.get("llm") or {}
    diffs: dict[str, dict[str, Any]] = {}
    for k in sorted(set(det.keys()) | set(llm.keys())):
        dv = det.get(k)
        lv = llm.get(k)
        if not _values_equal(dv, lv):
            diffs[k] = {"deterministic": dv, "llm": lv}
    d["differences"] = diffs
    return d


@router.get("/profile/proposals")
def list_proposals(limit: int = 20, status: str = "pending") -> dict:
    """List recent proposals, newest first. Default to pending only."""
    limit = max(1, min(int(limit), 100))
    if status:
        rows = get_conn().execute(
            """SELECT id, created_at, source, llm_run_id, status, applied_at
               FROM profile_proposal
               WHERE status = ?
               ORDER BY id DESC LIMIT ?""",
            (status, limit),
        ).fetchall()
    else:
        rows = get_conn().execute(
            """SELECT id, created_at, source, llm_run_id, status, applied_at
               FROM profile_proposal
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "source": r["source"],
            "llm_run_id": r["llm_run_id"],
            "status": r["status"],
            "applied_at": r["applied_at"],
        })
    return {"ok": True, "data": {"proposals": out, "count": len(out)}}


@router.get("/profile/proposals/{pid}")
def get_proposal(pid: int) -> dict:
    d = _proposal_row(pid)
    if not d:
        raise HTTPException(404, "proposal not found")
    return {"ok": True, "data": d}


@router.post("/profile/proposals/{pid}/accept")
def accept_proposal(pid: int, body: dict = Body(default={})) -> dict:
    """Body: {accepted_fields: {field_name: "deterministic" | "llm" | <raw value>}}

    Builds the final field set from the user's choices, writes it to
    `user_profile`, marks the proposal applied, and returns the updated
    profile row.
    """
    d = _proposal_row(pid)
    if not d:
        raise HTTPException(404, "proposal not found")
    if d.get("status") == "applied":
        raise HTTPException(409, "proposal already applied")

    accepted_choices = (body or {}).get("accepted_fields") or {}
    if not isinstance(accepted_choices, dict):
        raise HTTPException(400, "accepted_fields must be an object")

    det = d.get("deterministic") or {}
    llm = d.get("llm") or {}

    final_fields: dict[str, Any] = {}
    resolved: dict[str, str] = {}  # field -> source label for audit
    for field, choice in accepted_choices.items():
        if isinstance(choice, str) and choice in ("deterministic", "llm"):
            src = choice
            val = det.get(field) if src == "deterministic" else llm.get(field)
            if val is None:
                continue
            final_fields[field] = val
            resolved[field] = src
        else:
            # Raw value (user-edited override)
            final_fields[field] = choice
            resolved[field] = "manual"

    # Validate against UserProfileIn so we only update real columns and
    # benefit from its coercion (e.g. comma-string → list for list fields).
    try:
        # Coerce list fields from CSV string if the UI sent them as such
        for k in _LIST_FIELDS:
            v = final_fields.get(k)
            if isinstance(v, str):
                final_fields[k] = [s.strip() for s in v.split(",") if s.strip()]
        candidate = UserProfileIn(**{k: v for k, v in final_fields.items()
                                     if k in UserProfileIn.model_fields})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"validation failed: {exc}")

    payload = candidate.model_dump(exclude_unset=True)
    # Only write keys the user actually chose — exclude_unset above + the
    # explicit filter below makes this idempotent for partial accepts.
    payload = {k: v for k, v in payload.items() if k in final_fields}

    cols: list[str] = []
    vals: list[Any] = []
    for k, v in payload.items():
        if k in _LIST_FIELDS:
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v or []))
        elif k in _JSON_FIELDS:
            cols.append(f"{k} = ?")
            vals.append(json.dumps(v or {}))
        else:
            cols.append(f"{k} = ?")
            vals.append(v)
    now = time.time()
    # Profile update + proposal status flip must land together — a failure
    # between them would leave the profile changed but the proposal pending.
    with tx() as c:
        if cols:
            cols.append("updated_at = ?")
            vals.append(now)
            sql = f"UPDATE user_profile SET {', '.join(cols)} WHERE id = 1"
            c.execute(sql, vals)
        c.execute(
            """UPDATE profile_proposal
               SET status = 'applied', applied_at = ?, accepted_fields_json = ?
               WHERE id = ?""",
            (now, json.dumps(resolved), int(pid)),
        )
    audit("profile_proposal_accepted", "profile_proposal", int(pid),
          fields=sorted(payload.keys()), resolved=resolved)

    new_row = get_conn().execute(
        "SELECT * FROM user_profile WHERE id = 1"
    ).fetchone()
    return {
        "ok": True,
        "data": {
            "proposal_id": int(pid),
            "applied_fields": sorted(payload.keys()),
            "resolved": resolved,
            "profile": row_to_dict(new_row) or {},
        },
    }


@router.get("/profile/url-ingest-status")
def get_url_ingest_status() -> dict:
    """For each profile URL field, report whether the corresponding
    evidence_source has actually been ingested. The LinkedIn URL in
    particular often shows status='blocked_by_robots' since LinkedIn's
    robots.txt forbids unauthenticated profile crawls. The UI uses this
    to surface a paste-this-text fallback prominently."""
    conn = get_conn()
    row = conn.execute(
        "SELECT linkedin_url, github_url, portfolio_url FROM user_profile WHERE id = 1"
    ).fetchone()
    if not row:
        return {"ok": True, "data": {}}
    out: dict[str, Any] = {}
    for field, source_type in (("linkedin_url", "linkedin"),
                               ("github_url", "github"),
                               ("portfolio_url", "portfolio")):
        url = (row[field] or "").strip()
        if not url:
            out[field] = {"url": "", "ingested": False, "status": "unset",
                          "evidence_source_id": None, "char_count": 0}
            continue
        src = conn.execute(
            "SELECT id, length(raw_text) AS chars FROM evidence_source "
            "WHERE source_type = ? AND url = ? ORDER BY id DESC LIMIT 1",
            (source_type, url),
        ).fetchone()
        if src and (src["chars"] or 0) > 0:
            out[field] = {"url": url, "ingested": True, "status": "ok",
                          "evidence_source_id": src["id"], "char_count": int(src["chars"])}
            continue
        # No evidence_source — figure out why
        if "linkedin.com" in url.lower():
            note = "LinkedIn's robots.txt blocks automated profile fetches. Paste the visible profile text below or upload a LinkedIn data export — we'll run the LLM extractor on it."
            out[field] = {"url": url, "ingested": False, "status": "blocked_by_robots",
                          "evidence_source_id": None, "char_count": 0,
                          "remediation": note}
        else:
            out[field] = {"url": url, "ingested": False, "status": "not_fetched",
                          "evidence_source_id": None, "char_count": 0,
                          "remediation": "Fetch hasn't run yet, or the page returned empty. Use the VAULT UPDATE drawer to paste text directly."}
    return {"ok": True, "data": out}


@router.post("/profile/snapshot")
def post_profile_snapshot() -> dict:
    """Generate the user's career snapshot — who they are, what they do,
    where they are in their career, next-step recommendations + job
    recommendations. Uses the LLM if available; honest deterministic
    fallback when not. Persists as the new latest row."""
    from ..services.career_snapshot import generate_snapshot
    return generate_snapshot()


@router.get("/profile/snapshot")
def get_profile_snapshot() -> dict:
    """Return the latest snapshot, or null if none generated yet."""
    from ..services.career_snapshot import get_latest_snapshot
    snap = get_latest_snapshot()
    if snap is None:
        return {"ok": True, "data": None}
    return {"ok": True, "data": snap}


@router.post("/profile/proposals/{pid}/reject")
def reject_proposal(pid: int) -> dict:
    row = get_conn().execute(
        "SELECT id, status FROM profile_proposal WHERE id = ?", (int(pid),),
    ).fetchone()
    if not row:
        raise HTTPException(404, "proposal not found")
    if row["status"] == "applied":
        raise HTTPException(409, "cannot reject an applied proposal")
    get_conn().execute(
        "UPDATE profile_proposal SET status = 'rejected' WHERE id = ?",
        (int(pid),),
    )
    audit("profile_proposal_rejected", "profile_proposal", int(pid))
    return {"ok": True, "data": {"proposal_id": int(pid), "status": "rejected"}}


# ----- helpers -----

async def _parse_resume_upload(upload: UploadFile) -> dict[str, Any]:
    """Read upload into a temp file, parse via document + resume parser.

    Returns the resume parser's dict plus `_text` (raw text) and `_size`.
    """
    from ..services.document_parser import parse_file
    from ..services.resume_parser import parse as parse_resume

    suffix = ""
    name = upload.filename or "resume"
    if "." in name:
        suffix = "." + name.rsplit(".", 1)[1].lower()

    raw = await upload.read()
    # Safety-net size check now that we know the real byte count. Header
    # might have been absent or lied; this is the truth.
    validate_upload(upload, ("pdf", "docx", "doc", "md", "txt", "html"), raw_bytes=raw)
    with tempfile.NamedTemporaryFile(prefix="jhh_infer_", suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)

    try:
        doc = parse_file(tmp_path)
        text = (doc or {}).get("text", "") or ""
        if not text.strip():
            raise RuntimeError("no extractable text — is it a scanned PDF?")
        parsed = parse_resume(text)
        parsed["_text"] = text
        parsed["_size"] = len(raw)
        return parsed
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass


def _parse_linkedin(text: str | None, html: str | None) -> dict[str, Any]:
    from ..services.linkedin_ingestion import ingest_html, ingest_text
    if html:
        return ingest_html(html)
    return ingest_text(text or "")


def _first_match(pat: re.Pattern[str], text: str) -> str:
    if not text:
        return ""
    m = pat.search(text)
    if not m:
        return ""
    val = m.group(0).rstrip(".,;)")
    if not val.startswith("http"):
        val = "https://" + val
    return val


def _detect_location(resume_text: str | None, linkedin_text: str | None) -> str:
    """Look for "City, ST" pattern in the first 800 chars of resume header,
    falling back to LinkedIn's `Contact` section.
    """
    candidates: list[str] = []
    if resume_text:
        candidates.append(resume_text[:800])
    if linkedin_text:
        candidates.append(linkedin_text)
    for c in candidates:
        m = _CITY_STATE_RE.search(c)
        if m:
            return f"{m.group(1).strip()}, {m.group(2)}"
        # common LinkedIn `City, State, Country` triple — accept first two
        m2 = re.search(r"\b([A-Z][A-Za-z\-' ]+),\s+([A-Z][A-Za-z\-' ]+)(?:,\s+[A-Z][A-Za-z ]+)?\b",
                       c[:800])
        if m2 and m2.group(2).lower() not in {"present", "current"}:
            cand = f"{m2.group(1).strip()}, {m2.group(2).strip()}"
            if 5 <= len(cand) <= 50:
                candidates.append(cand)
                return cand
    return ""


def _collect_titles(resume: dict[str, Any], linkedin: dict[str, Any]) -> list[str]:
    """Build a forward-looking target_titles list.

    target_titles must be CLEAN job-title strings — never "Title — Company
    (dates)" suffixes. Strategy:
      1. Take the most recent role title from `resume.experience[*].title`,
         strip any "— Company" / "@ Company" / "(dates)" / trailing "()" noise.
      2. Add the forward-looking promotion (Senior → Staff → Principal, etc.).
      3. Add at most one sibling-discipline title (Senior Engineer →
         Engineering Manager).

    The user can edit any of these in Setup; this is the smart default.
    """
    raw: list[str] = []
    for exp in (resume.get("experience") or []):
        t = (exp.get("title") or "").strip()
        if t and 3 <= len(t) <= 120:
            raw.append(t)
    # LinkedIn 'experience' is a single text block — grab the first title-looking line
    if linkedin:
        exp_block = (linkedin.get("sections") or {}).get("experience", "")
        for line in (exp_block or "").splitlines():
            s = line.strip()
            if 3 <= len(s) <= 120 and not re.search(r"\d{4}", s):
                if any(w[0].isupper() for w in s.split()[:3] if w):
                    raw.append(s)
                    break

    titles: list[str] = []
    for t in raw:
        cleaned = _clean_title(t)
        if cleaned and 3 <= len(cleaned) <= 60 and _looks_like_role_title(cleaned):
            titles.append(cleaned)

    # Forward-looking suggestion based on the cleanest (first) title
    if titles:
        bumped = _bump_title(titles[0])
        for b in bumped:
            if b and b not in titles and _looks_like_role_title(b):
                titles.append(b)

    return dedupe_preserve_order(titles)[:6]


# Role-indicator vocabulary covers every industry family Job Hunt Hacker
# supports (tech, infosec, healthcare, legal, finance, creative, education,
# trades, sales/marketing, ops). A cleaned title must contain at least one of
# these tokens OR look like a multi-word role phrase — otherwise it is
# rejected as a likely employer name (e.g. "eBay", "Google", "Stripe").
_ROLE_INDICATORS = {
    # tech
    "engineer", "developer", "programmer", "architect", "scientist", "analyst",
    "designer", "devops", "sre", "sysadmin", "administrator", "qa", "tester",
    "researcher",
    # security
    "security", "infosec", "soc", "siem", "ir", "incident", "threat",
    "vulnerability", "pentester", "ciso", "cyber", "hacker", "redteam",
    "blueteam", "purple", "detection", "forensics", "grc", "compliance",
    "auditor", "risk",
    # management
    "manager", "director", "lead", "leader", "head", "chief", "vp", "president",
    "supervisor", "coordinator", "officer", "executive", "founder", "owner",
    "partner", "principal", "staff", "senior", "junior", "intern", "associate",
    # consulting / specialist
    "specialist", "consultant", "advisor", "advocate", "agent", "representative",
    "rep", "ambassador", "evangelist",
    # medical
    "nurse", "doctor", "physician", "therapist", "pharmacist", "technician",
    "technologist", "paramedic", "medic", "surgeon", "dentist", "hygienist",
    "radiologist", "clinician", "psychologist", "psychiatrist", "midwife",
    "veterinarian", "vet",
    # legal
    "attorney", "lawyer", "paralegal", "counsel", "clerk", "judge", "barrister",
    "solicitor",
    # finance / accounting
    "accountant", "controller", "bookkeeper", "trader", "broker", "banker",
    "underwriter", "actuary", "treasurer", "cfo",
    # creative
    "writer", "editor", "journalist", "producer", "photographer", "artist",
    "illustrator", "copywriter", "filmmaker", "musician", "composer",
    "animator", "videographer", "creator",
    # education
    "teacher", "professor", "instructor", "dean", "tutor", "librarian",
    "educator", "coach", "trainer", "facilitator",
    # trades
    "electrician", "plumber", "carpenter", "mechanic", "welder", "mason",
    "foreman", "operator", "machinist", "driver", "pilot", "captain",
    # sales / marketing / ops
    "sales", "salesperson", "marketing", "marketer", "account", "buyer",
    "merchandiser", "recruiter", "sourcer", "planner", "scheduler",
    "dispatcher", "logistician",
    # generic / catch-all roles
    "engineer", "manager", "director", "specialist", "intern", "fellow",
    "apprentice", "assistant", "administrator", "coordinator",
}


def _looks_like_role_title(s: str) -> bool:
    """Reject likely company names that the resume parser confused with titles.

    A real job title either:
      (a) contains at least one token from the role-indicator vocabulary, OR
      (b) contains a clear seniority/level prefix (Senior, Staff, Principal,
          Lead, Head of, Chief, VP of, ...).
    Single-word capitalized strings with neither signal — e.g. "eBay",
    "Google", "Stripe" — are rejected.
    """
    s = (s or "").strip()
    if not s:
        return False
    lower = s.lower()
    tokens = re.findall(r"[a-z]+", lower)
    if not tokens:
        return False
    if any(tok in _ROLE_INDICATORS for tok in tokens):
        return True
    # Seniority/leadership phrases that don't always carry a role noun on their own
    if re.search(r"\b(head of|chief|vp of|vice president|c[a-z]o)\b", lower):
        return True
    # Final guard: a true title is rarely a single capitalized word.
    # If we got here we matched no role indicator AND no seniority phrase, so
    # require at least 2 alphabetic tokens AND no obvious company-only signal.
    if len(tokens) >= 2 and not re.search(r"\b(inc|llc|ltd|corp|co|gmbh|s\.?a\.?|technologies|systems)\b", lower):
        return True
    return False


def _clean_title(raw: str) -> str:
    """Strip employer + date noise from a role title string.

    Examples:
      "Staff Backend Engineer — Lattice Data Systems (2020-2023)" → "Staff Backend Engineer"
      "Senior PM @ Stripe" → "Senior PM"
      "Software Engineer | Acme | 2018 - Present" → "Software Engineer"
      "Designer ()" → "Designer"
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # Remove anything from the first em-dash / hyphen / pipe / @ onward.
    # These are the canonical "Title — Company" / "Title | Company" / "Title @ Company" separators.
    for sep in (" — ", " – ", " | ", " @ ", " at "):
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    # Strip trailing date range: "Senior Engineer 2019-2022" / "PM 2020 - Present"
    s = re.sub(r"\s+\d{4}\s*[-–—]\s*(?:\d{4}|present|now|current)\s*$",
               "", s, flags=re.I).strip()
    # Strip "(...) YYYY" patterns: "Senior Engineer (Brightline) 2019-2022"
    s = re.sub(r"\s*\([^)]*\)\s*\d{4}.*$", "", s).strip()
    # Strip any remaining trailing (…) — empty parens or date span at end
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    # Strip trailing employer in plain hyphen form: "Title - Company"
    s = re.sub(r"\s+-\s+[A-Z][A-Za-z0-9 .,&'-]+$", "", s).strip()
    # Drop trailing punctuation
    s = s.rstrip(" -,|·")
    return s


def _bump_title(title: str) -> list[str]:
    """Given a current title, return zero or more forward-looking variants
    that represent the next likely step on the user's trajectory."""
    if not title:
        return []
    t = title.strip()
    out: list[str] = []

    # 1) Roman/numeric level: II → III, III → IV, etc.
    m = re.search(r"\b(I{1,4}V?|IV|V)\b\s*$", t)
    if m:
        roman = m.group(1)
        roman_map = {"I": "II", "II": "III", "III": "IV", "IV": "V", "V": "VI"}
        if roman in roman_map:
            out.append(t[:m.start()].rstrip() + " " + roman_map[roman])
    m2 = re.search(r"\b(\d+)\b\s*$", t)
    if m2:
        try:
            n = int(m2.group(1))
            out.append(t[:m2.start()].rstrip() + " " + str(n + 1))
        except ValueError:
            pass

    # 2) Word-prefix bump: Junior → Mid → Senior → Staff → Principal
    PROMOTIONS = [
        (r"^\bJunior\b\s*", "Mid-Level "),
        (r"^\bJr\.?\b\s*", "Mid-Level "),
        (r"^\bAssociate\b\s*", "Mid-Level "),
        (r"^\bMid[-\s]?Level\b\s*", "Senior "),
        (r"^\bMid\b\s*", "Senior "),
        (r"^\bSenior\b\s*", "Staff "),
        (r"^\bSr\.?\b\s*", "Staff "),
        (r"^\bStaff\b\s*", "Principal "),
        (r"^\bPrincipal\b\s*", "Distinguished "),
    ]
    for pat, repl in PROMOTIONS:
        if re.search(pat, t, re.I):
            out.append(re.sub(pat, repl, t, count=1, flags=re.I).strip())
            break
    # If no seniority prefix at all, prepend "Senior" as a likely next rung
    if not any(re.search(p[0], t, re.I) for p in PROMOTIONS) and not m and not m2:
        if not re.search(r"^\b(VP|Chief|Director|Head|CTO|CEO|CFO|COO)\b", t, re.I):
            out.append("Senior " + t)
            out.append("Staff " + t)

    # 3) Sibling promotion: IC → Manager (e.g. "Senior Engineer" → "Engineering Manager")
    m3 = re.search(r"(?i)\b(engineer|designer|analyst|scientist|developer)\b", t)
    if m3:
        discipline = m3.group(1).lower()
        if discipline == "engineer":
            out.append("Engineering Manager")
        elif discipline == "designer":
            out.append("Design Manager")
        elif discipline == "analyst":
            out.append("Analytics Manager")
        elif discipline == "scientist":
            out.append("Data Science Manager")
        elif discipline == "developer":
            out.append("Engineering Manager")

    # 4) Manager → Director step
    if re.search(r"(?i)\bManager\b", t) and not re.search(r"(?i)\b(Director|VP|Head)\b", t):
        out.append(re.sub(r"(?i)\bManager\b", "Director", t, count=1).strip())

    return [o.strip() for o in out if o and o.strip() != t.strip()]


def _collect_keywords(resume: dict[str, Any], linkedin: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for s in (resume.get("skills") or []):
        s = s.strip()
        if s:
            out.append(s)
    if linkedin:
        skills_block = (linkedin.get("sections") or {}).get("skills", "")
        for s in re.split(r"[\n,•·\|;]", skills_block or ""):
            s = re.sub(r"^[\-\*\s]+", "", s).strip()
            # drop endorsement counts like "Python · 32"
            s = re.sub(r"·.*$", "", s).strip()
            if s and 2 <= len(s) <= 60:
                out.append(s)
    # Also pass-through via the canonical extractor so we promote known
    # canonical names (e.g. "k8s" → "Kubernetes")
    try:
        from ..matching.skills_extractor import extract_skills
        combined = " ".join(out) + " " + (resume.get("_text", "") or "")
        canonical = extract_skills(combined)
        out = canonical + [s for s in out if s.lower() not in {c.lower() for c in canonical}]
    except Exception:
        pass
    return dedupe_preserve_order(out)[:12]


def _detect_seniority_targets(title: str) -> list[str]:
    try:
        from ..matching.seniority_parser import detect_seniority
    except Exception:
        return []
    lvl = detect_seniority(title or "")
    if not lvl:
        return []
    # Suggest the detected level + the next-step level (the user's likely target)
    ladder = ["intern", "entry", "mid", "senior", "staff", "principal",
              "manager", "director", "vp", "exec"]
    out = [lvl]
    try:
        i = ladder.index(lvl)
        # Suggest next non-IC step intelligently — keep one rung up on IC ladder
        if lvl in ("entry", "mid", "senior", "staff") and i + 1 < len(ladder):
            nxt = ladder[i + 1]
            if nxt not in out:
                out.append(nxt)
    except ValueError:
        pass
    return out


def _signals_remote_openness(text: str) -> bool:
    t = (text or "").lower()
    return any(s in t for s in (
        "open to remote", "remote-friendly", "remote first", "remote-first",
        "open to relocation", "open to work · remote", "remote ·",
    ))


def _guess_currency(location: str) -> str:
    if not location:
        return ""
    t = location.lower()
    if any(x in t for x in (", uk", ", gb", "london", "england")):
        return "GBP"
    if any(x in t for x in ("ireland", "germany", "france", "spain", "italy",
                            "netherlands", "portugal", "belgium")):
        return "EUR"
    if "canada" in t or re.search(r",\s+(ON|BC|AB|QC|MB|NS|NB|SK|NL|PE|YT|NT|NU)$", location):
        return "CAD"
    if "australia" in t:
        return "AUD"
    if "switzerland" in t or "zurich" in t or "geneva" in t:
        return "CHF"
    return "USD"
