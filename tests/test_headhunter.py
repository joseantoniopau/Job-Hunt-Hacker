"""Tests for the Headhunter-mode feature set:
- Agnostic role-family coverage (legal, medical, finance, education, creative,
  hospitality, trades, science).
- Salary intelligence.
- Company research aggregation.
- Networking CRUD + referral lookup.
- Velocity funnel + bottleneck.
- Negotiation market comparison.
- Followup email drafting (each stage).
- Agnostic keyword categories in the seed file.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.db import get_conn, init_db, tx
from backend.app.main import app
from backend.app.matching.scorer import _classify_role_families
from backend.app.services import (
    company_research,
    networking,
    salary_intelligence,
    velocity,
)
from backend.app.tailoring import followup_emails, negotiation

client = TestClient(app)


# ----------------------- helpers -----------------------

def _make_job(
    title: str = "Test Job",
    company: str = "TestCo",
    salary_min: int | None = None,
    salary_max: int | None = None,
    currency: str = "USD",
    location: str | None = "Remote",
    discovered_at: float | None = None,
) -> int:
    init_db()
    h = f"hash-{title}-{company}-{time.time_ns()}"
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO job_posting
               (source, title, company, location, salary_min, salary_max, currency,
                status, hash, discovered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)""",
            (
                "test", title, company, location, salary_min, salary_max,
                currency, h, discovered_at if discovered_at is not None else time.time(),
            ),
        )
        return int(cur.lastrowid)


def _make_application(job_id: int, status: str = "saved",
                      applied_at: float | None = None) -> int:
    init_db()
    with tx() as conn:
        cur = conn.execute(
            """INSERT INTO application (job_id, status, applied_at, audit_json)
               VALUES (?, ?, ?, '[]')""",
            (int(job_id), status, applied_at),
        )
        return int(cur.lastrowid)


def _record_outcome(application_id: int, outcome: str) -> None:
    from backend.app.services import effectiveness_tracker
    effectiveness_tracker.record(application_id, None, outcome)


def _wipe_test_state() -> None:
    """Clear test tables so funnel/velocity tests get a clean slate."""
    init_db()
    with tx() as conn:
        conn.execute("DELETE FROM effectiveness_event")
        conn.execute("DELETE FROM application")
        conn.execute("DELETE FROM connection_company")
        conn.execute("DELETE FROM connection")
        # Don't wipe job_posting — other tests may depend on it, and the
        # individual tests insert their own jobs with unique titles.


# ----------------------- agnostic coverage tests -----------------------

def test_role_families_include_non_tech():
    """Every new non-tech family classifies sample titles correctly."""
    assert "legal" in _classify_role_families("Associate Attorney")
    assert "legal" in _classify_role_families("Paralegal")
    assert "legal" in _classify_role_families("General Counsel")
    assert "medical" in _classify_role_families("Registered Nurse")
    assert "medical" in _classify_role_families("Pharmacist")
    assert "medical" in _classify_role_families("Physical Therapist")
    assert "finance" in _classify_role_families("Financial Analyst")
    assert "finance" in _classify_role_families("Controller")
    assert "finance" in _classify_role_families("Tax Accountant")
    assert "education" in _classify_role_families("Elementary Teacher")
    assert "education" in _classify_role_families("Curriculum Designer")
    assert "creative" in _classify_role_families("Art Director")
    assert "creative" in _classify_role_families("Videographer")
    assert "hospitality" in _classify_role_families("Executive Chef")
    assert "hospitality" in _classify_role_families("Sommelier")
    assert "trades" in _classify_role_families("Master Electrician")
    assert "trades" in _classify_role_families("HVAC Technician")
    assert "science" in _classify_role_families("Research Scientist")
    assert "science" in _classify_role_families("Microbiologist")


def test_agnostic_keywords_include_non_tech():
    """The seed ats_keywords.json carries categories for non-tech work."""
    p = Path(__file__).resolve().parents[1] / "data" / "seed" / "ats_keywords.json"
    assert p.exists(), "ats_keywords seed missing"
    data = json.loads(p.read_text())
    cats = data.get("categories") or {}
    for required in (
        "legal_terms", "medical_terms", "finance_terms",
        "sales_terms", "marketing_terms", "creative_terms",
        "education_terms", "hospitality_terms",
    ):
        assert required in cats, f"missing category: {required}"
        # Each new category must carry real keyword entries.
        assert len(cats[required]) >= 20, f"{required} has too few keywords"


# ----------------------- salary intelligence -----------------------

def test_salary_intelligence_empty_db_returns_clean_shape():
    """No matching jobs -> well-formed empty response."""
    out = salary_intelligence.compute_market(role="ZzzNoSuchRoleXyz123")
    assert out["count"] == 0
    assert out["p25"] is None
    assert out["median"] is None
    assert out["p75"] is None
    assert out["p90"] is None
    assert out["sample_jobs"] == []
    assert "currency" in out


def test_salary_intelligence_computes_percentiles():
    """Seed 10 jobs with known salaries and verify percentiles look right."""
    salaries = [100_000, 110_000, 120_000, 130_000, 140_000,
                150_000, 160_000, 170_000, 180_000, 200_000]
    for s in salaries:
        # min=max so the midpoint is exactly the salary value
        _make_job(
            title="Senior PercentileEngineer at Acme",
            company=f"AcmePerc{s}",
            salary_min=s,
            salary_max=s,
        )
    out = salary_intelligence.compute_market(role="Senior PercentileEngineer")
    assert out["count"] == 10
    assert out["median"] is not None
    # With salaries 100k..200k, median should be between 140k and 160k.
    assert 140_000 <= out["median"] <= 160_000, f"median {out['median']}"
    assert out["p25"] is not None and out["p25"] <= out["median"]
    assert out["p75"] is not None and out["p75"] >= out["median"]
    assert out["p90"] is not None and out["p90"] >= out["p75"]


# ----------------------- company research -----------------------

def test_company_research_aggregates_history():
    """Verify enrich() pulls jobs + applications + outcomes for a company."""
    co = "AcmeResearchCorp"
    j1 = _make_job(title="Backend Engineer", company=co, salary_min=120_000, salary_max=180_000)
    j2 = _make_job(title="DevOps Engineer", company=co, salary_min=130_000, salary_max=170_000)
    a1 = _make_application(j1, status="applied", applied_at=time.time())
    _record_outcome(a1, "replied")

    out = company_research.enrich(co)
    assert out["company"] == co
    assert out["jobs_seen"] >= 2
    assert out["salary_range"] is not None
    assert out["salary_range"]["min"] <= 120_000
    assert out["salary_range"]["max"] >= 180_000
    assert len(out["our_applications"]) >= 1
    assert out["outcomes"].get("replied", 0) >= 1
    assert isinstance(out["recent_jobs"], list)


# ----------------------- networking -----------------------

def test_networking_crud():
    cid = networking.add_connection(
        name="Test Person A",
        relationship="former colleague",
        company="NetTestCo",
        role="Engineer",
        contact="https://linkedin.com/in/testpersona",
        notes="met at conference",
    )
    assert cid > 0
    got = networking.get_connection(cid)
    assert got is not None
    assert got["name"] == "Test Person A"
    assert got["company"] == "NetTestCo"

    updated = networking.update_connection(cid, {"notes": "warm intro candidate"})
    assert updated["notes"] == "warm intro candidate"

    listing = networking.list_connections(limit=50)
    assert any(c["id"] == cid for c in listing)

    assert networking.delete_connection(cid) is True
    assert networking.get_connection(cid) is None


def test_networking_refer_at_company_returns_matching_connections():
    cid1 = networking.add_connection(
        name="Referrer Alpha", company="StripeReferTest", role="Engineer",
        contact="alpha@example.com",
    )
    cid2 = networking.add_connection(
        name="Referrer Beta", company="OtherCo", role="Manager",
        contact="beta@example.com",
        additional_companies=[{"company": "StripeReferTest", "role": "Past PM"}],
    )
    # An unrelated connection that should NOT match.
    networking.add_connection(
        name="Unrelated Gamma", company="UnrelatedCo", role="Designer",
        contact="gamma@example.com",
    )
    matches = networking.who_could_refer_at("StripeReferTest")
    ids = {c["id"] for c in matches}
    assert cid1 in ids
    assert cid2 in ids
    assert all(c["name"] != "Unrelated Gamma" for c in matches)
    # cleanup
    networking.delete_connection(cid1)
    networking.delete_connection(cid2)


# ----------------------- velocity -----------------------

def test_velocity_funnel_with_no_apps_returns_clean_shape():
    _wipe_test_state()
    f = velocity.funnel()
    assert f["applied"] == 0
    assert f["reply_rate"] == 0.0
    assert f["interview_rate"] == 0.0
    assert f["offer_rate"] == 0.0
    # All canonical keys present
    for key in ("prepared", "applied", "replied", "screened", "interviewed",
                "offered", "rejected", "ghosted",
                "reply_rate", "interview_rate", "offer_rate"):
        assert key in f


def test_velocity_funnel_with_data_computes_rates():
    _wipe_test_state()
    # 10 applied, 3 replied, 1 interviewed, 1 offered.
    for i in range(10):
        j = _make_job(title=f"Vel role {i}", company=f"VelCo{i}")
        a = _make_application(j, status="applied", applied_at=time.time())
        if i < 3:
            _record_outcome(a, "replied")
        if i == 9:
            _record_outcome(a, "interviewed")
            _record_outcome(a, "offered")
    f = velocity.funnel()
    assert f["applied"] == 10
    assert f["reply_rate"] > 0
    assert f["interview_rate"] >= 0.1
    assert f["offer_rate"] >= 0.1


# ----------------------- negotiation -----------------------

def test_negotiation_compare_to_market():
    # Seed market data with a known role first
    salaries = [120_000, 130_000, 140_000, 150_000, 160_000]
    for s in salaries:
        _make_job(
            title="NegoMarketTest Engineer",
            company=f"NegoCo{s}",
            salary_min=s, salary_max=s,
        )
    cmp = negotiation.compare_to_market(
        offer=125_000, role="NegoMarketTest Engineer", currency="USD"
    )
    assert cmp["offer"] == 125_000
    assert cmp["market_count"] >= 5
    assert cmp["market_median"] is not None
    assert cmp["percentile_band"] in ("0-25", "25-50")
    assert cmp["recommendation"]


def test_negotiation_generate_returns_full_script():
    j = _make_job(
        title="NegoScriptTest Senior Engineer",
        company="NegoScriptCo",
        salary_min=130_000, salary_max=180_000,
    )
    a = _make_application(j, status="applied", applied_at=time.time())
    out = negotiation.generate(
        application_id=a, offer_base=140_000, offer_total=160_000, currency="USD"
    )
    s = out["script"]
    for key in ("opening", "market_anchor", "counter_ask",
                "fallback_position", "walkaway", "talking_points"):
        assert key in s, f"missing key {key}"
    assert isinstance(s["talking_points"], list)
    assert out["provenance"]["provider"] in ("template", "llm")


# ----------------------- followups -----------------------

@pytest.mark.parametrize("stage", followup_emails.STAGES)
def test_followup_draft_each_stage_returns_subject_and_body(stage):
    j = _make_job(title="Followup Engineer", company="FollowupCo")
    a = _make_application(j, status="applied", applied_at=time.time())
    out = followup_emails.draft(application_id=a, stage=stage)
    assert out["stage"] == stage
    assert out["subject"]
    assert out["body"]
    assert len(out["body"]) > 30
    assert "provenance" in out
    assert "honesty_report" in out


def test_followup_stages_endpoint():
    r = client.get("/api/followup/stages")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    stages = [s["stage"] for s in body["data"]]
    for required in followup_emails.STAGES:
        assert required in stages


def test_followup_unknown_stage_raises():
    j = _make_job(title="Followup-bad-stage", company="BadStageCo")
    a = _make_application(j, status="applied", applied_at=time.time())
    with pytest.raises(ValueError):
        followup_emails.draft(application_id=a, stage="not_a_real_stage")


# ----------------------- http smoke -----------------------

def test_salary_market_http_endpoint():
    r = client.get("/api/salary/market?role=Backend+Engineer")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "count" in body["data"]


def test_salary_summary_http_endpoint():
    r = client.get("/api/salary/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "per_role" in body["data"]


def test_velocity_endpoints_http():
    r = client.get("/api/velocity/funnel")
    assert r.status_code == 200 and r.json()["ok"]
    r = client.get("/api/velocity/weekly?weeks=4")
    assert r.status_code == 200 and r.json()["ok"]
    r = client.get("/api/velocity/bottleneck")
    assert r.status_code == 200 and r.json()["ok"]


def test_connections_http_crud():
    r = client.post(
        "/api/connections",
        json={
            "name": "HTTP Test Connection",
            "company": "HttpTestCo",
            "role": "Engineer",
            "contact": "test@example.com",
        },
    )
    assert r.status_code == 200, r.text
    cid = r.json()["data"]["id"]

    r = client.get("/api/connections")
    assert r.status_code == 200
    assert any(c["id"] == cid for c in r.json()["data"])

    r = client.get("/api/connections/refer/HttpTestCo")
    assert r.status_code == 200
    assert any(c["id"] == cid for c in r.json()["data"])

    r = client.delete(f"/api/connections/{cid}")
    assert r.status_code == 200
