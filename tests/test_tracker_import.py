"""Tests for POST /api/data/import-tracker (Huntr / Teal / generic CSV import).

Covers:
  * Huntr-shaped CSV  -> jobs under source='import:huntr', stage names
    (Wishlist / Applied / Phone Screen) mapped to pipeline statuses, an
    application row only when the stage implies the user applied.
  * Teal-shaped CSV   -> Role/Status/Date Applied headers mapped, applied_at
    taken from the export, offer-stage rows produce applications.
  * generic CSV       -> documented column contract, salary range parsing,
    notes round-trip.
  * duplicates        -> re-importing the same file and in-file repeats are
    counted as skipped_duplicates, never double-inserted.
  * bad rows          -> reported in errors[] (capped at 20) without aborting
    the rest of the import; unparseable dates/salaries degrade gracefully.
  * request validation -> unknown format, empty upload, >5MB upload, missing
    title column, BOM (utf-8-sig) tolerance.
  * audit             -> every import writes a data_import_tracker audit row.

conftest.py points the DB at a temp file so nothing here touches real data.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

from fastapi.testclient import TestClient

from backend.app.db import get_conn, init_db
from backend.app.main import app

init_db()
client = TestClient(app)

URL = "/api/data/import-tracker"


def _post(text: str, fmt: str, filename: str = "export.csv"):
    return client.post(
        URL,
        files={"file": (filename, text.encode("utf-8"), "text/csv")},
        data={"format": fmt},
    )


def _uniq(prefix: str) -> str:
    """Unique company names per test so the cross-source title+company dedup
    probe never collides across test cases sharing the session DB."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _job_rows(source: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM job_posting WHERE source = ? ORDER BY id", (source,)
    ).fetchall()
    return [dict(r) for r in rows]


def _apps_for_job(job_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM application WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ huntr --

def test_huntr_csv_import():
    c1, c2, c3 = _uniq("HuntrCo1"), _uniq("HuntrCo2"), _uniq("HuntrCo3")
    csv_text = (
        "Title,Company,Location,Salary,Url,List,Date Added,Description\n"
        f'Backend Engineer,{c1},Remote,"$120,000 - $150,000",'
        "https://boards.example/jobs/h1,Wishlist,2026-05-01,Great team\n"
        f"Platform Engineer,{c2},NYC,140k,"
        "https://boards.example/jobs/h2,Applied,2026-05-02,Applied via site\n"
        f"Site Reliability Engineer,{c3},SF,,"
        "https://boards.example/jobs/h3,Phone Screen,2026-05-03,\n"
    )
    r = _post(csv_text, "huntr")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["format"] == "huntr"
    assert data["total_rows"] == 3
    assert data["imported_jobs"] == 3
    assert data["imported_applications"] == 2  # Applied + Phone Screen
    assert data["skipped_duplicates"] == 0
    assert data["error_count"] == 0
    assert data["errors"] == []

    conn = get_conn()
    by_company = {}
    for c in (c1, c2, c3):
        row = conn.execute(
            "SELECT * FROM job_posting WHERE company = ?", (c,)
        ).fetchone()
        assert row is not None, f"job for {c} missing"
        by_company[c] = dict(row)

    for c in (c1, c2, c3):
        assert by_company[c]["source"] == "import:huntr"

    # salary parsed out of "$120,000 - $150,000"
    assert by_company[c1]["salary_min"] == 120000
    assert by_company[c1]["salary_max"] == 150000
    # "140k" -> 140000 (single figure: min == max)
    assert by_company[c2]["salary_min"] == 140000
    assert by_company[c2]["salary_max"] == 140000

    # Wishlist -> saved: job only, no application
    assert _apps_for_job(by_company[c1]["id"]) == []
    # Applied -> applied
    apps2 = _apps_for_job(by_company[c2]["id"])
    assert len(apps2) == 1 and apps2[0]["status"] == "applied"
    assert apps2[0]["mode"] == "import"
    assert apps2[0]["application_url"] == "https://boards.example/jobs/h2"
    # Phone Screen -> interview
    apps3 = _apps_for_job(by_company[c3]["id"])
    assert len(apps3) == 1 and apps3[0]["status"] == "interview"

    # Description landed as the job description (notes field)
    assert by_company[c1]["description"] == "Great team"
    # Date Added used for posted_at when no applied date exists
    assert by_company[c1]["posted_at"] == "2026-05-01"


# ------------------------------------------------------------------- teal --

def test_teal_csv_import():
    c1, c2, c3 = _uniq("TealCo1"), _uniq("TealCo2"), _uniq("TealCo3")
    csv_text = (
        "Company,Role,Status,URL,Location,Salary,Date Saved,Date Applied,Notes,Excitement\n"
        f"{c1},Data Engineer,Bookmarked,https://teal.example/j/1,Remote,,2026-05-01,,Looks cool,4\n"
        f'{c2},ML Engineer,Applied,https://teal.example/j/2,Austin,"$160,000",2026-05-01,05/20/2026,Submitted,5\n'
        f"{c3},Staff Engineer,Offer Received,https://teal.example/j/3,Remote,,2026-05-01,2026-05-10,Negotiating,5\n"
    )
    r = _post(csv_text, "teal")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["imported_jobs"] == 3
    assert data["imported_applications"] == 2  # Applied + Offer Received
    assert data["skipped_duplicates"] == 0
    assert data["error_count"] == 0

    conn = get_conn()
    job1 = dict(conn.execute(
        "SELECT * FROM job_posting WHERE company = ?", (c1,)).fetchone())
    job2 = dict(conn.execute(
        "SELECT * FROM job_posting WHERE company = ?", (c2,)).fetchone())
    job3 = dict(conn.execute(
        "SELECT * FROM job_posting WHERE company = ?", (c3,)).fetchone())

    assert job1["source"] == "import:teal"
    assert job1["title"] == "Data Engineer"          # Role -> title
    assert _apps_for_job(job1["id"]) == []           # Bookmarked -> saved

    apps2 = _apps_for_job(job2["id"])
    assert len(apps2) == 1 and apps2[0]["status"] == "applied"
    # applied_at parsed from "05/20/2026" (US format)
    expected = datetime.strptime("05/20/2026", "%m/%d/%Y").timestamp()
    assert abs(apps2[0]["applied_at"] - expected) < 1

    apps3 = _apps_for_job(job3["id"])
    assert len(apps3) == 1 and apps3[0]["status"] == "offer"
    expected3 = datetime.fromisoformat("2026-05-10").timestamp()
    assert abs(apps3[0]["applied_at"] - expected3) < 1
    # posted_at prefers the applied date when present
    assert job3["posted_at"] == "2026-05-10"

    assert job2["salary_min"] == 160000


def test_teal_min_max_salary_columns():
    c = _uniq("TealMinMax")
    csv_text = (
        "Company,Role,Status,Min Salary,Max Salary\n"
        f"{c},Backend Dev,Bookmarked,\"$90,000\",\"$130,000\"\n"
    )
    r = _post(csv_text, "teal")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["imported_jobs"] == 1
    job = dict(get_conn().execute(
        "SELECT * FROM job_posting WHERE company = ?", (c,)).fetchone())
    assert job["salary_min"] == 90000
    assert job["salary_max"] == 130000


# ---------------------------------------------------------------- generic --

def test_generic_csv_import():
    c1, c2 = _uniq("GenCo1"), _uniq("GenCo2")
    csv_text = (
        "title,company,url,status,applied_date,location,salary,notes\n"
        f"Frontend Dev,{c1},https://gen.example/1,applied,2026-04-15,Remote,90000-110000,note one\n"
        f"Product Designer,{c2},,saved,,LA,,\n"
    )
    r = _post(csv_text, "csv")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["format"] == "csv"
    assert data["imported_jobs"] == 2
    assert data["imported_applications"] == 1
    assert data["skipped_duplicates"] == 0
    assert data["error_count"] == 0

    conn = get_conn()
    job1 = dict(conn.execute(
        "SELECT * FROM job_posting WHERE company = ?", (c1,)).fetchone())
    job2 = dict(conn.execute(
        "SELECT * FROM job_posting WHERE company = ?", (c2,)).fetchone())
    assert job1["source"] == "import:csv"
    assert job1["salary_min"] == 90000
    assert job1["salary_max"] == 110000
    assert job1["apply_url"] == "https://gen.example/1"
    assert job1["description"] == "note one"

    apps = _apps_for_job(job1["id"])
    assert len(apps) == 1 and apps[0]["status"] == "applied"
    expected = datetime.fromisoformat("2026-04-15").timestamp()
    assert abs(apps[0]["applied_at"] - expected) < 1
    assert apps[0]["notes"] == "note one"

    # saved row: job only, no application; missing url/salary tolerated
    assert _apps_for_job(job2["id"]) == []
    assert job2["salary_min"] is None


def test_generic_alias_format_and_unknown_stage():
    """'generic' is accepted as an alias of csv; an unmappable custom stage
    degrades to saved (job imported, no application)."""
    c = _uniq("GenAlias")
    csv_text = (
        "title,company,url,status\n"
        f"QA Engineer,{c},https://gen.example/qa,Totally Custom Column\n"
    )
    r = _post(csv_text, "generic")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["format"] == "csv"
    assert data["imported_jobs"] == 1
    assert data["imported_applications"] == 0
    job = dict(get_conn().execute(
        "SELECT * FROM job_posting WHERE company = ?", (c,)).fetchone())
    assert _apps_for_job(job["id"]) == []


# -------------------------------------------------------------- duplicates --

def test_reimport_skips_duplicates():
    c1, c2 = _uniq("DupCo1"), _uniq("DupCo2")
    csv_text = (
        "title,company,url,status,applied_date,location,salary,notes\n"
        f"Backend Dev,{c1},https://dup.example/1,applied,2026-03-01,Remote,,x\n"
        f"Data Analyst,{c2},,saved,,Chicago,,\n"
    )
    r1 = _post(csv_text, "csv")
    assert r1.status_code == 200
    d1 = r1.json()["data"]
    assert d1["imported_jobs"] == 2
    assert d1["imported_applications"] == 1

    r2 = _post(csv_text, "csv")
    assert r2.status_code == 200
    d2 = r2.json()["data"]
    assert d2["imported_jobs"] == 0
    assert d2["imported_applications"] == 0
    assert d2["skipped_duplicates"] == 2
    assert d2["error_count"] == 0

    # exactly one job row each, exactly one application total
    conn = get_conn()
    n1 = conn.execute(
        "SELECT COUNT(*) FROM job_posting WHERE company = ?", (c1,)
    ).fetchone()[0]
    n2 = conn.execute(
        "SELECT COUNT(*) FROM job_posting WHERE company = ?", (c2,)
    ).fetchone()[0]
    assert n1 == 1 and n2 == 1


def test_in_file_duplicates_collapsed():
    c = _uniq("DupInFile")
    row = f"DevOps Engineer,{c},https://dup.example/infile,applied,2026-03-02,Remote,,\n"
    csv_text = (
        "title,company,url,status,applied_date,location,salary,notes\n"
        + row + row
    )
    r = _post(csv_text, "csv")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total_rows"] == 2
    assert data["imported_jobs"] == 1
    assert data["skipped_duplicates"] == 1
    assert data["imported_applications"] == 1


# ---------------------------------------------------------------- bad rows --

def test_bad_rows_reported_not_fatal():
    c1, c2 = _uniq("BadCo1"), _uniq("BadCo2")
    csv_text = (
        "title,company,url,status,applied_date,location,salary,notes\n"
        f",{c1},https://bad.example/1,applied,2026-01-01,Remote,,no title here\n"
        f"Good Job,{c2},https://bad.example/2,applied,not-a-date,Remote,garbage salary,ok\n"
    )
    r = _post(csv_text, "csv")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total_rows"] == 2
    assert data["imported_jobs"] == 1            # the good row survives
    assert data["imported_applications"] == 1
    assert data["error_count"] == 1
    assert len(data["errors"]) == 1
    assert "missing title" in data["errors"][0]
    assert "row 2" in data["errors"][0]

    # unparseable date/salary degrade: applied_at ~ now, salary NULL
    job = dict(get_conn().execute(
        "SELECT * FROM job_posting WHERE company = ?", (c2,)).fetchone())
    assert job["salary_min"] is None
    apps = _apps_for_job(job["id"])
    assert len(apps) == 1
    assert abs(apps[0]["applied_at"] - time.time()) < 60


def test_error_list_capped_at_20():
    bad_rows = "".join(
        f",NoTitleCo{i},,saved,,,,\n" for i in range(25)
    )
    csv_text = "title,company,url,status,applied_date,location,salary,notes\n" + bad_rows
    r = _post(csv_text, "csv")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["error_count"] == 25
    assert len(data["errors"]) == 20
    assert data["imported_jobs"] == 0


# -------------------------------------------------------------- validation --

def test_unknown_format_rejected():
    r = _post("title,company\nA,B\n", "monster")
    assert r.status_code == 400
    assert "unknown format" in r.json()["detail"]


def test_empty_upload_rejected():
    r = _post("", "csv")
    assert r.status_code == 400


def test_oversize_upload_rejected():
    big = "title,company\n" + ("a" * (5 * 1024 * 1024))
    r = _post(big, "csv")
    assert r.status_code == 413


def test_missing_title_column_rejected():
    r = _post("company,url\nAcme,https://x.example\n", "csv")
    assert r.status_code == 400
    assert "title" in r.json()["detail"].lower()


def test_utf8_sig_bom_tolerated():
    c = _uniq("BomCo")
    csv_text = (
        "\ufeff" + "title,company,url,status\n"
        f"Engineer,{c},https://bom.example/1,applied\n"
    )
    r = _post(csv_text, "csv", filename="bom.csv")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["imported_jobs"] == 1
    assert data["imported_applications"] == 1
    job = get_conn().execute(
        "SELECT * FROM job_posting WHERE company = ?", (c,)).fetchone()
    assert job is not None


# ------------------------------------------------------------------- audit --

def test_import_is_audited():
    c = _uniq("AuditCo")
    csv_text = (
        "title,company,url,status\n"
        f"Auditor,{c},https://audit.example/1,applied\n"
    )
    r = _post(csv_text, "csv")
    assert r.status_code == 200
    row = get_conn().execute(
        "SELECT * FROM audit_log WHERE action = 'data_import_tracker' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    detail = json.loads(row["detail_json"])
    assert detail["format"] == "csv"
    assert detail["imported_jobs"] >= 1
