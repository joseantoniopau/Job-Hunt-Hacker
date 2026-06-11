"""Tests for the referral finder (task G5):

- GET /api/referrals ranking (current > past > fuzzy > mention) + tiebreaks
- fuzzy company matching (case/punct-insensitive, whole-token substring)
- mention/alumni signal from notes and role text
- GET /api/referrals/companies-with-connections (open jobs only)
- GET /api/referrals/job-flags (has_referral map, bad input handling)
- suggested_message grounding: real stored names only, no placeholders
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from backend.app.db import get_conn, init_db, tx
from backend.app.main import app
from backend.app.services import networking

client = TestClient(app)


# ----------------------- helpers -----------------------

def _make_job(
    title: str = "Test Job",
    company: str | None = "TestCo",
    status: str = "new",
) -> int:
    init_db()
    h = f"ref-hash-{title}-{company}-{time.time_ns()}"
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO job_posting (source, title, company, status, hash, discovered_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("test", title, company, status, h, time.time()),
        )
        return int(cur.lastrowid)


def _set_profile_name(name: str | None) -> None:
    with tx() as conn:
        conn.execute("UPDATE user_profile SET name = ? WHERE id = 1", (name,))


@pytest.fixture(autouse=True)
def _clean_state():
    """Each test gets an empty rolodex, no jobs, and an unnamed profile."""
    init_db()
    with tx() as conn:
        conn.execute("DELETE FROM connection_company")
        conn.execute("DELETE FROM connection")
        conn.execute("DELETE FROM job_posting")
        conn.execute("UPDATE user_profile SET name = NULL WHERE id = 1")
    yield


# ----------------------- ranking -----------------------

def test_ranking_order_current_past_fuzzy_mention():
    cur_id = networking.add_connection("Alice Current", company="Stripe")
    past_id = networking.add_connection(
        "Bob Past", company="OtherCo",
        additional_companies=[{"company": "Stripe", "role": "SWE"}],
    )
    fuzzy_id = networking.add_connection("Cara Fuzzy", company="Stripe, Inc.")
    mention_id = networking.add_connection(
        "Dan Mention", company="Elsewhere",
        notes="Met at a hackathon; knows the Stripe payments team well.",
    )
    networking.add_connection("Eve Unrelated", company="Google")

    resp = client.get("/api/referrals", params={"company": "Stripe"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["company"] == "Stripe"
    assert body["count"] == 4

    kinds = [r["match_kind"] for r in body["data"]]
    ids = [r["connection"]["id"] for r in body["data"]]
    assert kinds == ["current", "past", "fuzzy", "mention"]
    assert ids == [cur_id, past_id, fuzzy_id, mention_id]
    names = [r["connection"]["name"] for r in body["data"]]
    assert "Eve Unrelated" not in names

    # Every result carries the contract fields.
    for r in body["data"]:
        assert set(r) >= {"connection", "match_kind", "last_contacted_at", "suggested_message"}
        assert r["last_contacted_at"] == r["connection"]["last_contacted_at"]


def test_ranking_tiebreak_recent_contact_first_within_kind():
    now = time.time()
    stale = networking.add_connection("Stale Sam", company="Stripe")
    warm = networking.add_connection("Warm Wendy", company="Stripe")
    never = networking.add_connection("Never Ned", company="Stripe")
    networking.update_connection(stale, {"last_contacted_at": now - 90 * 86400})
    networking.update_connection(warm, {"last_contacted_at": now - 86400})

    resp = client.get("/api/referrals", params={"company": "Stripe"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert [r["match_kind"] for r in data] == ["current"] * 3
    ordered = [r["connection"]["id"] for r in data]
    assert ordered[0] == warm
    assert ordered[1] == stale
    assert ordered[2] == never  # never-contacted sorts last within the kind


# ----------------------- fuzzy matching -----------------------

def test_fuzzy_matching_case_punct_and_substring():
    networking.add_connection("Gina Google", company="Google LLC")

    # case-insensitive + token-substring: 'google' ~ 'Google LLC'
    resp = client.get("/api/referrals", params={"company": "google"})
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["match_kind"] == "fuzzy"
    assert data[0]["matched_company"] == "Google LLC"

    # punctuation-insensitive: 'Google, L.L.C.' normalizes to 'google l l c'
    # whose first token still whole-token-matches.
    resp = client.get("/api/referrals", params={"company": "GOOGLE LLC."})
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["match_kind"] == "fuzzy"


def test_exact_match_beats_fuzzy_and_norm_is_case_insensitive():
    networking.add_connection("Exact Erin", company="  stripe ")
    resp = client.get("/api/referrals", params={"company": "Stripe"})
    data = resp.json()["data"]
    assert len(data) == 1
    assert data[0]["match_kind"] == "current"


def test_fuzzy_does_not_match_partial_tokens_or_short_strings():
    networking.add_connection("Smart Steve", company="Smart Co")
    networking.add_connection("Brief Bea", company="GE")

    # 'art' is inside 'smart' but not a whole token -> no match
    resp = client.get("/api/referrals", params={"company": "art"})
    assert resp.json()["count"] == 0

    # 2-char target only matches by full equality, never substring
    resp = client.get("/api/referrals", params={"company": "GE"})
    data = resp.json()["data"]
    assert [r["connection"]["name"] for r in data] == ["Brief Bea"]
    assert data[0]["match_kind"] == "current"


def test_mention_signal_from_notes_and_role():
    networking.add_connection(
        "Nora Notes", company="FreelanceCo",
        notes="Alumni network: she interned at Datadog before consulting.",
    )
    networking.add_connection(
        "Rolf Role", company="ConsultCo", role="Ex-Datadog SRE, now advisor",
    )
    networking.add_connection("Zed Zero", company="ConsultCo", notes="No overlap at all.")

    resp = client.get("/api/referrals", params={"company": "Datadog"})
    data = resp.json()["data"]
    assert len(data) == 2
    assert all(r["match_kind"] == "mention" for r in data)
    assert {r["connection"]["name"] for r in data} == {"Nora Notes", "Rolf Role"}


# ----------------------- input validation -----------------------

def test_referrals_requires_company_or_job():
    assert client.get("/api/referrals").status_code == 400


def test_referrals_unknown_job_404():
    assert client.get("/api/referrals", params={"job_id": 99999999}).status_code == 404


def test_referrals_company_derived_from_job():
    job_id = _make_job(title="Platform Engineer", company="Stripe")
    networking.add_connection("Alice Current", company="Stripe")
    resp = client.get("/api/referrals", params={"job_id": job_id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["company"] == "Stripe"
    assert body["job"]["id"] == job_id
    assert body["job"]["title"] == "Platform Engineer"
    assert body["count"] == 1


# ----------------------- suggested message -----------------------

def test_suggested_message_contains_real_names_only():
    _set_profile_name("Jane Doe")
    job_id = _make_job(title="Senior Backend Engineer", company="Stripe")
    networking.add_connection("Carlos Rivera", company="Stripe")

    resp = client.get("/api/referrals", params={"company": "Stripe", "job_id": job_id})
    assert resp.status_code == 200
    msg = resp.json()["data"][0]["suggested_message"]

    # Real stored facts appear...
    assert "Carlos" in msg                      # connection first name
    assert "Jane Doe" in msg                    # profile name
    assert "Stripe" in msg                      # company
    assert "Senior Backend Engineer" in msg     # job title from job_id

    # ...and nothing fabricated or templated leaks through.
    for placeholder in ("[", "]", "{", "}", "Your Name", "TODO", "FIXME", "None"):
        assert placeholder not in msg


def test_suggested_message_without_profile_name_omits_signoff():
    _set_profile_name(None)
    networking.add_connection("Carlos Rivera", company="Stripe")
    resp = client.get("/api/referrals", params={"company": "Stripe"})
    msg = resp.json()["data"][0]["suggested_message"]
    assert "Carlos" in msg
    assert "Stripe" in msg
    assert "None" not in msg
    assert "—" not in msg          # signature omitted entirely
    assert "it's" not in msg       # intro omitted entirely
    assert "[" not in msg and "{" not in msg


def test_suggested_message_kind_specific_phrasing_is_evidence_grounded():
    _set_profile_name("Jane Doe")
    networking.add_connection("Alice Current", company="Stripe")
    networking.add_connection(
        "Bob Past", company="OtherCo", additional_companies=[{"company": "Stripe"}],
    )
    networking.add_connection(
        "Dan Mention", company="Elsewhere", notes="knows folks at Stripe",
    )
    resp = client.get("/api/referrals", params={"company": "Stripe"})
    by_kind = {r["match_kind"]: r["suggested_message"] for r in resp.json()["data"]}
    assert "you're at Stripe" in by_kind["current"]
    assert "previously worked at Stripe" in by_kind["past"]
    # mention phrasing must NOT claim they work(ed) there — only that the
    # company came up.
    assert "coming up in your background" in by_kind["mention"]
    assert "you're at" not in by_kind["mention"]
    assert "worked at" not in by_kind["mention"]


# ----------------------- companies-with-connections -----------------------

def test_companies_with_connections_open_jobs_only():
    j_stripe_1 = _make_job(title="SWE", company="Stripe", status="new")
    j_stripe_2 = _make_job(title="SRE", company="Stripe", status="saved")
    _make_job(title="PM", company="Microsoft", status="applied")  # not open
    _make_job(title="Designer", company="NoConnCo", status="new")  # no connection
    j_google = _make_job(title="Data Eng", company="Google LLC", status="new")

    networking.add_connection("Alice Current", company="Stripe")
    networking.add_connection("Bob Past", company="X",
                              additional_companies=[{"company": "Stripe"}])
    networking.add_connection("Mia Microsoft", company="Microsoft")
    networking.add_connection("Gina Google", company="Google")  # fuzzy vs 'Google LLC'

    resp = client.get("/api/referrals/companies-with-connections")
    assert resp.status_code == 200
    body = resp.json()
    by_company = {row["company"]: row for row in body["data"]}

    # Microsoft job isn't open; NoConnCo has no connections.
    assert set(by_company) == {"Stripe", "Google LLC"}

    stripe = by_company["Stripe"]
    assert sorted(stripe["job_ids"]) == sorted([j_stripe_1, j_stripe_2])
    assert stripe["job_count"] == 2
    assert stripe["connection_count"] == 2
    assert stripe["match_kinds"] == {"current": 1, "past": 1}

    google = by_company["Google LLC"]
    assert google["job_ids"] == [j_google]
    assert google["connection_count"] == 1
    assert google["match_kinds"] == {"fuzzy": 1}

    # Sorted by connection_count desc -> Stripe first.
    assert body["data"][0]["company"] == "Stripe"


def test_companies_with_connections_empty_when_no_network():
    _make_job(title="SWE", company="Stripe", status="new")
    resp = client.get("/api/referrals/companies-with-connections")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


# ----------------------- job flags -----------------------

def test_job_flags_map():
    j_yes = _make_job(title="SWE", company="Stripe")
    j_no = _make_job(title="SWE", company="LonelyCo")
    j_nocompany = _make_job(title="SWE", company=None)
    networking.add_connection("Alice Current", company="Stripe")

    resp = client.get(
        "/api/referrals/job-flags",
        params={"job_ids": f"{j_yes},{j_no},{j_nocompany},99999999"},
    )
    assert resp.status_code == 200
    flags = resp.json()["data"]
    assert flags == {
        str(j_yes): True,
        str(j_no): False,
        str(j_nocompany): False,
        "99999999": False,
    }


def test_job_flags_fuzzy_and_mention_count_as_referral():
    j_fuzzy = _make_job(title="SWE", company="Google LLC")
    j_mention = _make_job(title="SWE", company="Datadog")
    networking.add_connection("Gina Google", company="Google")
    networking.add_connection("Nora Notes", company="Elsewhere",
                              notes="interned at Datadog")
    resp = client.get("/api/referrals/job-flags",
                      params={"job_ids": f"{j_fuzzy},{j_mention}"})
    flags = resp.json()["data"]
    assert flags[str(j_fuzzy)] is True
    assert flags[str(j_mention)] is True


def test_job_flags_empty_and_bad_input():
    resp = client.get("/api/referrals/job-flags", params={"job_ids": ""})
    assert resp.status_code == 200
    assert resp.json()["data"] == {}

    # tolerant of stray commas/spaces
    j = _make_job(title="SWE", company="Stripe")
    networking.add_connection("Alice", company="Stripe")
    resp = client.get("/api/referrals/job-flags", params={"job_ids": f" {j}, ,"})
    assert resp.status_code == 200
    assert resp.json()["data"] == {str(j): True}

    assert client.get("/api/referrals/job-flags",
                      params={"job_ids": "1,abc"}).status_code == 400


# ----------------------- service-level sanity -----------------------

def test_service_find_referrals_direct():
    networking.add_connection("Alice Current", company="Stripe")
    out = networking.find_referrals(company="Stripe")
    assert out["company"] == "Stripe"
    assert out["job"] is None
    assert out["count"] == 1
    assert out["results"][0]["match_kind"] == "current"


def test_service_limit_caps_results():
    for i in range(5):
        networking.add_connection(f"Person {i}", company="Stripe")
    out = networking.find_referrals(company="Stripe", limit=2)
    assert out["count"] == 2
    assert len(out["results"]) == 2
