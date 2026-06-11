"""Tests for Task G3 — resume A/B variants + outcome feedback loop.

Covers:
  * ab_summary() aggregation math (events + application status history,
    per-application dedup, orphan events, insufficient-sample flagging)
  * GET /api/effectiveness/ab endpoint
  * POST /api/effectiveness/job-feedback recording (good/bad verdicts,
    validation, 404s, linkage to latest application)
  * scorer.load_feedback_adjustments() + score_job() integration:
    no effect below n=5, capped at +/-15%, direction correct for both the
    role-family penalty and the keyword weight.

conftest.py redirects the DB to a temp file, so nothing here touches the
user's real vault.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.app.db import get_conn, init_db, tx
from backend.app.main import app
from backend.app.matching import scorer
from backend.app.services import effectiveness_tracker as et

client = TestClient(app)


# ----------------------------- fixtures / helpers -----------------------------

@pytest.fixture(autouse=True)
def _clean_db():
    """Each test starts from an empty funnel/feedback state."""
    init_db()
    with tx() as conn:
        conn.execute("DELETE FROM effectiveness_event")
        conn.execute("DELETE FROM application")
        conn.execute("DELETE FROM job_match")
        conn.execute("DELETE FROM cover_letter")
        conn.execute("DELETE FROM tailored_resume")
        conn.execute("DELETE FROM job_posting")
        conn.execute(
            "UPDATE user_profile SET target_titles = NULL, scoring_weights_json = NULL "
            "WHERE id = 1"
        )
    yield


_seq = {"n": 0}


def _make_job(title: str = "Test Job", company: str = "TestCo",
              description: str = "") -> int:
    _seq["n"] += 1
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO job_posting (source, title, company, description, status, hash, discovered_at) "
            "VALUES (?, ?, ?, ?, 'new', ?, ?)",
            ("test", title, company, description,
             f"hash-{title}-{_seq['n']}-{time.time()}", time.time()),
        )
        return int(cur.lastrowid)


def _make_resume(resume_type: str = "job_specific") -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO tailored_resume (resume_type, markdown, created_at) "
            "VALUES (?, '# r', ?)",
            (resume_type, time.time()),
        )
        return int(cur.lastrowid)


def _make_app(job_id: int, resume_id: int | None = None, status: str = "saved",
              audit_history: list | None = None) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO application (job_id, status, resume_id, audit_json) "
            "VALUES (?, ?, ?, ?)",
            (job_id, status, resume_id, json.dumps(audit_history or [])),
        )
        return int(cur.lastrowid)


def _set_target_titles(titles: list[str]) -> None:
    with tx() as conn:
        conn.execute(
            "UPDATE user_profile SET target_titles = ? WHERE id = 1",
            (json.dumps(titles),),
        )


def _record_feedback(n_good: int, n_bad: int, title: str = "Data Analyst") -> None:
    """Create n_good + n_bad jobs of the given title and rate them."""
    for _ in range(n_good):
        et.record_job_feedback(_make_job(title), "good_fit")
    for _ in range(n_bad):
        et.record_job_feedback(_make_job(title), "bad_fit")


# ----------------------------- A/B aggregation -----------------------------

def test_ab_aggregation_math_two_styles():
    # Style "concise": 6 apps sent; 3 reach replied; 2 interviewed; 1 offered.
    rid_a = _make_resume("concise")
    stages = ["offered", "interviewed", "replied", "sent", "sent", "sent"]
    for outcome in stages:
        aid = _make_app(_make_job(), resume_id=rid_a)
        et.record(aid, rid_a, "sent")
        if outcome != "sent":
            et.record(aid, rid_a, outcome)
    # Style "detailed": 2 apps sent, 1 replied -> insufficient at min_n=5.
    rid_b = _make_resume("detailed")
    aid1 = _make_app(_make_job(), resume_id=rid_b)
    et.record(aid1, rid_b, "sent")
    et.record(aid1, rid_b, "replied")
    aid2 = _make_app(_make_job(), resume_id=rid_b)
    et.record(aid2, rid_b, "sent")

    data = et.ab_summary(min_n=5)
    by_style = {s["style"]: s for s in data["styles"]}
    a = by_style["concise"]
    assert a["sent"] == 6
    assert a["replied"] == 3       # cumulative: offered+interviewed+replied
    assert a["interviewed"] == 2   # offered + interviewed
    assert a["offered"] == 1
    assert a["reply_rate"] == pytest.approx(0.5)
    assert a["interview_rate"] == pytest.approx(round(2 / 6, 4))
    assert a["offer_rate"] == pytest.approx(round(1 / 6, 4))
    assert a["insufficient_data"] is False
    assert a["caveat"] == ""

    b = by_style["detailed"]
    assert b["sent"] == 2 and b["replied"] == 1
    assert b["insufficient_data"] is True
    assert "2 application" in b["caveat"] and "5" in b["caveat"]

    # Sufficient styles sort before insufficient ones.
    assert data["styles"][0]["style"] == "concise"
    assert data["min_n"] == 5 and data["total_styles"] == 2


def test_ab_uses_application_status_history():
    """No effectiveness events at all — funnel comes from status history."""
    rid = _make_resume("history_style")
    hist = [
        {"ts": 1.0, "action": "created", "status": "saved"},
        {"ts": 2.0, "action": "update", "fields": {"status": "applied"}},
        {"ts": 3.0, "action": "update", "fields": {"status": "replied"}},
    ]
    _make_app(_make_job(), resume_id=rid, status="interview", audit_history=hist)
    data = et.ab_summary(min_n=1)
    row = {s["style"]: s for s in data["styles"]}["history_style"]
    assert row["sent"] == 1
    assert row["replied"] == 1
    assert row["interviewed"] == 1  # current status 'interview'
    assert row["offered"] == 0


def test_ab_saved_only_application_not_counted():
    rid = _make_resume("idle_style")
    _make_app(_make_job(), resume_id=rid, status="saved")
    data = et.ab_summary(min_n=1)
    assert "idle_style" not in {s["style"] for s in data["styles"]}


def test_ab_dedupes_multiple_events_per_application():
    rid = _make_resume("dedupe_style")
    aid = _make_app(_make_job(), resume_id=rid)
    for _ in range(3):
        et.record(aid, rid, "sent")
    et.record(aid, rid, "replied")
    row = {s["style"]: s for s in et.ab_summary(min_n=1)["styles"]}["dedupe_style"]
    assert row["sent"] == 1
    assert row["replied"] == 1


def test_ab_orphan_events_each_count_independently():
    rid = _make_resume("orphan_style")
    et.record(None, rid, "sent")
    et.record(None, rid, "offered")
    row = {s["style"]: s for s in et.ab_summary(min_n=1)["styles"]}["orphan_style"]
    assert row["sent"] == 2          # two independent units
    assert row["offered"] == 1
    assert row["replied"] == 1       # cumulative from the offered unit


def test_ab_rejected_implies_sent_and_flags():
    rid = _make_resume("rej_style")
    _make_app(_make_job(), resume_id=rid, status="rejected")
    row = {s["style"]: s for s in et.ab_summary(min_n=1)["styles"]}["rej_style"]
    assert row["sent"] == 1
    assert row["rejected"] == 1
    assert row["replied"] == 0


def test_ab_feedback_events_excluded_from_funnel():
    rid = _make_resume("fb_style")
    jid = _make_job("Data Analyst")
    aid = _make_app(jid, resume_id=rid)
    et.record(aid, rid, "sent")
    et.record_job_feedback(jid, "good_fit")
    row = {s["style"]: s for s in et.ab_summary(min_n=1)["styles"]}["fb_style"]
    assert row["sent"] == 1 and row["replied"] == 0


def test_ab_endpoint():
    rid = _make_resume("ep_style")
    aid = _make_app(_make_job(), resume_id=rid)
    et.record(aid, rid, "sent")
    r = client.get("/api/effectiveness/ab", params={"min_n": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["min_n"] == 2
    row = {s["style"]: s for s in body["data"]["styles"]}["ep_style"]
    assert row["insufficient_data"] is True

    assert client.get("/api/effectiveness/ab", params={"min_n": 0}).status_code == 400


def test_record_endpoint_accepts_job_id():
    jid = _make_job()
    aid = _make_app(jid)
    r = client.post("/api/effectiveness/record",
                    json={"application_id": aid, "outcome": "sent", "job_id": jid})
    assert r.status_code == 200
    eid = r.json()["data"]["id"]
    row = get_conn().execute(
        "SELECT job_id, outcome FROM effectiveness_event WHERE id = ?", (eid,)
    ).fetchone()
    assert row["job_id"] == jid and row["outcome"] == "sent"


# ----------------------------- job feedback recording -----------------------------

def test_job_feedback_recorded_with_outcome_notes_and_links():
    jid = _make_job("Data Analyst")
    rid = _make_resume("concise")
    aid = _make_app(jid, resume_id=rid)
    r = client.post("/api/effectiveness/job-feedback",
                    json={"job_id": jid, "verdict": "good_fit", "reason": "great stack"})
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["outcome"] == "user_feedback_good"
    row = get_conn().execute(
        "SELECT * FROM effectiveness_event WHERE id = ?", (body["data"]["id"],)
    ).fetchone()
    assert row["outcome"] == "user_feedback_good"
    assert row["notes"] == "great stack"
    assert row["job_id"] == jid
    assert row["application_id"] == aid   # latest application linked
    assert row["resume_id"] == rid

    r2 = client.post("/api/effectiveness/job-feedback",
                     json={"job_id": jid, "verdict": "bad_fit"})
    assert r2.status_code == 200
    assert r2.json()["data"]["outcome"] == "user_feedback_bad"


def test_job_feedback_validation_errors():
    jid = _make_job()
    assert client.post("/api/effectiveness/job-feedback",
                       json={"job_id": jid, "verdict": "meh"}).status_code == 400
    assert client.post("/api/effectiveness/job-feedback",
                       json={"job_id": 999999, "verdict": "good_fit"}).status_code == 404


def test_feedback_summary_endpoint():
    _record_feedback(5, 0, title="Data Analyst")
    r = client.get("/api/effectiveness/feedback-summary")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["min_events"] == 5
    assert data["max_shift"] == pytest.approx(0.15)
    fam = data["families"]["data"]
    assert fam["good"] == 5 and fam["bad"] == 0 and fam["active"] is True


# ----------------------------- scorer feedback loop -----------------------------

def test_no_adjustment_below_min_sample():
    _set_target_titles(["Backend Engineer"])
    _record_feedback(4, 0, title="Data Analyst")   # n=4 < 5
    adj = scorer.load_feedback_adjustments()
    assert adj["data"]["active"] is False
    assert adj["data"]["factor"] == 1.0

    jid = _make_job("Data Analyst")  # empty description -> neutral sub-scores, nonzero base
    baseline = scorer.score_job(jid, llm_polish=False, apply_feedback=False)
    adjusted = scorer.score_job(jid, llm_polish=False, apply_feedback=True)
    assert adjusted["feedback_factor"] == 1.0
    assert adjusted["overall_score"] == baseline["overall_score"]
    assert adjusted["weights_used"] == baseline["weights_used"]


def test_good_feedback_softens_penalty_and_raises_keyword_weight():
    _set_target_titles(["Backend Engineer"])   # engineering family
    _record_feedback(5, 0, title="Data Analyst")  # data family, all good
    adj = scorer.load_feedback_adjustments()
    assert adj["data"]["factor"] == pytest.approx(1.15)

    jid = _make_job("Data Analyst")  # empty description -> neutral sub-scores, nonzero base
    baseline = scorer.score_job(jid, llm_polish=False, apply_feedback=False)
    adjusted = scorer.score_job(jid, llm_polish=False, apply_feedback=True)

    # Mismatched family: base penalty 0.45, softened to 0.45 * 1.15.
    assert baseline["role_family_penalty"] == pytest.approx(0.45)
    assert adjusted["role_family_penalty"] == pytest.approx(round(0.45 * 1.15, 4))
    assert adjusted["feedback_factor"] == pytest.approx(1.15)
    assert "data" in adjusted["feedback_families"]
    assert adjusted["overall_score"] > baseline["overall_score"]
    # Keyword dimension gains relative weight after renormalization.
    assert adjusted["weights_used"]["keywords"] > baseline["weights_used"]["keywords"]


def test_bad_feedback_hardens_penalty_and_lowers_keyword_weight():
    _set_target_titles(["Backend Engineer"])
    _record_feedback(0, 5, title="Data Analyst")
    adj = scorer.load_feedback_adjustments()
    assert adj["data"]["factor"] == pytest.approx(0.85)

    jid = _make_job("Data Analyst")  # empty description -> neutral sub-scores, nonzero base
    baseline = scorer.score_job(jid, llm_polish=False, apply_feedback=False)
    adjusted = scorer.score_job(jid, llm_polish=False, apply_feedback=True)

    assert adjusted["role_family_penalty"] == pytest.approx(round(0.45 * 0.85, 4))
    assert adjusted["overall_score"] < baseline["overall_score"]
    assert adjusted["weights_used"]["keywords"] < baseline["weights_used"]["keywords"]


def test_bad_feedback_on_matched_family_does_not_flag_mismatch():
    """Feedback hardening (1.0 -> 0.85) must not masquerade as a
    role-family mismatch red flag."""
    _set_target_titles(["Data Analyst"])           # data family — matched
    _record_feedback(0, 5, title="Data Analyst")
    jid = _make_job("Data Analyst")
    adjusted = scorer.score_job(jid, llm_polish=False, apply_feedback=True)
    assert adjusted["role_family_penalty_base"] == pytest.approx(1.0)
    assert adjusted["role_family_penalty"] == pytest.approx(0.85)
    assert not any("Role-family mismatch" in f for f in adjusted["red_flags"])


def test_adjustment_capped_at_fifteen_percent():
    _record_feedback(20, 0, title="Data Analyst")
    adj = scorer.load_feedback_adjustments()
    assert adj["data"]["factor"] == pytest.approx(1.15)   # capped, not 1 + 0.15*20

    with tx() as conn:
        conn.execute("DELETE FROM effectiveness_event")
        conn.execute("DELETE FROM job_posting")
    _record_feedback(0, 20, title="Data Analyst")
    adj = scorer.load_feedback_adjustments()
    assert adj["data"]["factor"] == pytest.approx(0.85)

    # Structural clamp: even a corrupt/extreme per-family factor can't push
    # the applied factor outside [0.85, 1.15].
    f, fams = scorer._feedback_factor_for_job(
        "Data Analyst", {"data": {"factor": 9.0, "active": True}})
    assert f == pytest.approx(1.15) and fams == ["data"]
    f, _ = scorer._feedback_factor_for_job(
        "Data Analyst", {"data": {"factor": 0.01, "active": True}})
    assert f == pytest.approx(0.85)


def test_mixed_feedback_proportional_signal():
    # 3 good / 2 bad -> signal 0.2 -> factor 1 + 0.15*0.2 = 1.03
    _record_feedback(3, 2, title="Data Analyst")
    adj = scorer.load_feedback_adjustments()
    assert adj["data"]["n"] == 5
    assert adj["data"]["signal"] == pytest.approx(0.2)
    assert adj["data"]["factor"] == pytest.approx(1.03)


def test_feedback_only_applies_to_rated_families():
    """Feedback on data-family jobs must not move an engineering job."""
    _set_target_titles(["Backend Engineer"])
    _record_feedback(5, 0, title="Data Analyst")
    jid = _make_job("Backend Engineer")
    adjusted = scorer.score_job(jid, llm_polish=False, apply_feedback=True)
    assert adjusted["feedback_factor"] == 1.0
    assert adjusted["feedback_families"] == []
    assert adjusted["role_family_penalty"] == pytest.approx(1.0)


def test_no_feedback_at_all_is_a_noop():
    adj = scorer.load_feedback_adjustments()
    assert adj == {}
    jid = _make_job("Data Analyst")
    res = scorer.score_job(jid, llm_polish=False)
    assert res["feedback_factor"] == 1.0
