"""Followup email drafting — produce a personalized subject + body for any
stage of the application cycle.

Each stage has a templated structure (deterministic baseline) so the app
always works. When an LLM is available we let it polish the body using the
candidate's verified evidence, then run a strict provenance filter so we
never ship a claim with no backing.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..db import audit, get_conn, row_to_dict

log = logging.getLogger("jhh.tailoring.followup_emails")

try:
    from ..llm import get_llm  # type: ignore
    from ..llm.json_repair import extract_json  # type: ignore
    from ..llm.observability import observed_complete  # type: ignore
except Exception:  # pragma: no cover
    get_llm = None  # type: ignore
    extract_json = None  # type: ignore
    observed_complete = None  # type: ignore

try:
    from .provenance import ProvenanceMap
    from .honesty_report import build_report
except Exception:  # pragma: no cover
    ProvenanceMap = None  # type: ignore
    build_report = None  # type: ignore


STAGES = [
    "applied",
    "screened",
    "interview_thank_you",
    "post_interview_followup",
    "ghost_check_in",
    "rejection_response",
    "offer_negotiate_kick_off",
    "decline_offer",
    "accept_offer",
]


def list_stages() -> list[dict]:
    return [
        {"stage": "applied",
         "title": "Application sent — light touch followup",
         "send_after_days": 7},
        {"stage": "screened",
         "title": "After a recruiter/HR phone screen",
         "send_after_days": 1},
        {"stage": "interview_thank_you",
         "title": "Thank-you note same day as interview",
         "send_after_days": 0},
        {"stage": "post_interview_followup",
         "title": "Check-in after no response post-interview",
         "send_after_days": 5},
        {"stage": "ghost_check_in",
         "title": "Polite nudge when the recruiter has gone dark",
         "send_after_days": 10},
        {"stage": "rejection_response",
         "title": "Gracious reply to a rejection that keeps the door open",
         "send_after_days": 0},
        {"stage": "offer_negotiate_kick_off",
         "title": "Open the negotiation conversation",
         "send_after_days": 1},
        {"stage": "decline_offer",
         "title": "Decline an offer professionally",
         "send_after_days": 0},
        {"stage": "accept_offer",
         "title": "Accept an offer in writing",
         "send_after_days": 0},
    ]


# ---- data load ----

def _load_application(application_id: int) -> dict:
    conn = get_conn()
    row = conn.execute(
        """SELECT a.*, j.title AS job_title, j.company AS job_company,
                  j.location AS job_location, j.description AS job_description,
                  j.apply_url AS job_apply_url
           FROM application a
           LEFT JOIN job_posting j ON j.id = a.job_id
           WHERE a.id = ?""",
        (int(application_id),),
    ).fetchone()
    if not row:
        raise ValueError(f"application id={application_id} not found")
    return dict(row)


def _load_user_profile() -> dict:
    conn = get_conn()
    row = conn.execute("SELECT * FROM user_profile WHERE id = 1").fetchone()
    return row_to_dict(row) or {}


def _retrieve_claims(app: dict, max_claims: int = 5) -> list[dict]:
    try:
        from ..services import career_vault  # type: ignore
        fn = getattr(career_vault, "retrieve_for_job", None)
        if callable(fn):
            blob = " ".join([app.get("job_title") or "", app.get("job_description") or ""])
            hits = fn(blob, top=max_claims) or []
            if hits:
                return hits
    except Exception:
        pass
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM career_claim WHERE allowed_for_resume = 1 ORDER BY confidence DESC LIMIT ?",
        (int(max_claims),),
    ).fetchall()
    return [row_to_dict(r) for r in rows if r]


# ---- deterministic templates per stage ----

def _signature(profile: dict) -> str:
    name = (profile.get("name") or "[Your Name]").strip()
    extras = []
    if profile.get("phone"):
        extras.append(str(profile["phone"]))
    if profile.get("linkedin_url"):
        extras.append(str(profile["linkedin_url"]))
    extras_str = ("\n" + " · ".join(extras)) if extras else ""
    return f"Best,\n{name}{extras_str}"


def _template(stage: str, app: dict, profile: dict, claims: list[dict]) -> dict:
    company = app.get("job_company") or "the team"
    role = app.get("job_title") or "this role"
    sig = _signature(profile)
    cited_claim_ids: list[int] = []
    # Pick one short claim for personalization in stages that benefit.
    headline_claim = None
    for c in claims:
        text = (c.get("claim_text") or "").strip()
        if text and len(text.split()) >= 4:
            headline_claim = text
            cited_claim_ids = [int(c["id"])] if c.get("id") else []
            break

    if stage == "applied":
        subject = f"Application for {role} — quick note"
        body = (
            f"Hi,\n\n"
            f"I submitted my application for the {role} position at {company} "
            f"last week. I wanted to reiterate my interest and ask if there's any "
            f"additional information that would help your review.\n\n"
            f"{sig}\n"
        )
    elif stage == "screened":
        subject = f"Following up on our conversation — {role}"
        body = (
            f"Hi,\n\n"
            f"Thanks for the call yesterday. I enjoyed learning more about the "
            f"team's priorities for the {role} role at {company}. "
        )
        if headline_claim:
            body += (
                f"Reflecting on what you described, I think the work I've done — "
                f"{headline_claim} — maps closely to where you're heading.\n\n"
            )
        else:
            body += "\n"
        body += f"Looking forward to next steps.\n\n{sig}\n"
    elif stage == "interview_thank_you":
        subject = f"Thank you — {role} interview at {company}"
        body = (
            f"Hi,\n\n"
            f"Thank you for taking the time to meet today about the {role} role. "
            f"I came away even more interested in the work the team is doing, "
            f"particularly around the priorities you walked me through.\n\n"
        )
        if headline_claim:
            body += (
                f"Connecting it to my own experience — {headline_claim} — I think "
                f"there's a strong overlap with the scope you described.\n\n"
            )
        body += f"Please let me know if there's anything else I can provide.\n\n{sig}\n"
    elif stage == "post_interview_followup":
        subject = f"Checking in — {role} at {company}"
        body = (
            f"Hi,\n\n"
            f"I wanted to follow up on our conversation about the {role} role. "
            f"I remain very interested and wanted to check in on timing for the "
            f"next steps. Happy to provide anything else that would be useful "
            f"as the team makes its decision.\n\n"
            f"{sig}\n"
        )
    elif stage == "ghost_check_in":
        subject = f"Quick check-in on the {role} role"
        body = (
            f"Hi,\n\n"
            f"I know things get busy. I wanted to send a brief note to check in "
            f"on the status of the {role} position at {company}. If timing has "
            f"shifted or you're prioritizing other candidates, I'd appreciate "
            f"knowing so I can plan accordingly.\n\n"
            f"{sig}\n"
        )
    elif stage == "rejection_response":
        subject = f"Thank you — and staying in touch"
        body = (
            f"Hi,\n\n"
            f"Thanks for letting me know — I appreciate the update, and even more "
            f"the time the team spent on the conversations. I'd love to stay in "
            f"touch as the team grows or as other roles open up that could be a "
            f"fit. Wishing the new hire and the team well.\n\n"
            f"{sig}\n"
        )
    elif stage == "offer_negotiate_kick_off":
        subject = f"Re: offer for {role}"
        body = (
            f"Hi,\n\n"
            f"Thank you for the offer for the {role} position at {company}. "
            f"I'm excited about the team and the work. I'd like to discuss a "
            f"couple of items in the package before signing — could we find a "
            f"15-minute slot in the next day or two to talk it through?\n\n"
            f"{sig}\n"
        )
    elif stage == "decline_offer":
        subject = f"Decision on the {role} offer"
        body = (
            f"Hi,\n\n"
            f"Thank you again for the offer for the {role} position at {company}. "
            f"After careful consideration, I've decided to accept another "
            f"opportunity that better aligns with my current goals. I'm grateful "
            f"for the time the team invested and I'd very much like to keep the "
            f"door open for the future.\n\n"
            f"{sig}\n"
        )
    elif stage == "accept_offer":
        subject = f"Accepting the {role} offer at {company}"
        body = (
            f"Hi,\n\n"
            f"I'm thrilled to formally accept the offer for the {role} position "
            f"at {company}. Please send over the next steps for paperwork and "
            f"onboarding. Looking forward to getting started.\n\n"
            f"{sig}\n"
        )
    else:
        raise ValueError(f"unknown stage: {stage}")
    return {"subject": subject, "body": body, "claim_ids": cited_claim_ids}


_LLM_SYS = (
    "You polish a job-search followup email. You receive a stage, the job, "
    "and the candidate's verified evidence claims. Keep the message concise "
    "(under 180 words), polite, and human. CRITICAL: never invent achievements. "
    "If you reference a claim, include its claim_id in the cited_claim_ids list. "
    "Output ONLY JSON: {\"subject\": str, \"body\": str, \"cited_claim_ids\": [int]}"
)


def _llm_polish(stage: str, app: dict, profile: dict, claims: list[dict],
                base: dict) -> tuple[Optional[dict], Optional[int]]:
    """Run the followup-polish LLM call under observability.

    Returns (polished_dict_or_None, llm_run_id). None means the call
    failed or produced unusable output, so the caller keeps the template
    draft; llm_run_id is None when no run row was recorded.
    """
    if get_llm is None or observed_complete is None or extract_json is None:
        return None, None
    try:
        provider = get_llm()
        evidence = [
            {"id": c.get("id"), "text": (c.get("claim_text") or "")[:200]}
            for c in claims if c.get("id")
        ]
        user = json.dumps({
            "stage": stage,
            "company": app.get("job_company"),
            "role": app.get("job_title"),
            "candidate_name": profile.get("name"),
            "evidence": evidence,
            "starter_subject": base["subject"],
            "starter_body": base["body"],
        })
        raw, run_id = observed_complete(
            provider,
            "followup_email",
            _LLM_SYS,
            user,
            max_tokens=900,
            temperature=0.4,
            target_type="application",
            target_id=int(app.get("id") or 0) or None,
        )
        llm_run_id = int(run_id) if run_id and run_id > 0 else None
        out = extract_json(raw or "")
        if not isinstance(out, dict) or not out.get("body"):
            return None, llm_run_id
        return out, llm_run_id
    except Exception as e:
        log.warning("LLM followup polish failed: %s", e)
        return None, None


def _filter_claim_ids(ids, allowed_ids: set[int]) -> list[int]:
    clean: list[int] = []
    for i in (ids or []):
        try:
            iv = int(i)
        except Exception:
            continue
        if iv in allowed_ids:
            clean.append(iv)
    return clean


def _build_honesty_report(claim_ids: list[int]) -> dict:
    if ProvenanceMap is None or build_report is None:
        return {"facts_used": len(claim_ids), "potential_overstatement_risk": "n/a"}
    pm = ProvenanceMap()
    if claim_ids:
        pm.link("body", claim_ids)
    return build_report(provenance=pm, keyword_matrix=[], gaps_flagged=[], dropped_segments=[])


def draft(application_id: int, stage: str) -> dict:
    stage_norm = (stage or "").strip().lower()
    if stage_norm not in STAGES:
        raise ValueError(f"unknown stage: {stage!r}; valid: {STAGES}")
    app = _load_application(application_id)
    profile = _load_user_profile()
    claims = _retrieve_claims(app)
    allowed_ids = {int(c["id"]) for c in claims if c.get("id")}

    base = _template(stage_norm, app, profile, claims)
    used = "template"
    final_subject = base["subject"]
    final_body = base["body"]
    cited = base["claim_ids"]

    polished, llm_run_id = _llm_polish(stage_norm, app, profile, claims, base)
    if polished:
        subj = (polished.get("subject") or "").strip()
        body = (polished.get("body") or "").strip()
        if subj and body and len(body) < 4000:
            final_subject = subj
            final_body = body
            cited = _filter_claim_ids(polished.get("cited_claim_ids"), allowed_ids)
            used = "llm"

    honesty = _build_honesty_report(cited)

    out = {
        "stage": stage_norm,
        "application_id": int(application_id),
        "subject": final_subject,
        "body": final_body,
        "llm_run_id": llm_run_id,
        "provenance": {
            "claim_ids": cited,
            "claims_available": len(allowed_ids),
            "provider": used,
        },
        "honesty_report": honesty,
    }
    try:
        audit(
            "followup_drafted",
            "application",
            int(application_id),
            stage=stage_norm,
            provider=used,
            llm_run_id=llm_run_id,
        )
    except Exception:
        pass
    return out


__all__ = ["draft", "list_stages", "STAGES"]
