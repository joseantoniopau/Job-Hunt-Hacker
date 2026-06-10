"""Tests for the vault re-ingest completeness backstop:
chrome stripping + deterministic position parsing + missing-role synthesis.

These guard against the real failure observed in production: an 11k-char
LinkedIn paste produced only 8 LLM claims — no role titles or dates for one
employer and an entire employer (early-career role) silently dropped.
"""
from __future__ import annotations

from backend.app.services.llm_vault_reingest import (
    _strip_profile_chrome,
    _supplement_missing_positions,
    parse_positions,
)

# Mimics a real LinkedIn paste: nav chrome, a grouped employer (multiple
# roles, blank lines between header and duration), simple-layout entries,
# location lines, descriptions.
LINKEDIN_FIXTURE = """\
Home
My Network
Jobs
Messaging
Notifications
Experience
AcmeCorp logo
AcmeCorp

8 yrs
Senior Widget Engineer
Full-time
Feb 2024 - Present · 2 yrs 5 mos
Remote · Remote
Widget Engineer
Full-time
Mar 2021 - Present · 5 yrs 4 mos
Built widget pipelines that processed millions of events per day in production.
Globex Security

2 yrs
Mountain View, California
Advanced Threat Analyst
Nov 2017 - Jun 2018 · 8 mos
In charge of analyzing all production traffic and fingerprinting all automated activity on web and native app environments.
Security Operations Analyst
Jul 2016 - Oct 2017 · 1 yr 4 mos
Network Engineer
DcinemaNOC
Nov 2013 - Jul 2016 · 2 yrs 9 mos
Worked in a 24/7 Network Operations Center environment where we monitored various equipment.
Office Manager
Behar & Behar P.A.
Dec 2010 - Jul 2013 · 2 yrs 8 mos
Show all 8 experiences
Education
"""


def test_strip_chrome_removes_nav_keeps_content():
    cleaned = _strip_profile_chrome(LINKEDIN_FIXTURE)
    assert "My Network" not in cleaned
    assert "Notifications" not in cleaned
    assert "AcmeCorp logo" not in cleaned
    assert "Show all 8 experiences" not in cleaned
    # Content survives
    assert "Senior Widget Engineer" in cleaned
    assert "Advanced Threat Analyst" in cleaned
    assert "fingerprinting all automated activity" in cleaned


def test_parse_positions_grouped_and_simple_layouts():
    got = parse_positions(_strip_profile_chrome(LINKEDIN_FIXTURE))
    triples = {(p["title"], p["employer"], p["date_start"]) for p in got}
    assert ("Senior Widget Engineer", "AcmeCorp", "Feb 2024") in triples
    assert ("Widget Engineer", "AcmeCorp", "Mar 2021") in triples
    assert ("Advanced Threat Analyst", "Globex Security", "Nov 2017") in triples
    assert ("Security Operations Analyst", "Globex Security", "Jul 2016") in triples
    assert ("Network Engineer", "DcinemaNOC", "Nov 2013") in triples
    assert ("Office Manager", "Behar & Behar P.A.", "Dec 2010") in triples
    assert len(got) == 6


def test_parse_positions_date_end_present_and_explicit():
    got = {p["title"]: p for p in parse_positions(_strip_profile_chrome(LINKEDIN_FIXTURE))}
    assert got["Senior Widget Engineer"]["date_end"].lower() == "present"
    assert got["Advanced Threat Analyst"]["date_end"] == "Jun 2018"


def test_parse_positions_empty_text():
    assert parse_positions("") == []
    assert parse_positions("just some prose without any dated positions") == []


def test_supplement_adds_only_missing_positions():
    positions = parse_positions(_strip_profile_chrome(LINKEDIN_FIXTURE))
    # LLM covered one position (by title mention) — the other five are missing.
    candidates = [{
        "source_id": 4,
        "claim_type": "role",
        "claim_text": "Worked as Senior Widget Engineer at AcmeCorp",
        "normalized_claim": "worked as senior widget engineer at acmecorp",
        "date_start": "Feb 2024", "date_end": "Present",
        "employer": "AcmeCorp",
    }]
    merged, added = _supplement_missing_positions(candidates, positions, 4)
    assert added == 5
    texts = [c["claim_text"] for c in merged]
    assert "Network Engineer at DcinemaNOC" in texts
    assert "Advanced Threat Analyst at Globex Security" in texts
    # The covered one was not duplicated.
    assert sum("Senior Widget Engineer" in t for t in texts) == 1
    # Synthesized rows carry dates + provenance span.
    syn = next(c for c in merged if c["claim_text"] == "Network Engineer at DcinemaNOC")
    assert syn["date_start"] == "Nov 2013" and syn["date_end"] == "Jul 2016"
    assert syn["_source_span"]


def test_supplement_noop_when_all_covered():
    positions = parse_positions(_strip_profile_chrome(LINKEDIN_FIXTURE))
    candidates = [{
        "source_id": 4, "claim_type": "role",
        "claim_text": f'{p["title"]} at {p["employer"]}',
        "normalized_claim": f'{p["title"]} at {p["employer"]}'.lower(),
        "date_start": p["date_start"], "date_end": p["date_end"],
        "employer": p["employer"],
    } for p in positions]
    merged, added = _supplement_missing_positions(candidates, positions, 4)
    assert added == 0
    assert len(merged) == len(positions)
