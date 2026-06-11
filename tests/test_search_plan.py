"""Tests for the shared search-plan builder and the remote country penalty.

Guards the production failure where an autopilot run searched job boards
for the user's EMPLOYER name ("eBay") and passed the home city to a remote
search (which starves board results to zero).
"""
from __future__ import annotations

import json
import time

import pytest

from backend.app.db import get_conn, init_db, tx
from backend.app.matching.location_parser import match_location, parse_location
from backend.app.services.search_plan import build_search_plan


@pytest.fixture()
def profile_with_employer_title():
    init_db()
    with tx() as c:
        c.execute("DELETE FROM career_claim")
        c.execute("DELETE FROM evidence_source")
        c.execute(
            "INSERT INTO evidence_source (source_type, title, raw_text, created_at) "
            "VALUES ('text', 'fixture', 'fixture', ?)", (time.time(),))
        sid = c.execute("SELECT max(id) FROM evidence_source").fetchone()[0]
        c.execute(
            "INSERT INTO career_claim (source_id, claim_type, claim_text, "
            "normalized_claim, employer, confidence, evidence_strength, "
            "user_verified, allowed_for_resume, contradiction_status, created_at) "
            "VALUES (?, 'role', 'Security Engineer at AcmeCorp', "
            "'security engineer at acmecorp', 'AcmeCorp', 0.9, 'strong', 1, 1, "
            "'none', ?)", (sid, time.time()))
        c.execute(
            "UPDATE user_profile SET target_titles = ?, target_keywords = ?, "
            "preferred_locations = ?, location = ?, remote_preference = ? "
            "WHERE id = 1",
            (json.dumps(["AcmeCorp", "Threat Hunter", "AI Engineer"]),
             json.dumps(["python", "security"]),
             json.dumps(["Miami Beach", "FL"]),
             "Miami Beach, FL", "remote"))
    yield
    with tx() as c:
        c.execute("DELETE FROM career_claim")
        c.execute("DELETE FROM evidence_source")


def test_employer_name_never_becomes_query(profile_with_employer_title):
    plan = build_search_plan()
    assert "AcmeCorp" not in plan.queries
    assert plan.dropped_employer_queries == ["AcmeCorp"]
    assert plan.queries == ["Threat Hunter", "AI Engineer"]


def test_remote_search_suppresses_home_city(profile_with_employer_title):
    plan = build_search_plan()
    assert plan.is_remote is True
    assert plan.location is None


def test_onsite_user_keeps_location(profile_with_employer_title):
    with tx() as c:
        c.execute("UPDATE user_profile SET remote_preference = 'onsite' WHERE id = 1")
    plan = build_search_plan()
    assert plan.is_remote is False
    assert plan.location == "Miami Beach"


def test_all_titles_employers_falls_back_to_keywords(profile_with_employer_title):
    with tx() as c:
        c.execute("UPDATE user_profile SET target_titles = ? WHERE id = 1",
                  (json.dumps(["AcmeCorp"]),))
    plan = build_search_plan()
    assert plan.queries == ["python security"]


# ---- remote country penalty ----

_US_PREFS = {"remote_preference": "remote",
             "preferred_locations": ["Miami Beach", "FL"],
             "location": "Miami Beach, FL"}


def test_remote_foreign_country_penalized():
    job = parse_location("Brazil")
    job["remote"] = True
    assert match_location(job, _US_PREFS) == 0.25


def test_remote_same_country_full_score():
    job = parse_location("Austin, TX, USA")
    job["remote"] = True
    assert match_location(job, _US_PREFS) == 1.0


def test_remote_unknown_country_unpenalized():
    job = parse_location("Remote")
    assert match_location(job, _US_PREFS) == 1.0
