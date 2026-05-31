"""Single-bullet resume rewrite ("rewrite this bullet") with provenance.

Surface API:

  * ``iterate_bullet(resume_id, section_index, item_index, instruction)``
      Returns a proposed rewrite for one bullet, never persists. Output
      shape::

          {
            "ok": bool,
            "resume_id": int,
            "section_index": int, "item_index": int,
            "original": {"text": str, "evidence_ids": [int]},
            "rewritten": {"text": str, "evidence_ids": [int]},
            "accepted": False,
            "honesty_report": {...},
            "detail": str | None,    # only when ok=False
          }

  * ``accept_iteration(resume_id, section_index, item_index, new_text,
                        new_evidence_ids)``
      Writes the change back into the tailored_resume row's structured
      JSON, re-renders markdown + plain text, and bumps `updated_at`.
      Returns the persisted resume bundle.

The LLM call is deliberately scoped to a single bullet: we send the
original text, its evidence_ids, the evidence dicts they refer to, and
the user's natural-language instruction. The model returns ``{text,
evidence_ids}`` and we run it through ``guardrails.validate_provenance``
against the bullet's *original* allowed evidence id set — so a rewrite
cannot bring in new claims out of nowhere.
"""
from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any

from ..db import audit, get_conn, row_to_dict, tx
from ..llm import get_llm
from ..llm import guardrails
from ..utils.exporters import to_markdown, to_plain_text

log = logging.getLogger("jhh.tailoring.resume_iteration")


# ---------- helpers ----------

def _load_tailored(resume_id: int) -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM tailored_resume WHERE id = ?", (int(resume_id),)
    ).fetchone()
    if row is None:
        raise ValueError(f"tailored_resume id={resume_id} not found")
    return row_to_dict(row) or {}


def _structured_from_markdown_fallback(markdown: str) -> dict:
    """Last-ditch reconstruction when the row lost its provenance_json.

    We can't recover original evidence_ids from markdown alone, so the
    structured shape we return has empty evidence_ids — which means the
    guardrails layer will drop everything. This is intentional: a
    rewrite without provenance is dishonest, so we'd rather fail loudly
    than guess. Callers should treat this as an error.
    """
    return {"header": {}, "summary": "", "sections": []}


def _load_structured(resume_row: dict) -> dict:
    """Pull the structured resume shape out of the row.

    Tailored resumes today persist provenance_json as ``{segments: {...}}``
    and the markdown/plain_text strings separately. We never persisted the
    full structured tree, so we rebuild it from the markdown when needed.

    Strategy: walk the existing `markdown` line-by-line, then merge in
    the provenance segments so each `sections[i].items[j]` carries the
    original `evidence_ids` from provenance.
    """
    md = (resume_row.get("markdown") or "").strip()
    prov = resume_row.get("provenance_json") or {}
    # provenance_json may already be a dict thanks to row_to_dict's _json auto-decode
    if isinstance(prov, str):
        try:
            prov = json.loads(prov)
        except Exception:
            prov = {}
    if not isinstance(prov, dict):
        prov = {}
    segments = prov.get("segments") or {}

    sections = _parse_markdown_into_sections(md)
    # Now overlay evidence_ids from the segments map by segment_id position.
    for s_idx, sec in enumerate(sections):
        for i_idx, item in enumerate(sec.get("items") or []):
            seg_id = f"sections[{s_idx}].items[{i_idx}]"
            ids = segments.get(seg_id) or []
            if isinstance(ids, list):
                item["evidence_ids"] = [int(x) for x in ids if isinstance(x, (int, float, str))
                                        and str(x).lstrip("-").isdigit()]
            else:
                item["evidence_ids"] = []

    # Header + summary best-effort from markdown
    header = _parse_header_from_markdown(md)
    summary = _parse_summary_from_markdown(md)

    return {"header": header, "summary": summary, "sections": sections}


def _parse_header_from_markdown(md: str) -> dict:
    lines = [l.rstrip() for l in md.splitlines()]
    header: dict = {}
    for line in lines[:8]:
        s = line.strip()
        if s.startswith("# "):
            header["name"] = s[2:].strip()
            continue
        if not s or s.startswith("#") or s.startswith("-") or s.startswith("*"):
            continue
        # Contact line like "email | phone | location"
        if "|" in s and "@" in s and "links" not in header:
            parts = [p.strip() for p in s.split("|") if p.strip()]
            for p in parts:
                if "@" in p and "email" not in header:
                    header["email"] = p
                elif any(c.isdigit() for c in p) and "phone" not in header:
                    header["phone"] = p
                else:
                    header.setdefault("location", p)
    return header


def _parse_summary_from_markdown(md: str) -> str:
    # crude: look for a "## Summary" block and grab its body until next ##
    lines = md.splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        s = line.rstrip()
        if s.lower().startswith("## summary"):
            in_block = True
            continue
        if in_block:
            if s.startswith("## ") or s.startswith("# "):
                break
            if s.strip():
                out.append(s.strip())
    return " ".join(out).strip()


def _parse_markdown_into_sections(md: str) -> list[dict]:
    """Split markdown headings (## Title) into sections with bullet items."""
    sections: list[dict] = []
    current: dict | None = None
    skip_until_blank = False
    for raw in md.splitlines():
        s = raw.rstrip()
        if s.startswith("## "):
            title = s[3:].strip()
            # We deliberately skip "## Summary" — it goes into structured["summary"]
            if title.lower() == "summary":
                current = None
                skip_until_blank = True
                continue
            skip_until_blank = False
            current = {"title": title, "items": []}
            sections.append(current)
            continue
        if current is None:
            continue
        if s.startswith("- ") or s.startswith("* "):
            text = s[2:].strip()
            if text:
                current["items"].append({"text": text, "evidence_ids": []})
    return sections


def _persist_structured(resume_id: int, structured: dict) -> tuple[str, str]:
    """Re-render markdown + plain_text from the structured shape and write
    them + the rebuilt provenance_json back to the row. Returns (md, txt).
    """
    md = to_markdown(structured)
    txt = to_plain_text(structured)

    # Rebuild provenance_json from the structured sections so future
    # iterations can recover evidence_ids round-trip.
    prov: dict = {"segments": {}}
    for s_idx, sec in enumerate(structured.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        for i_idx, item in enumerate(sec.get("items") or []):
            if not isinstance(item, dict):
                continue
            seg_id = f"sections[{s_idx}].items[{i_idx}]"
            prov["segments"][seg_id] = list(item.get("evidence_ids") or [])
    all_ids: set[int] = set()
    for ids in prov["segments"].values():
        for i in ids:
            try:
                all_ids.add(int(i))
            except Exception:
                pass
    prov["distinct_evidence_ids"] = sorted(all_ids)
    prov["coverage"] = {
        "n_segments": len(prov["segments"]),
        "n_with_evidence": sum(1 for v in prov["segments"].values() if v),
        "n_without": sum(1 for v in prov["segments"].values() if not v),
    }

    with tx() as conn:
        conn.execute(
            "UPDATE tailored_resume SET markdown = ?, plain_text = ?, provenance_json = ? WHERE id = ?",
            (md, txt, json.dumps(prov, default=str), int(resume_id)),
        )
    return md, txt


def _load_evidence_dicts(evidence_ids: list[int]) -> list[dict]:
    if not evidence_ids:
        return []
    conn = get_conn()
    placeholders = ",".join("?" for _ in evidence_ids)
    rows = conn.execute(
        f"SELECT * FROM career_claim WHERE id IN ({placeholders})",
        tuple(int(i) for i in evidence_ids),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = row_to_dict(r) or {}
        out.append({
            "id": int(d.get("id")),
            "claim_type": d.get("claim_type") or "",
            "claim_text": (d.get("claim_text") or d.get("normalized_claim") or "")[:500],
            "employer": d.get("employer") or "",
            "skill": d.get("skill") or "",
            "tool": d.get("tool") or "",
            "date_start": d.get("date_start") or "",
            "date_end": d.get("date_end") or "",
        })
    return out


# ---------- LLM prompt ----------

_REWRITE_SYS = (
    "You rewrite a single resume bullet under strict honesty rules.\n\n"
    "ABSOLUTE RULES:\n"
    "1. Do not invent any new fact, metric, employer, tool, skill, or date "
    "that does not appear in the supplied evidence.\n"
    "2. Preserve all of the original bullet's evidence_ids — every claim "
    "the original bullet rested on must still be grounded.\n"
    "3. You may rephrase, tighten, sharpen verbs, or restructure for ATS — "
    "as long as the meaning stays grounded in the supplied evidence.\n"
    "4. Return ONLY valid JSON: {\"text\": str, \"evidence_ids\": [int]}\n"
    "5. evidence_ids MUST be a subset of the original evidence_ids — never "
    "add an id that wasn't in the original.\n"
)


def _build_user_prompt(original_text: str, original_evidence_ids: list[int],
                       evidence_dicts: list[dict], instruction: str) -> str:
    payload = {
        "original_bullet": {
            "text": original_text,
            "evidence_ids": original_evidence_ids,
        },
        "evidence": evidence_dicts,
        "user_instruction": instruction or "Tighten the bullet for clarity.",
    }
    return (
        "Rewrite the resume bullet according to the user's instruction. "
        "Return JSON only.\n\n"
        + json.dumps(payload, indent=2, default=str)
    )


def _deterministic_rewrite(original_text: str,
                           original_evidence_ids: list[int],
                           instruction: str) -> dict:
    """Fallback when the LLM returns nothing usable.

    We don't try to be clever — we just tag the original bullet with the
    instruction so the user sees that nothing was changed and can iterate
    again. evidence_ids are preserved.
    """
    text = (original_text or "").strip()
    if instruction:
        # Light surface-level cleanup hint based on the instruction keywords.
        instr = instruction.lower()
        if "shorter" in instr or "tighten" in instr or "concise" in instr:
            # crude: trim parentheticals
            import re as _re
            text = _re.sub(r"\s*\([^)]*\)", "", text).strip()
        if "verb" in instr or "stronger" in instr:
            # Capitalize first word; trust the original verb
            if text:
                text = text[0].upper() + text[1:]
    return {"text": text, "evidence_ids": list(original_evidence_ids)}


# ---------- public API ----------

def iterate_bullet(resume_id: int, section_index: int, item_index: int,
                   instruction: str) -> dict:
    """Generate a proposed rewrite for a single bullet (no persistence)."""
    try:
        row = _load_tailored(resume_id)
    except ValueError as e:
        return {
            "ok": False, "detail": str(e),
            "resume_id": int(resume_id),
            "section_index": int(section_index), "item_index": int(item_index),
        }

    structured = _load_structured(row)
    sections = structured.get("sections") or []
    try:
        section = sections[int(section_index)]
        item = (section.get("items") or [])[int(item_index)]
    except (IndexError, TypeError, KeyError):
        return {
            "ok": False,
            "detail": f"bullet sections[{section_index}].items[{item_index}] not found",
            "resume_id": int(resume_id),
            "section_index": int(section_index), "item_index": int(item_index),
        }

    original_text = (item.get("text") or "").strip()
    original_evidence_ids = [int(x) for x in (item.get("evidence_ids") or [])
                             if isinstance(x, (int, float, str)) and str(x).lstrip("-").isdigit()]
    if not original_evidence_ids:
        return {
            "ok": False,
            "detail": "bullet has no original evidence_ids — cannot rewrite without provenance",
            "resume_id": int(resume_id),
            "section_index": int(section_index), "item_index": int(item_index),
            "original": {"text": original_text, "evidence_ids": []},
            "rewritten": {"text": "", "evidence_ids": []},
            "accepted": False,
        }

    allowed: set[int] = set(original_evidence_ids)
    ev_dicts = _load_evidence_dicts(original_evidence_ids)

    llm = get_llm()
    user_prompt = _build_user_prompt(original_text, original_evidence_ids,
                                     ev_dicts, instruction or "")
    rewritten: dict = {}
    try:
        rewritten = llm.complete_json(_REWRITE_SYS, user_prompt, max_tokens=800) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("LLM bullet rewrite failed: %s", e)
        rewritten = {}

    if not isinstance(rewritten, dict) or not (rewritten.get("text") or "").strip():
        rewritten = _deterministic_rewrite(original_text, original_evidence_ids, instruction or "")

    # Validate provenance: force evidence_ids to be a subset of allowed.
    # We use the same guardrails.validate_provenance API by wrapping into the
    # "single segment" shape it understands.
    seg = {"text": rewritten.get("text") or "",
           "evidence_ids": rewritten.get("evidence_ids") or []}
    cleaned = guardrails.validate_provenance(copy.deepcopy(seg), allowed)
    rewritten_clean = {
        "text": (cleaned.get("text") or "").strip(),
        "evidence_ids": cleaned.get("evidence_ids") or [],
    }
    dropped = (cleaned.get("honesty_report") or {}).get("dropped_segments") or []

    # If guardrails dropped everything, fall back to the original verbatim —
    # we never return a bullet with no provenance.
    if not rewritten_clean["evidence_ids"]:
        rewritten_clean = {
            "text": original_text,
            "evidence_ids": list(original_evidence_ids),
        }

    audit(
        "resume_iteration_proposed",
        "tailored_resume",
        int(resume_id),
        section_index=int(section_index),
        item_index=int(item_index),
        instruction=(instruction or "")[:240],
        original_len=len(original_text),
        rewritten_len=len(rewritten_clean["text"]),
        dropped_count=len(dropped),
        provider=getattr(llm, "name", "unknown"),
    )

    return {
        "ok": True,
        "resume_id": int(resume_id),
        "section_index": int(section_index),
        "item_index": int(item_index),
        "original": {
            "text": original_text,
            "evidence_ids": original_evidence_ids,
        },
        "rewritten": rewritten_clean,
        "accepted": False,
        "honesty_report": {
            "allowed_evidence_ids": sorted(allowed),
            "dropped_segments": dropped,
        },
    }


def accept_iteration(resume_id: int, section_index: int, item_index: int,
                     new_text: str, new_evidence_ids: list[int]) -> dict:
    """Persist a proposed rewrite. Re-renders markdown + plain_text.

    Enforces the same provenance rule as ``iterate_bullet``: the accepted
    evidence_ids must be a subset of the bullet's original evidence_ids.
    """
    try:
        row = _load_tailored(resume_id)
    except ValueError as e:
        return {"ok": False, "detail": str(e), "resume_id": int(resume_id)}

    structured = _load_structured(row)
    sections = structured.get("sections") or []
    try:
        section = sections[int(section_index)]
        item = (section.get("items") or [])[int(item_index)]
    except (IndexError, TypeError, KeyError):
        return {
            "ok": False,
            "detail": f"bullet sections[{section_index}].items[{item_index}] not found",
            "resume_id": int(resume_id),
        }

    original_text = (item.get("text") or "").strip()
    original_evidence_ids = [int(x) for x in (item.get("evidence_ids") or [])
                             if isinstance(x, (int, float, str)) and str(x).lstrip("-").isdigit()]
    if not original_evidence_ids:
        return {
            "ok": False,
            "detail": "bullet has no original evidence_ids — cannot accept rewrite",
            "resume_id": int(resume_id),
        }
    allowed = set(original_evidence_ids)

    text = (new_text or "").strip()
    if not text:
        return {"ok": False, "detail": "new_text is empty", "resume_id": int(resume_id)}

    raw_ids: list[int] = []
    for x in (new_evidence_ids or []):
        try:
            raw_ids.append(int(x))
        except Exception:
            continue
    clean_ids = [i for i in raw_ids if i in allowed]
    if not clean_ids:
        return {
            "ok": False,
            "detail": "new_evidence_ids must be a subset of the original bullet's evidence_ids",
            "resume_id": int(resume_id),
            "allowed": sorted(allowed),
            "rejected": sorted(set(raw_ids) - allowed),
        }

    # Mutate the structured tree and persist
    item["text"] = text
    item["evidence_ids"] = clean_ids

    md, txt = _persist_structured(int(resume_id), structured)

    audit(
        "resume_iteration_accepted",
        "tailored_resume",
        int(resume_id),
        section_index=int(section_index),
        item_index=int(item_index),
        original_text=original_text[:240],
        new_text=text[:240],
        evidence_ids=clean_ids,
    )

    return {
        "ok": True,
        "resume_id": int(resume_id),
        "section_index": int(section_index),
        "item_index": int(item_index),
        "original": {"text": original_text, "evidence_ids": original_evidence_ids},
        "accepted": {"text": text, "evidence_ids": clean_ids},
        "markdown": md,
        "plain_text": txt,
    }


__all__ = ["iterate_bullet", "accept_iteration"]
