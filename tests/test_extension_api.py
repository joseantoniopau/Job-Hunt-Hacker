"""HTTP tests for the browser-extension support API (/api/extension).

Covers: /status, /fill-data matched by apply_url host+path, /fill-data
matched by fuzzy company+title, tailored-resume / cover-letter / base-resume
selection, evidence-grounded answer composition, and the graceful
empty-vault response.

NOTE: test order matters — the empty-vault test runs FIRST, before any
seeding, because the suite shares one per-process SQLite file.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from backend.app.db import tx
from backend.app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# seed helpers
# ---------------------------------------------------------------------------

def _seed_job(title: str, company: str, apply_url: str | None = None,
              description: str = "", requirements: str = "") -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO job_posting (source, title, company, location, "
            "description, requirements, apply_url, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("test", title, company, "Remote", description, requirements,
             apply_url, time.time()),
        )
        return int(cur.lastrowid)


def _seed_tailored_resume(job_id: int, plain_text: str) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO tailored_resume (job_id, plain_text, created_at) "
            "VALUES (?, ?, ?)",
            (job_id, plain_text, time.time()),
        )
        return int(cur.lastrowid)


def _seed_cover_letter(job_id: int, text: str) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO cover_letter (job_id, text, created_at) VALUES (?, ?, ?)",
            (job_id, text, time.time()),
        )
        return int(cur.lastrowid)


def _seed_base_resume(raw_text: str, is_master: int = 1) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO resume_document (filename, file_type, raw_text, "
            "is_master, created_at) VALUES ('base.md', 'md', ?, ?, ?)",
            (raw_text, is_master, time.time()),
        )
        return int(cur.lastrowid)


def _seed_claims(claims: list[dict]) -> list[int]:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO evidence_source (source_type, title, raw_text, "
            "created_at) VALUES ('resume', 'seed', 'seed evidence text', ?)",
            (time.time(),),
        )
        source_id = int(cur.lastrowid)
        ids: list[int] = []
        for cl in claims:
            cur = c.execute(
                "INSERT INTO career_claim (source_id, claim_type, claim_text, "
                "skill, tool, confidence, user_verified, allowed_for_resume, "
                "contradiction_status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id,
                 cl.get("claim_type", "accomplishment"),
                 cl["claim_text"],
                 cl.get("skill"),
                 cl.get("tool"),
                 cl.get("confidence", 0.9),
                 cl.get("user_verified", 1),
                 cl.get("allowed_for_resume", 1),
                 cl.get("contradiction_status", "none"),
                 time.time()),
            )
            ids.append(int(cur.lastrowid))
        return ids


def _fill(params: dict) -> dict:
    r = client.get("/api/extension/fill-data", params=params)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    return body.get("data") or {}


# ---------------------------------------------------------------------------
# 1. graceful empty-vault response
# ---------------------------------------------------------------------------

def _wipe_vault() -> None:
    """Make emptiness explicit — the session DB is shared across test files,
    so 'runs first' is not a real guarantee."""
    with tx() as c:
        for t in ("cover_letter", "tailored_resume", "career_claim",
                  "evidence_source", "job_posting", "resume_document"):
            c.execute(f"DELETE FROM {t}")


def test_fill_data_empty_vault_graceful():
    _wipe_vault()
    data = _fill({})
    # Profile row is the seeded singleton: present, fields nullable.
    profile = data.get("profile") or {}
    for key in ("name", "email", "phone", "location", "linkedin_url",
                "github_url", "portfolio_url"):
        assert key in profile
    assert data.get("job") is None
    assert data.get("resume_text") is None
    assert data.get("resume_id") is None
    assert data.get("resume_source") is None
    assert data.get("cover_letter_text") is None
    assert data.get("cover_letter_id") is None
    answers = data.get("answers") or {}
    assert answers.get("why_company") is None
    assert answers.get("experience_summary") is None
    assert answers.get("evidence_claim_ids") == []


def test_fill_data_empty_vault_with_unmatched_url():
    _wipe_vault()
    data = _fill({"url": "https://jobs.example.com/postings/999999"})
    assert data.get("job") is None
    assert data.get("resume_text") is None
    assert (data.get("answers") or {}).get("experience_summary") is None


# ---------------------------------------------------------------------------
# 2. /status
# ---------------------------------------------------------------------------

def test_router_registered_in_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert "extension_api" in (r.json().get("routers_loaded") or [])


def test_status_reachable_and_profile_name():
    r = client.get("/api/extension/status")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    data = body.get("data") or {}
    assert data.get("reachable") is True
    assert data.get("app") == "Job Hunt Hacker"
    assert data.get("version")
    counts = data.get("counts") or {}
    for key in ("jobs", "claims", "tailored_resumes", "cover_letters"):
        assert isinstance(counts.get(key), int)

    # Profile name surfaces once set.
    tag = f"Ext Tester {int(time.time() * 1000)}"
    r2 = client.put("/api/profile", json={"name": tag, "email": "ext@example.com",
                                          "phone": "+1 555 0100",
                                          "location": "Austin, TX",
                                          "linkedin_url": "",
                                          "github_url": "",
                                          "portfolio_url": ""})
    assert r2.status_code == 200
    r3 = client.get("/api/extension/status")
    assert (r3.json().get("data") or {}).get("profile_name") == tag


# ---------------------------------------------------------------------------
# 3. matching by apply_url host+path
# ---------------------------------------------------------------------------

def test_fill_data_match_by_url_exact_normalized():
    job_id = _seed_job(
        "Platform Engineer", "Acme Widgets",
        apply_url="https://boards.greenhouse.io/acmewidgets/jobs/4012",
    )
    # www prefix + trailing slash + query string must all normalize away.
    data = _fill({"url": "http://www.boards.greenhouse.io/acmewidgets/jobs/4012/?utm_source=jhh"})
    job = data.get("job") or {}
    assert job.get("id") == job_id
    assert job.get("matched_by") == "url"
    assert job.get("company") == "Acme Widgets"
    # Profile travels with every response.
    assert "email" in (data.get("profile") or {})


def test_fill_data_match_by_url_path_prefix():
    # Page URL is the job URL plus an /application suffix — same host,
    # job path is a prefix of the page path.
    data = _fill({"url": "https://boards.greenhouse.io/acmewidgets/jobs/4012/application"})
    job = data.get("job") or {}
    assert job.get("company") == "Acme Widgets"
    assert job.get("matched_by") == "url"


def test_fill_data_url_no_match_on_other_host():
    data = _fill({"url": "https://lever.co/acmewidgets/jobs/4012"})
    assert data.get("job") is None


# ---------------------------------------------------------------------------
# 4. fuzzy company+title matching
# ---------------------------------------------------------------------------

def test_fill_data_match_by_company_and_title_fuzzy():
    job_id = _seed_job("Senior Backend Engineer", "Vextrel Dynamics",
                       apply_url="https://jobs.vextrel.example/careers/77")
    data = _fill({"company": "vextrel dynamics",
                  "title": "Senior Backend Engineer (Remote)"})
    job = data.get("job") or {}
    assert job.get("id") == job_id
    assert job.get("matched_by") == "company_title"


def test_fill_data_url_miss_falls_back_to_company_title():
    data = _fill({"url": "https://totally-unrelated.example/apply/1",
                  "company": "Vextrel Dynamics",
                  "title": "Senior Backend Engineer"})
    job = data.get("job") or {}
    assert job.get("company") == "Vextrel Dynamics"
    assert job.get("matched_by") == "company_title"


def test_fill_data_wrong_company_does_not_match():
    data = _fill({"company": "Globotron Unrelated Industries",
                  "title": "Senior Backend Engineer"})
    # company+title given with a non-existent company: strict gate => None
    assert data.get("job") is None


# ---------------------------------------------------------------------------
# 5. resume + cover-letter selection
# ---------------------------------------------------------------------------

def test_fill_data_prefers_tailored_resume_and_cover_letter():
    job_id = _seed_job("Staff SRE", "Northwind Cloud",
                       apply_url="https://northwind.example/careers/sre-9")
    _seed_tailored_resume(job_id, "TAILORED-OLD")
    newest = _seed_tailored_resume(job_id, "TAILORED-RESUME-PLAIN-TEXT v2")
    letter_id = _seed_cover_letter(job_id, "Dear Northwind, COVER-LETTER-TEXT.")
    data = _fill({"url": "https://northwind.example/careers/sre-9"})
    assert (data.get("job") or {}).get("id") == job_id
    assert data.get("resume_source") == "tailored"
    assert data.get("resume_id") == newest
    assert data.get("resume_text") == "TAILORED-RESUME-PLAIN-TEXT v2"
    assert data.get("cover_letter_id") == letter_id
    assert "COVER-LETTER-TEXT" in (data.get("cover_letter_text") or "")


def test_fill_data_falls_back_to_base_resume():
    base_id = _seed_base_resume("BASE-RESUME-RAW-TEXT", is_master=1)
    job_id = _seed_job("QA Analyst", "Fernwood Labs",
                       apply_url="https://fernwood.example/jobs/qa-1")
    data = _fill({"url": "https://fernwood.example/jobs/qa-1"})
    assert (data.get("job") or {}).get("id") == job_id
    assert data.get("resume_source") == "base"
    assert data.get("resume_id") == base_id
    assert data.get("resume_text") == "BASE-RESUME-RAW-TEXT"
    assert data.get("cover_letter_text") is None
    assert data.get("cover_letter_id") is None


def test_fill_data_no_job_match_still_returns_base_resume():
    data = _fill({"url": "https://nowhere.example/nothing"})
    assert data.get("job") is None
    assert data.get("resume_source") == "base"
    assert data.get("resume_text") == "BASE-RESUME-RAW-TEXT"


# ---------------------------------------------------------------------------
# 6. evidence-grounded answers
# ---------------------------------------------------------------------------

def test_fill_data_answers_grounded_in_claims_only():
    claim_ids = _seed_claims([
        {"claim_type": "accomplishment",
         "claim_text": "Led migration of the payment platform to Kubernetes, cutting deploy time by 80%",
         "skill": "Kubernetes", "user_verified": 1, "confidence": 0.95},
        {"claim_type": "skill", "claim_text": "postgresql",
         "skill": "PostgreSQL", "user_verified": 1, "confidence": 0.9},
        {"claim_type": "accomplishment",
         "claim_text": "FABRICATED-MARKER should never surface in answers",
         "skill": "Excel", "allowed_for_resume": 0},
    ])
    job_id = _seed_job(
        "Infrastructure Engineer", "Quillback Systems",
        apply_url="https://quillback.example/jobs/infra-3",
        description="We run Kubernetes and PostgreSQL at scale.",
    )
    data = _fill({"url": "https://quillback.example/jobs/infra-3"})
    assert (data.get("job") or {}).get("id") == job_id
    answers = data.get("answers") or {}

    summary = answers.get("experience_summary") or ""
    assert "Led migration of the payment platform" in summary
    # Bare skill tokens must not be passed off as sentences.
    assert "postgresql." not in summary.lower()

    why = answers.get("why_company") or ""
    assert "Quillback Systems" in why
    assert "Infrastructure Engineer" in why
    assert "kubernetes" in why.lower()  # documented overlap with the posting

    # Disallowed claims never leak into either answer.
    assert "FABRICATED-MARKER" not in summary
    assert "FABRICATED-MARKER" not in why

    used = answers.get("evidence_claim_ids") or []
    assert claim_ids[0] in used
    assert claim_ids[2] not in used


def test_fill_data_answers_from_company_hint_without_job_match():
    # No job in the vault for this company; why_company still composes
    # from the hint + verified claims (claims exist from the prior test).
    data = _fill({"company": "Brand New Startup Co"})
    assert data.get("job") is None
    answers = data.get("answers") or {}
    why = answers.get("why_company") or ""
    assert "Brand New Startup Co" in why
    assert "FABRICATED-MARKER" not in why
    assert (answers.get("experience_summary") or "") != ""
