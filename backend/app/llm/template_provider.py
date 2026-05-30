"""Template provider — deterministic output assembled from evidence.

Used when no API key is configured. Honest by construction: every output
segment is built from concrete evidence strings passed in via the prompt;
it cannot invent facts.

The trick: the caller passes the JSON evidence in the `user` message. This
provider extracts that JSON and assembles the requested output using
straightforward templates. If JSON output is requested, it returns an empty
dict (the caller's caller is responsible for falling back to structured
deterministic logic, not the LLM).
"""
from __future__ import annotations

import json
import re

from .base import LLMProvider
from .json_repair import extract_json


class TemplateProvider(LLMProvider):
    name = "template"

    def complete(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.3) -> str:
        # Try to extract any JSON evidence the caller embedded.
        ev = extract_json(user) or {}
        kind = self._detect_kind(system + " " + user)

        if kind == "resume_bullets" and isinstance(ev, dict):
            bullets = []
            for c in (ev.get("claims") or [])[:6]:
                text = (c.get("claim_text") or "").strip()
                if text:
                    bullets.append("- " + _polish(text))
            return "\n".join(bullets) or "- (no evidence supplied)"

        if kind == "cover_letter" and isinstance(ev, dict):
            company = ev.get("company") or "the team"
            role = ev.get("role") or "this role"
            highlights = [c.get("claim_text", "") for c in (ev.get("claims") or [])[:3]]
            highlights = [h for h in highlights if h]
            body = "\n\n".join([
                f"Dear Hiring Manager,",
                f"I'm writing to express interest in the {role} position at {company}.",
                "Relevant background from my career:",
                "\n".join("- " + _polish(h) for h in highlights) or "- (evidence pending)",
                "I'd welcome the opportunity to discuss how this fits your team's needs.",
                "Thank you for your consideration,\n[Your Name]",
            ])
            return body

        if kind == "recruiter_message" and isinstance(ev, dict):
            role = ev.get("role") or "the open role"
            return (f"Hi — I came across the {role} listing and my background looks like a strong fit "
                    "based on my recent work. Open to a brief intro call this week? Thanks.")

        if kind == "interview_prep" and isinstance(ev, dict):
            return self._interview_prep(ev)

        # Generic JSON-extraction tasks: return the JSON we found if any.
        if isinstance(ev, (dict, list)):
            return json.dumps(ev)
        return ""

    # ---- helpers ----

    @staticmethod
    def _detect_kind(blob: str) -> str:
        b = blob.lower()
        if "cover letter" in b: return "cover_letter"
        if "recruiter message" in b: return "recruiter_message"
        if "interview prep" in b or "interview talking" in b: return "interview_prep"
        if "resume bullet" in b or "tailored resume" in b: return "resume_bullets"
        return "generic"

    @staticmethod
    def _interview_prep(ev: dict) -> str:
        role = ev.get("role") or "the role"
        out = [f"Interview talking points — {role}", ""]
        for c in (ev.get("claims") or [])[:5]:
            out.append("- " + _polish(c.get("claim_text", "")))
        out.append("")
        out.append("Likely questions:")
        out += [
            "- Walk me through your most relevant project for this role.",
            "- What's a measurable outcome you're proud of?",
            "- How do you make tradeoffs under deadline pressure?",
            "- What gap do you have, and how would you close it in 90 days?",
        ]
        return "\n".join(out)


def _polish(s: str) -> str:
    s = (s or "").strip().rstrip(".")
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    # Capitalize first letter, normalize verbs lightly
    return s[0].upper() + s[1:] + "."
