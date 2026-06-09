"""LLM-powered interview prep packets + practice (mock interview) mode.

Two halves:
  * Prep packet — POST /api/interview/prep/{application_id}
    Builds a JSON packet (company brief, behavioral + technical + scenario
    questions, STAR skeletons) anchored to the candidate's Career Vault
    evidence and the JD. Every reference cites an evidence claim_id.

  * Practice — POST /api/interview/practice/{application_id}/start
    Pre-picks N questions from the latest packet, asks them one at a time,
    grades each answer against the EVIDENCE PACK so answers that name real
    claims get credit and unverified claims get flagged honestly.

All LLM calls go through `observed_complete()` so they show up in the LLM
activity panel and the user can replay the prompt + output.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import audit, get_conn, row_to_dict, tx
from ..llm import get_llm
from ..llm.json_repair import extract_json
from ..llm.observability import observed_complete
from ..services import career_vault
from ..tailoring import interview_library

log = logging.getLogger("jhh.routers.interview_prep")

router = APIRouter(prefix="/api/interview", tags=["interview_prep"])


# ----------------------------------------------------------------------
# Pydantic bodies
# ----------------------------------------------------------------------

class StartPracticeBody(BaseModel):
    question_count: int = 5


class SubmitAnswerBody(BaseModel):
    turn_index: int
    user_answer: str


# ----------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------

def _load_application(application_id: int) -> dict:
    """Application row + job_posting joined. Raises 404 if missing."""
    conn = get_conn()
    row = conn.execute(
        """SELECT a.*, j.title AS job_title, j.company AS job_company,
                  j.location AS job_location, j.description AS job_description,
                  j.requirements AS job_requirements,
                  j.apply_url AS job_apply_url, j.source AS job_source
           FROM application a
           LEFT JOIN job_posting j ON j.id = a.job_id
           WHERE a.id = ?""",
        (int(application_id),),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"application {application_id} not found")
    return row_to_dict(row) or {}


def _load_user_profile() -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    return row_to_dict(row) or {}


def _evidence_pack_for_job(job: dict, top: int = 20) -> list[dict]:
    """Pull the top-N most relevant career_claim rows for this JD.

    Falls back to the most-recent verified claims when the vector store is
    empty or the JD text is too sparse to retrieve against.
    """
    text_parts = [
        job.get("job_title") or "",
        job.get("job_company") or "",
        job.get("job_description") or "",
        " ".join(job.get("job_requirements") or [])
        if isinstance(job.get("job_requirements"), list)
        else (job.get("job_requirements") or ""),
    ]
    text = " ".join(p for p in text_parts if p).strip()
    rows: list[dict] = []
    if text:
        try:
            rows = career_vault.retrieve_for_job(text, top=top) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("evidence retrieval failed: %s", exc)
            rows = []
    if not rows:
        # Fall back to most-recent verified claims so the LLM still has
        # something concrete to ground on.
        conn = get_conn()
        sql = ("SELECT * FROM career_claim "
               "WHERE allowed_for_resume = 1 "
               "ORDER BY user_verified DESC, created_at DESC LIMIT ?")
        rs = conn.execute(sql, (int(top),)).fetchall()
        rows = [row_to_dict(r) for r in rs]
    return rows


def _evidence_for_prompt(rows: list[dict]) -> list[dict]:
    """Trim down a vault claim row to the small fields the LLM needs.

    Keeps `id` so the LLM can cite it back in `suggested_claim_id` /
    `evidence_used` lists, and keeps `claim_type`, the text, and a tiny
    `provenance` blob from the parent evidence_source (title + url + type)
    so the UI can show "claim is from your LinkedIn / résumé / GitHub".
    """
    conn = get_conn()
    out: list[dict] = []
    for c in rows or []:
        cid = c.get("id")
        text = (c.get("claim_text") or "").strip()
        if not cid or not text:
            continue
        src_id = c.get("source_id")
        provenance: dict[str, Any] = {}
        if src_id:
            try:
                src = conn.execute(
                    "SELECT source_type, title, filename, url FROM evidence_source WHERE id = ?",
                    (int(src_id),),
                ).fetchone()
                if src:
                    provenance = {
                        "source_id": int(src_id),
                        "source_type": src["source_type"],
                        "title": src["title"],
                        "filename": src["filename"],
                        "url": src["url"],
                    }
            except Exception:  # noqa: BLE001
                provenance = {}
        out.append({
            "id": int(cid),
            "claim_type": c.get("claim_type"),
            "claim_text": text,
            "employer": c.get("employer"),
            "skill": c.get("skill"),
            "tool": c.get("tool"),
            "user_verified": int(c.get("user_verified") or 0) == 1,
            "provenance": provenance,
        })
    return out


# ----------------------------------------------------------------------
# Prep packet generation
# ----------------------------------------------------------------------

_HONESTY_PARAGRAPH = (
    "EVIDENCE PACK contains the candidate's ONLY verifiable claims. Never "
    "reference experience, skills, or accomplishments outside it. Never invent "
    "company facts beyond what is in the JOB DESCRIPTION. If a question asks "
    "about something the candidate has no evidence for, your skeleton should "
    "suggest they say so honestly."
)


_PREP_SYSTEM = (
    "You generate an interview prep packet for a candidate. Output strict JSON "
    "matching the schema in the user message — no prose, no markdown fences. "
    "Use ONLY the EVIDENCE PACK for candidate references — never invent. "
    "Tailor questions to the JOB DESCRIPTION's stated requirements. Identify "
    "the candidate's strongest claim from EVIDENCE PACK that supports each "
    "behavioral question.\n\n"
    + _HONESTY_PARAGRAPH
)


def _build_prep_user_prompt(job: dict, evidence_pack: list[dict],
                            profile: dict) -> str:
    """Assemble the USER-side prompt with JD + evidence + schema."""
    name = profile.get("name") or "the candidate"
    target_titles = profile.get("target_titles") or []
    target_str = ", ".join(target_titles) if isinstance(target_titles, list) else str(target_titles or "")

    parts: list[str] = []
    parts.append(f"CANDIDATE: {name}")
    if target_str:
        parts.append(f"CANDIDATE TARGET TITLES: {target_str}")
    parts.append("")
    parts.append("JOB DESCRIPTION")
    parts.append(f"  title: {job.get('job_title') or ''}")
    parts.append(f"  company: {job.get('job_company') or ''}")
    parts.append(f"  location: {job.get('job_location') or ''}")
    jd_text = (job.get("job_description") or "").strip()
    if len(jd_text) > 4000:
        jd_text = jd_text[:4000] + "\n…[truncated]"
    parts.append("  description:")
    parts.append(jd_text or "(no description provided)")
    parts.append("")
    parts.append("EVIDENCE PACK (cite by `id`)")
    if not evidence_pack:
        parts.append("  (no verified claims available — keep skeletons honest "
                     "and suggest the candidate acknowledge gaps)")
    else:
        parts.append(json.dumps(evidence_pack, indent=2, default=str))
    parts.append("")
    parts.append(
        "Return JSON with EXACTLY this schema:\n"
        "{\n"
        '  "company_brief": "3-4 sentences summarizing what the JOB DESCRIPTION '
        'tells us about the company and role — do not invent outside facts",\n'
        '  "behavioral_questions": [\n'
        '    {"question": "...", "target_competency": "leadership|collaboration|...",\n'
        '     "suggested_claim_id": <int from EVIDENCE PACK or null>}\n'
        "  ],   // exactly 8 entries, tied to JD requirements\n"
        '  "technical_questions": [\n'
        '    {"question": "...", "skill_or_tool": "...",\n'
        '     "suggested_claim_id": <int from EVIDENCE PACK or null>}\n'
        "  ],   // exactly 6 entries, tied to tools/skills in the JD\n"
        '  "scenario_questions": [\n'
        '    {"question": "What would you do if ...", "judgement_axis": "..."}\n'
        "  ],   // exactly 4 entries\n"
        '  "star_skeletons": [\n'
        '    {"situation_from_claim_id": <int>, "behavioral_question": "...",\n'
        '     "draft_star": {"situation": "...", "task": "...", "action": "...",\n'
        '                     "result": "..."}}\n'
        "  ]   // exactly 5 entries, each pulling situation from a real claim\n"
        "}\n"
        "When EVIDENCE PACK contains no relevant claim for a behavioral or "
        "technical question, use null for `suggested_claim_id` — do not pick a "
        "random unrelated claim."
    )
    return "\n".join(parts)


def _deterministic_packet(job: dict, evidence_pack: list[dict]) -> dict:
    """Fallback packet used when no LLM call succeeds.

    Pulls behavioral questions from the role-family library + general
    bucket, picks technical questions tagged to claim tools/skills, and
    writes STAR skeletons from the top 5 claims so the UI still renders
    something useful + honest.
    """
    library = interview_library.load_questions() or {}
    role_qs = interview_library.questions_for_role(job.get("job_title") or "", n=8) or []
    if len(role_qs) < 8:
        general = library.get("general") or []
        for q in general:
            if q not in role_qs:
                role_qs.append(q)
            if len(role_qs) >= 8:
                break
    behavioral = []
    for i, q in enumerate(role_qs[:8]):
        suggested = evidence_pack[i]["id"] if i < len(evidence_pack) else None
        behavioral.append({
            "question": q,
            "target_competency": "general",
            "suggested_claim_id": suggested,
        })

    tools = sorted({(c.get("tool") or c.get("skill") or "").strip()
                    for c in evidence_pack
                    if (c.get("tool") or c.get("skill"))})
    tools = [t for t in tools if t][:6]
    if not tools:
        tools = ["the primary tool listed in the JD"] * 6
    technical: list[dict] = []
    for i, t in enumerate(tools[:6]):
        technical.append({
            "question": f"Walk me through a real project where you used {t}.",
            "skill_or_tool": t,
            "suggested_claim_id": evidence_pack[i]["id"] if i < len(evidence_pack) else None,
        })

    scenario = [
        {"question": "What would you do if you discovered a critical bug right "
                     "before a launch with no time to revert?",
         "judgement_axis": "risk-management"},
        {"question": "What would you do if a senior teammate kept blocking your "
                     "PRs with conflicting feedback?",
         "judgement_axis": "conflict"},
        {"question": "What would you do if the metric you optimized started "
                     "moving in the wrong direction one week post-launch?",
         "judgement_axis": "instrumentation"},
        {"question": "What would you do if a customer escalation revealed a "
                     "process gap on your team?",
         "judgement_axis": "ownership"},
    ]

    skeletons = []
    for i in range(min(5, len(evidence_pack))):
        claim = evidence_pack[i]
        text = claim.get("claim_text") or ""
        skeletons.append({
            "situation_from_claim_id": claim["id"],
            "behavioral_question": behavioral[min(i, len(behavioral) - 1)]["question"]
            if behavioral else "Tell me about a recent project.",
            "draft_star": {
                "situation": text,
                "task": "What you were responsible for delivering.",
                "action": "Anchor in the verified work above; describe what you did.",
                "result": "Quantify the outcome only if it appears in EVIDENCE PACK.",
            },
        })

    return {
        "company_brief": (job.get("job_description") or "").strip()[:400]
        or "(no company brief available — JD is empty)",
        "behavioral_questions": behavioral,
        "technical_questions": technical,
        "scenario_questions": scenario,
        "star_skeletons": skeletons,
    }


def _normalize_packet(parsed: Any, evidence_pack: list[dict],
                      job: dict) -> dict:
    """Coerce a possibly-messy LLM response into the expected shape."""
    if not isinstance(parsed, dict):
        return _deterministic_packet(job, evidence_pack)
    valid_ids = {c["id"] for c in evidence_pack}

    def _clean_int(v: Any) -> Optional[int]:
        try:
            iv = int(v)
            return iv if iv in valid_ids else None
        except Exception:
            return None

    def _coerce_list(key: str, expected_keys: list[str]) -> list[dict]:
        raw = parsed.get(key) or []
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for item in raw:
            if isinstance(item, str):
                out.append({"question": item})
            elif isinstance(item, dict):
                cleaned = {k: item.get(k) for k in expected_keys}
                if "suggested_claim_id" in expected_keys:
                    cleaned["suggested_claim_id"] = _clean_int(item.get("suggested_claim_id"))
                out.append(cleaned)
        return out

    behavioral = _coerce_list("behavioral_questions",
                              ["question", "target_competency", "suggested_claim_id"])
    technical = _coerce_list("technical_questions",
                             ["question", "skill_or_tool", "suggested_claim_id"])
    scenario = _coerce_list("scenario_questions", ["question", "judgement_axis"])

    skeletons_raw = parsed.get("star_skeletons") or []
    skeletons: list[dict] = []
    if isinstance(skeletons_raw, list):
        for sk in skeletons_raw:
            if not isinstance(sk, dict):
                continue
            cid = _clean_int(sk.get("situation_from_claim_id"))
            star = sk.get("draft_star") or {}
            if not isinstance(star, dict):
                star = {}
            skeletons.append({
                "situation_from_claim_id": cid,
                "behavioral_question": sk.get("behavioral_question"),
                "draft_star": {
                    "situation": star.get("situation"),
                    "task": star.get("task"),
                    "action": star.get("action"),
                    "result": star.get("result"),
                },
            })

    company_brief = parsed.get("company_brief")
    if not isinstance(company_brief, str) or not company_brief.strip():
        company_brief = (job.get("job_description") or "").strip()[:400] or ""

    # Ensure we always have something useful — backfill empty buckets.
    if not behavioral or not technical or not scenario or not skeletons:
        fallback = _deterministic_packet(job, evidence_pack)
        if not behavioral:
            behavioral = fallback["behavioral_questions"]
        if not technical:
            technical = fallback["technical_questions"]
        if not scenario:
            scenario = fallback["scenario_questions"]
        if not skeletons:
            skeletons = fallback["star_skeletons"]

    return {
        "company_brief": company_brief.strip(),
        "behavioral_questions": behavioral,
        "technical_questions": technical,
        "scenario_questions": scenario,
        "star_skeletons": skeletons,
    }


def _generate_prep_packet(application_id: int) -> dict:
    """Generate a fresh prep packet, persist it, return the full row."""
    app = _load_application(application_id)
    job_id = app.get("job_id")
    profile = _load_user_profile()
    evidence_rows = _evidence_pack_for_job(app, top=20)
    evidence_pack = _evidence_for_prompt(evidence_rows)

    provider = get_llm()
    sys_prompt = _PREP_SYSTEM
    user_prompt = _build_prep_user_prompt(app, evidence_pack, profile)
    llm_run_id = -1
    raw = ""
    parsed: Any = None
    try:
        raw, llm_run_id = observed_complete(
            provider,
            "interview_prep",
            sys_prompt,
            user_prompt,
            max_tokens=3500,
            temperature=0.2,
            target_type="application",
            target_id=int(application_id),
        )
        if raw:
            parsed = extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("interview_prep LLM call failed: %s", exc)
        parsed = None

    packet = _normalize_packet(parsed, evidence_pack, app)

    # Persist
    now = time.time()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO interview_prep_packet
               (application_id, job_id, created_at, company_brief,
                behavioral_questions_json, technical_questions_json,
                scenario_questions_json, star_skeletons_json, llm_run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(application_id),
                int(job_id) if job_id else None,
                now,
                packet["company_brief"],
                json.dumps(packet["behavioral_questions"]),
                json.dumps(packet["technical_questions"]),
                json.dumps(packet["scenario_questions"]),
                json.dumps(packet["star_skeletons"]),
                int(llm_run_id) if llm_run_id and llm_run_id > 0 else None,
            ),
        )
        packet_id = int(cur.lastrowid)

    audit("interview_prep_packet", "application", int(application_id),
          packet_id=packet_id, llm_run_id=llm_run_id,
          claims_used=len(evidence_pack))

    return _packet_row(packet_id)


def _packet_row(packet_id: int) -> dict:
    row = get_conn().execute(
        "SELECT * FROM interview_prep_packet WHERE id = ?",
        (int(packet_id),),
    ).fetchone()
    return row_to_dict(row) or {}


def _latest_packet_for(application_id: int) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT * FROM interview_prep_packet "
        "WHERE application_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (int(application_id),),
    ).fetchone()
    return row_to_dict(row) if row else None


# ----------------------------------------------------------------------
# Prep endpoints
# ----------------------------------------------------------------------

@router.post("/prep/{application_id}")
def create_prep(application_id: int) -> dict:
    packet = _generate_prep_packet(int(application_id))
    return {
        "ok": True,
        "data": packet,
        "llm_run_id": packet.get("llm_run_id"),
    }


@router.get("/prep/{application_id}")
def get_prep(application_id: int) -> dict:
    packet = _latest_packet_for(int(application_id))
    if not packet:
        raise HTTPException(404, f"no interview prep packet for application {application_id}")
    return {"ok": True, "data": packet}


@router.get("/prep/{application_id}/eligible")
def eligible_check(application_id: int) -> dict:
    """Quick existence + summary check used by the UI to decide GENERATE vs VIEW."""
    packet = _latest_packet_for(int(application_id))
    return {"ok": True, "data": {
        "has_packet": packet is not None,
        "packet_id": packet.get("id") if packet else None,
        "created_at": packet.get("created_at") if packet else None,
    }}


# ----------------------------------------------------------------------
# Practice mode
# ----------------------------------------------------------------------

def _pick_practice_questions(packet: dict, n: int) -> list[dict]:
    """Pick N questions evenly from behavioral/technical/scenario lists.

    Each entry is a dict ``{question_text, question_type}`` ready to be
    inserted as a turn.
    """
    pools = [
        ("behavioral", packet.get("behavioral_questions_json") or []),
        ("technical", packet.get("technical_questions_json") or []),
        ("scenario", packet.get("scenario_questions_json") or []),
    ]
    # Normalize each pool to question strings
    normalized: list[tuple[str, list[str]]] = []
    for kind, items in pools:
        qs: list[str] = []
        for it in items:
            if isinstance(it, dict):
                q = (it.get("question") or "").strip()
                if q:
                    qs.append(q)
            elif isinstance(it, str) and it.strip():
                qs.append(it.strip())
        normalized.append((kind, qs))

    out: list[dict] = []
    idx = 0
    cursors = {k: 0 for k, _ in normalized}
    while len(out) < n:
        if not normalized:
            break
        kind, qs = normalized[idx % len(normalized)]
        cur = cursors[kind]
        if cur < len(qs):
            out.append({"question_text": qs[cur], "question_type": kind})
            cursors[kind] = cur + 1
        else:
            # remove exhausted pool
            normalized = [(k, q) for k, q in normalized if cursors[k] < len(q)]
            if not normalized:
                break
            continue
        idx += 1
    return out[:n]


def _turn_to_dict(row: Any) -> dict:
    """Pretty-format a `interview_practice_turn` row for the UI."""
    d = row_to_dict(row) or {}
    # row_to_dict already json-decodes *_json columns
    return d


def _load_session(session_id: int) -> dict:
    row = get_conn().execute(
        "SELECT * FROM interview_practice_session WHERE id = ?",
        (int(session_id),),
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"practice session {session_id} not found")
    return row_to_dict(row) or {}


def _load_turns(session_id: int) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM interview_practice_turn WHERE session_id = ? "
        "ORDER BY turn_index ASC",
        (int(session_id),),
    ).fetchall()
    return [_turn_to_dict(r) for r in rows]


@router.post("/practice/{application_id}/start")
def start_practice(application_id: int, body: Optional[StartPracticeBody] = None) -> dict:
    n = int((body.question_count if body else 5) or 5)
    n = max(1, min(n, 20))
    packet = _latest_packet_for(int(application_id))
    if not packet:
        # Inline-generate so the user can START PRACTICE without a separate click.
        packet = _generate_prep_packet(int(application_id))

    picks = _pick_practice_questions(packet, n)
    if not picks:
        raise HTTPException(500, "no questions available in the packet")

    now = time.time()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO interview_practice_session
               (application_id, prep_packet_id, started_at, status,
                question_count, avg_score)
               VALUES (?, ?, ?, 'active', 0, NULL)""",
            (int(application_id), int(packet["id"]), now),
        )
        session_id = int(cur.lastrowid)

        # Pre-insert ALL turn rows so the UI can show progress. The first
        # one is `turn_index=0`; the rest have empty user_answer.
        for i, pick in enumerate(picks):
            conn.execute(
                """INSERT INTO interview_practice_turn
                   (session_id, turn_index, question_text, question_type,
                    user_answer, feedback_text, score, evidence_used_json,
                    llm_run_id, created_at)
                   VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?)""",
                (session_id, i, pick["question_text"], pick["question_type"], now),
            )

    audit("interview_practice_start", "application", int(application_id),
          session_id=session_id, question_count=len(picks))

    turns = _load_turns(session_id)
    first_turn = turns[0] if turns else None
    return {"ok": True, "data": {
        "session_id": session_id,
        "packet_id": int(packet["id"]),
        "total_questions": len(picks),
        "first_question": first_turn,
        "turns": turns,
    }}


_PRACTICE_SYSTEM = (
    "You grade a candidate's interview answer. Output strict JSON only — no "
    "markdown, no prose outside JSON.\n\n"
    "EVIDENCE PACK is the candidate's verifiable claims. If the answer "
    "references a claim in EVIDENCE PACK, mark its id in `evidence_used`. If "
    "the answer makes a claim NOT in EVIDENCE PACK (or contradicts it), flag "
    "that claim string in `unverified_claims`. Score 0-10 weighted on "
    "(a) clarity, (b) relevance to the question, (c) evidence-grounding, and "
    "(d) STAR structure (Situation/Task/Action/Result). "
    "Be honest — under-fabricated answers should still score well if they are "
    "structured and verifiable; over-fabricated answers should be flagged.\n\n"
    + _HONESTY_PARAGRAPH
)


def _build_practice_user_prompt(question: str, question_type: str,
                                user_answer: str,
                                evidence_pack: list[dict]) -> str:
    parts = [
        f"QUESTION TYPE: {question_type}",
        "QUESTION:",
        question,
        "",
        "CANDIDATE ANSWER:",
        user_answer.strip() or "(no answer provided)",
        "",
        "EVIDENCE PACK (the candidate's only verifiable claims — cite ids back):",
        json.dumps(evidence_pack, indent=2, default=str) if evidence_pack else "(no claims available)",
        "",
        "Return JSON with EXACTLY this schema:",
        "{",
        '  "score": <number 0-10>,',
        '  "strengths": ["...", "..."],',
        '  "improvements": ["...", "..."],',
        '  "evidence_used": [<claim ids the answer correctly leveraged>],',
        '  "unverified_claims": ["<verbatim claim text in the answer that is NOT in EVIDENCE PACK>"],',
        '  "rewrite_suggestion": "A STAR-structured rewrite (4-7 sentences) grounded only in EVIDENCE PACK."',
        "}",
    ]
    return "\n".join(parts)


def _deterministic_feedback(user_answer: str,
                            evidence_pack: list[dict]) -> dict:
    """Fallback when no LLM is available — heuristic, not creative."""
    answer = (user_answer or "").strip()
    if not answer:
        return {
            "score": 0,
            "strengths": [],
            "improvements": [
                "Answer was blank — every interview question deserves a real attempt, even a brief one.",
            ],
            "evidence_used": [],
            "unverified_claims": [],
            "rewrite_suggestion": "Anchor in a real project from your Vault. Walk through Situation → Task → Action → Result.",
        }
    words = answer.split()
    star_words = {"situation", "task", "action", "result", "challenge", "outcome"}
    hits = sum(1 for w in words if w.lower().strip(".,") in star_words)
    score = 5.0
    if len(words) > 80:
        score += 1.5
    if len(words) > 200:
        score += 1.0
    score += min(2.0, hits * 0.5)
    # evidence overlap: tally claim_text words present
    matched_ids: list[int] = []
    answer_lower = answer.lower()
    for c in evidence_pack:
        tokens = [t for t in (c.get("claim_text") or "").lower().split() if len(t) > 4]
        if tokens and sum(1 for t in tokens if t in answer_lower) >= 2:
            matched_ids.append(c["id"])
    if matched_ids:
        score += 1.0
    score = max(0.0, min(10.0, score))
    strengths = []
    improvements = []
    if hits >= 2:
        strengths.append("Answer uses STAR-style structuring words.")
    if matched_ids:
        strengths.append(f"Answer overlaps with {len(matched_ids)} Vault claim(s).")
    if not matched_ids:
        improvements.append("Tie the answer to a specific Vault claim with concrete details.")
    if len(words) < 60:
        improvements.append("Stretch the answer past 60 words so the interviewer can see structure.")
    return {
        "score": round(score, 1),
        "strengths": strengths,
        "improvements": improvements,
        "evidence_used": matched_ids,
        "unverified_claims": [],
        "rewrite_suggestion": (
            "Open with the SITUATION (project + scope), state the TASK (what you owned), "
            "walk through 2-3 ACTIONS you took, and close with a RESULT — quantify only "
            "what your Vault confirms."
        ),
    }


def _normalize_feedback(parsed: Any, fallback: dict) -> dict:
    if not isinstance(parsed, dict):
        return fallback
    try:
        score = float(parsed.get("score"))
        if score != score:  # NaN
            score = fallback["score"]
        score = max(0.0, min(10.0, score))
    except Exception:
        score = float(fallback["score"])
    strengths = parsed.get("strengths") or []
    if not isinstance(strengths, list):
        strengths = []
    improvements = parsed.get("improvements") or []
    if not isinstance(improvements, list):
        improvements = []
    evidence_used = parsed.get("evidence_used") or []
    clean_ids: list[int] = []
    if isinstance(evidence_used, list):
        for v in evidence_used:
            try:
                clean_ids.append(int(v))
            except Exception:
                continue
    unverified = parsed.get("unverified_claims") or []
    if not isinstance(unverified, list):
        unverified = []
    rewrite = parsed.get("rewrite_suggestion") or ""
    if not isinstance(rewrite, str):
        rewrite = str(rewrite or "")
    return {
        "score": round(score, 2),
        "strengths": [str(s) for s in strengths if str(s).strip()],
        "improvements": [str(s) for s in improvements if str(s).strip()],
        "evidence_used": clean_ids,
        "unverified_claims": [str(s) for s in unverified if str(s).strip()],
        "rewrite_suggestion": rewrite.strip(),
    }


@router.post("/practice/{session_id}/answer")
def submit_answer(session_id: int, body: SubmitAnswerBody) -> dict:
    session = _load_session(int(session_id))
    if (session.get("status") or "active") != "active":
        raise HTTPException(400, f"session {session_id} is not active "
                                  f"(status={session.get('status')})")
    application_id = int(session.get("application_id") or 0)

    # Load the addressed turn row
    conn = get_conn()
    turn_row = conn.execute(
        "SELECT * FROM interview_practice_turn WHERE session_id = ? AND turn_index = ?",
        (int(session_id), int(body.turn_index)),
    ).fetchone()
    if turn_row is None:
        raise HTTPException(404, f"turn {body.turn_index} not found in session {session_id}")
    turn = row_to_dict(turn_row) or {}

    # Evidence pack derived from the application's job posting
    app = _load_application(application_id)
    evidence_rows = _evidence_pack_for_job(app, top=20)
    evidence_pack = _evidence_for_prompt(evidence_rows)

    # Build prompts
    provider = get_llm()
    sys = _PRACTICE_SYSTEM
    user = _build_practice_user_prompt(
        question=turn.get("question_text") or "",
        question_type=turn.get("question_type") or "general",
        user_answer=body.user_answer or "",
        evidence_pack=evidence_pack,
    )

    fallback = _deterministic_feedback(body.user_answer or "", evidence_pack)
    feedback: dict = fallback
    llm_run_id = -1
    try:
        raw, llm_run_id = observed_complete(
            provider,
            "interview_practice",
            sys,
            user,
            max_tokens=1000,
            temperature=0.2,
            target_type="application",
            target_id=application_id,
        )
        if raw:
            parsed = extract_json(raw)
            feedback = _normalize_feedback(parsed, fallback)
    except Exception as exc:  # noqa: BLE001
        log.warning("interview_practice LLM call failed: %s", exc)
        feedback = fallback

    # Persist the turn update
    feedback_text = json.dumps({
        "strengths": feedback["strengths"],
        "improvements": feedback["improvements"],
        "unverified_claims": feedback["unverified_claims"],
        "rewrite_suggestion": feedback["rewrite_suggestion"],
    })
    with tx() as c:
        c.execute(
            """UPDATE interview_practice_turn
               SET user_answer = ?, feedback_text = ?, score = ?,
                   evidence_used_json = ?, llm_run_id = ?
               WHERE id = ?""",
            (
                body.user_answer or "",
                feedback_text,
                float(feedback["score"]),
                json.dumps(feedback["evidence_used"]),
                int(llm_run_id) if llm_run_id and llm_run_id > 0 else None,
                int(turn["id"]),
            ),
        )

    # Recount answered turns; recompute avg_score; check if done
    conn2 = get_conn()
    answered = conn2.execute(
        "SELECT score FROM interview_practice_turn WHERE session_id = ? "
        "AND user_answer IS NOT NULL AND user_answer != ''",
        (int(session_id),),
    ).fetchall()
    scores = [float(r["score"]) for r in answered if r["score"] is not None]
    avg = round(sum(scores) / len(scores), 2) if scores else None
    total = conn2.execute(
        "SELECT COUNT(*) AS n FROM interview_practice_turn WHERE session_id = ?",
        (int(session_id),),
    ).fetchone()["n"]
    next_row = conn2.execute(
        "SELECT * FROM interview_practice_turn WHERE session_id = ? "
        "AND (user_answer IS NULL OR user_answer = '') AND turn_index > ? "
        "ORDER BY turn_index ASC LIMIT 1",
        (int(session_id), int(body.turn_index)),
    ).fetchone()

    finished_at = None
    new_status = "active"
    if next_row is None:
        finished_at = time.time()
        new_status = "completed"

    with tx() as c:
        c.execute(
            """UPDATE interview_practice_session
               SET question_count = ?, avg_score = ?, status = ?, finished_at = ?
               WHERE id = ?""",
            (
                len(scores),
                avg,
                new_status,
                finished_at,
                int(session_id),
            ),
        )

    audit("interview_practice_answer", "application", application_id,
          session_id=int(session_id), turn_index=int(body.turn_index),
          score=feedback["score"], llm_run_id=llm_run_id,
          status_after=new_status)

    updated_turn = row_to_dict(get_conn().execute(
        "SELECT * FROM interview_practice_turn WHERE id = ?", (int(turn["id"]),)
    ).fetchone())
    next_turn = row_to_dict(next_row) if next_row else None
    session_after = _load_session(int(session_id))

    return {
        "ok": True,
        "data": {
            "turn": updated_turn,
            "feedback": feedback,
            "llm_run_id": int(llm_run_id) if llm_run_id and llm_run_id > 0 else None,
            "next_question": next_turn,
            "session": session_after,
            "total_questions": int(total),
        },
    }


@router.get("/practice/sessions/{application_id}")
def list_sessions(application_id: int) -> dict:
    rows = get_conn().execute(
        "SELECT * FROM interview_practice_session WHERE application_id = ? "
        "ORDER BY started_at DESC",
        (int(application_id),),
    ).fetchall()
    return {"ok": True, "data": [row_to_dict(r) for r in rows]}


@router.get("/practice/session/{session_id}")
def get_session(session_id: int) -> dict:
    session = _load_session(int(session_id))
    turns = _load_turns(int(session_id))
    return {"ok": True, "data": {"session": session, "turns": turns}}


# ----------------------------------------------------------------------
# Eligible applications surface
# ----------------------------------------------------------------------

_ELIGIBLE_STATUSES = ("prepared", "applied", "interview", "offer", "screened", "replied")


@router.get("/eligible")
def list_eligible_applications() -> dict:
    """List applications that make sense to prep for + indicate packet status."""
    conn = get_conn()
    placeholders = ",".join("?" for _ in _ELIGIBLE_STATUSES)
    rows = conn.execute(
        f"""SELECT a.id AS application_id, a.status, a.job_id, a.next_followup_at,
                   j.title AS job_title, j.company AS job_company,
                   j.location AS job_location,
                   (SELECT p.id FROM interview_prep_packet p
                     WHERE p.application_id = a.id
                     ORDER BY p.created_at DESC LIMIT 1) AS packet_id,
                   (SELECT p.created_at FROM interview_prep_packet p
                     WHERE p.application_id = a.id
                     ORDER BY p.created_at DESC LIMIT 1) AS packet_created_at,
                   (SELECT COUNT(*) FROM interview_practice_session s
                     WHERE s.application_id = a.id) AS practice_count
            FROM application a
            LEFT JOIN job_posting j ON j.id = a.job_id
            WHERE a.status IN ({placeholders})
            ORDER BY
              CASE a.status
                WHEN 'interview' THEN 0
                WHEN 'offer' THEN 1
                WHEN 'screened' THEN 2
                WHEN 'replied' THEN 3
                WHEN 'applied' THEN 4
                WHEN 'prepared' THEN 5
                ELSE 9 END,
              a.id DESC""",
        _ELIGIBLE_STATUSES,
    ).fetchall()
    return {"ok": True, "data": [dict(r) for r in rows]}
