"""JD change tracking + application deadlines + notifications (Task G1).

Covers:
  * job_posting_snapshot baseline + change detection (helper + endpoints)
  * snapshot recorded ONLY when the description actually changed
  * posting_changed flag in the jobs list/detail responses
  * deadline_at PATCH parsing (epoch, ISO, clear, garbage -> 400)
  * deadline reminder scheduler job fires exactly once per deadline
  * notifications list / unread filter / mark-read
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.db import get_conn, init_db, tx
from backend.app.integrations import scheduler as sched
from backend.app.routers import applications as applications_router
from backend.app.routers import jobs as jobs_router
from backend.app.routers.jobs import snapshot_job_if_changed


DESC_A = (
    "We are hiring a Senior Python Engineer. You will build FastAPI services, "
    "design SQLite schemas and maintain APScheduler pipelines."
)
DESC_B = (
    "We are hiring a Senior Python Engineer. You will build FastAPI services, "
    "design PostgreSQL schemas, maintain Kubernetes deployments and mentor juniors."
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    init_db()
    app = FastAPI()
    app.include_router(jobs_router.router)
    app.include_router(applications_router.router)
    return TestClient(app)


def _mk_job(description: str, title: str = "Engineer", company: str = "Acme") -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO job_posting (source, title, company, description, "
            "discovered_at, status) VALUES ('test', ?, ?, ?, ?, 'new')",
            (title, company, description, time.time()),
        )
        return int(cur.lastrowid)


def _mk_application(client: TestClient, job_id: int) -> int:
    r = client.post("/api/applications", json={"job_id": job_id})
    assert r.status_code == 200, r.text
    return int(r.json()["data"]["id"])


def _app_row(app_id: int) -> dict:
    row = get_conn().execute(
        "SELECT * FROM application WHERE id = ?", (app_id,)
    ).fetchone()
    assert row is not None
    return dict(row)


def _snapshot_count(job_id: int) -> int:
    return int(get_conn().execute(
        "SELECT COUNT(*) FROM job_posting_snapshot WHERE job_id = ?", (job_id,)
    ).fetchone()[0])


# --------------------------------------------------------------- snapshots --

def test_first_check_creates_baseline_only(client: TestClient) -> None:
    job_id = _mk_job(DESC_A)
    r = client.post(f"/api/jobs/{job_id}/snapshot-check")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["changed"] is False
    assert data["baseline_created"] is True
    assert data["snapshot_id"] is not None
    assert _snapshot_count(job_id) == 1

    # Unchanged description -> no new snapshot on repeat checks.
    r2 = client.post(f"/api/jobs/{job_id}/snapshot-check")
    assert r2.status_code == 200
    assert r2.json()["data"]["changed"] is False
    assert r2.json()["data"]["baseline_created"] is False
    assert _snapshot_count(job_id) == 1


def test_changed_description_records_snapshot_with_diff(client: TestClient) -> None:
    job_id = _mk_job(DESC_A)
    client.post(f"/api/jobs/{job_id}/snapshot-check")  # baseline
    r = client.post(
        f"/api/jobs/{job_id}/snapshot-check", json={"description": DESC_B}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["changed"] is True
    assert _snapshot_count(job_id) == 2

    summary = data["change_summary"]
    assert summary["chars_added"] == max(0, len(DESC_B) - len(DESC_A))
    # words >4 chars that only appear in one side
    assert "postgresql" in summary["added_keywords"]
    assert "kubernetes" in summary["added_keywords"]
    assert "sqlite" in summary["removed_keywords"]
    assert "apscheduler" in summary["removed_keywords"]

    # The job row's description was updated to the new text.
    row = get_conn().execute(
        "SELECT description FROM job_posting WHERE id = ?", (job_id,)
    ).fetchone()
    assert row["description"] == DESC_B

    # Same description again -> no third snapshot.
    r3 = client.post(
        f"/api/jobs/{job_id}/snapshot-check", json={"description": DESC_B}
    )
    assert r3.json()["data"]["changed"] is False
    assert _snapshot_count(job_id) == 2


def test_snapshot_check_detects_row_drift(client: TestClient) -> None:
    """No body: the endpoint compares the CURRENT row against the latest
    snapshot — catches description updates made by other code paths."""
    job_id = _mk_job(DESC_A)
    client.post(f"/api/jobs/{job_id}/snapshot-check")  # baseline
    with tx() as c:
        c.execute(
            "UPDATE job_posting SET description = ? WHERE id = ?", (DESC_B, job_id)
        )
    r = client.post(f"/api/jobs/{job_id}/snapshot-check")
    assert r.status_code == 200
    assert r.json()["data"]["changed"] is True
    assert _snapshot_count(job_id) == 2


def test_snapshot_helper_direct_and_missing_job(client: TestClient) -> None:
    job_id = _mk_job(DESC_A)
    out = snapshot_job_if_changed(job_id)  # baseline
    assert out["baseline_created"] is True and out["changed"] is False
    out2 = snapshot_job_if_changed(job_id, DESC_A)  # unchanged
    assert out2["changed"] is False
    assert _snapshot_count(job_id) == 1
    with pytest.raises(LookupError):
        snapshot_job_if_changed(99999999)
    assert client.post("/api/jobs/99999999/snapshot-check").status_code == 404
    assert client.get("/api/jobs/99999999/snapshots").status_code == 404


def test_snapshots_listing_shape(client: TestClient) -> None:
    job_id = _mk_job(DESC_A)
    client.post(f"/api/jobs/{job_id}/snapshot-check")
    client.post(f"/api/jobs/{job_id}/snapshot-check", json={"description": DESC_B})
    r = client.get(f"/api/jobs/{job_id}/snapshots")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    newest, oldest = body["data"]
    assert newest["initial"] is False and oldest["initial"] is True
    assert newest["captured_at"] >= oldest["captured_at"]
    assert isinstance(newest["change_summary"], dict)
    assert newest["description"] == DESC_B
    assert oldest["description"] == DESC_A
    assert newest["content_hash"] != oldest["content_hash"]


# -------------------------------------------------------- posting_changed --

def test_posting_changed_flag_in_jobs_list(client: TestClient) -> None:
    changed_job = _mk_job(DESC_A, title="Changed Role", company="ChangedCo")
    stable_job = _mk_job(DESC_A, title="Stable Role", company="StableCo")
    no_app_job = _mk_job(DESC_A, title="NoApp Role", company="NoAppCo")

    # Baselines first, then the user saves applications.
    for jid in (changed_job, stable_job, no_app_job):
        client.post(f"/api/jobs/{jid}/snapshot-check")
    _mk_application(client, changed_job)
    _mk_application(client, stable_job)

    time.sleep(0.05)  # snapshot must land strictly after the application ts
    client.post(
        f"/api/jobs/{changed_job}/snapshot-check", json={"description": DESC_B}
    )

    r = client.get("/api/jobs", params={"limit": 500})
    assert r.status_code == 200
    by_id = {row["id"]: row for row in r.json()["data"]}
    assert by_id[changed_job]["posting_changed"] is True
    assert by_id[stable_job]["posting_changed"] is False
    assert by_id[no_app_job]["posting_changed"] is False

    # Same flag on the single-job endpoint.
    r1 = client.get(f"/api/jobs/{changed_job}")
    assert r1.json()["data"]["posting_changed"] is True


# ------------------------------------------------------------- deadlines --

def test_patch_deadline_epoch_and_default_source(client: TestClient) -> None:
    job_id = _mk_job(DESC_A)
    app_id = _mk_application(client, job_id)
    deadline = time.time() + 7 * 86400
    r = client.patch(f"/api/applications/{app_id}", json={"deadline_at": deadline})
    assert r.status_code == 200
    row = _app_row(app_id)
    assert row["deadline_at"] == pytest.approx(deadline, abs=1.0)
    assert row["deadline_source"] == "manual"
    assert row["reminder_sent_at"] is None


def test_patch_deadline_iso_and_explicit_source(client: TestClient) -> None:
    job_id = _mk_job(DESC_A)
    app_id = _mk_application(client, job_id)
    r = client.patch(
        f"/api/applications/{app_id}",
        json={"deadline_at": "2030-01-01T00:00:00Z", "deadline_source": "jd"},
    )
    assert r.status_code == 200
    expected = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
    row = _app_row(app_id)
    assert row["deadline_at"] == pytest.approx(expected, abs=1.0)
    assert row["deadline_source"] == "jd"

    # Other ApplicationUpdate fields still flow through alongside deadlines.
    r2 = client.patch(
        f"/api/applications/{app_id}",
        json={"status": "applied", "deadline_at": "2030-06-01"},
    )
    assert r2.status_code == 200
    assert r2.json()["data"]["status"] == "applied"
    assert _app_row(app_id)["deadline_at"] == pytest.approx(
        datetime(2030, 6, 1, tzinfo=timezone.utc).timestamp(), abs=1.0
    )


def test_patch_deadline_clear_and_invalid(client: TestClient) -> None:
    job_id = _mk_job(DESC_A)
    app_id = _mk_application(client, job_id)
    client.patch(f"/api/applications/{app_id}", json={"deadline_at": time.time() + 100})
    r = client.patch(f"/api/applications/{app_id}", json={"deadline_at": ""})
    assert r.status_code == 200
    row = _app_row(app_id)
    assert row["deadline_at"] is None
    assert row["deadline_source"] is None

    r_bad = client.patch(
        f"/api/applications/{app_id}", json={"deadline_at": "not-a-date"}
    )
    assert r_bad.status_code == 400

    r_missing = client.patch(
        "/api/applications/99999999", json={"deadline_at": time.time() + 100}
    )
    assert r_missing.status_code == 404


# ---------------------------------------------------- reminder scheduler --

def test_deadline_reminder_fires_once(client: TestClient) -> None:
    job_id = _mk_job(DESC_A, title="Deadline Role", company="DeadlineCo")
    app_id = _mk_application(client, job_id)
    soon = time.time() + 2 * 3600  # inside the 48h window
    client.patch(f"/api/applications/{app_id}", json={"deadline_at": soon})

    out = sched.run_deadline_reminders()
    assert out["ok"] is True
    assert app_id in out["reminded"]
    assert _app_row(app_id)["reminder_sent_at"] is not None

    # Second sweep: reminder_sent_at is stamped -> nothing fires again.
    out2 = sched.run_deadline_reminders()
    assert app_id not in out2["reminded"]
    n = get_conn().execute(
        "SELECT COUNT(*) FROM notification WHERE kind = 'deadline_reminder' "
        "AND target_type = 'application' AND target_id = ?",
        (app_id,),
    ).fetchone()[0]
    assert int(n) == 1

    # The notification carries job context and an audit entry exists.
    note = get_conn().execute(
        "SELECT * FROM notification WHERE target_id = ? AND kind = 'deadline_reminder'",
        (app_id,),
    ).fetchone()
    assert "DeadlineCo" in note["title"]
    assert note["read"] == 0
    a = get_conn().execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'deadline_reminder' "
        "AND target_id = ?",
        (app_id,),
    ).fetchone()[0]
    assert int(a) >= 1


def test_deadline_outside_window_or_archived_not_reminded(client: TestClient) -> None:
    far_job = _mk_job(DESC_A)
    far_app = _mk_application(client, far_job)
    client.patch(
        f"/api/applications/{far_app}",
        json={"deadline_at": time.time() + 100 * 3600},  # > 48h away
    )
    dead_job = _mk_job(DESC_A)
    dead_app = _mk_application(client, dead_job)
    client.patch(
        f"/api/applications/{dead_app}",
        json={"deadline_at": time.time() + 3600, "status": "rejected"},
    )
    out = sched.run_deadline_reminders()
    assert far_app not in out["reminded"]
    assert dead_app not in out["reminded"]
    assert _app_row(far_app)["reminder_sent_at"] is None


def test_new_deadline_rearms_reminder(client: TestClient) -> None:
    job_id = _mk_job(DESC_A)
    app_id = _mk_application(client, job_id)
    client.patch(f"/api/applications/{app_id}", json={"deadline_at": time.time() + 3600})
    sched.run_deadline_reminders()
    assert _app_row(app_id)["reminder_sent_at"] is not None
    # PATCHing a new deadline resets reminder_sent_at -> fires again.
    client.patch(f"/api/applications/{app_id}", json={"deadline_at": time.time() + 7200})
    assert _app_row(app_id)["reminder_sent_at"] is None
    out = sched.run_deadline_reminders()
    assert app_id in out["reminded"]


def test_register_deadline_reminders_is_safe() -> None:
    # Returns a bool either way (False when APScheduler is unavailable),
    # and never raises.
    assert sched.register_deadline_reminders() in (True, False)


# ---------------------------------------------------------- notifications --

def test_notifications_list_and_mark_read(client: TestClient) -> None:
    job_id = _mk_job(DESC_A, title="Notify Role", company="NotifyCo")
    app_id = _mk_application(client, job_id)
    client.patch(f"/api/applications/{app_id}", json={"deadline_at": time.time() + 3600})
    sched.run_deadline_reminders()

    r = client.get("/api/notifications", params={"limit": 200})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["unread_count"] >= 1
    mine = [n for n in body["data"]
            if n["target_type"] == "application" and n["target_id"] == app_id]
    assert len(mine) == 1
    note = mine[0]
    assert note["kind"] == "deadline_reminder"
    assert note["read"] == 0
    assert "NotifyCo" in note["title"]

    # kind filter + unread filter include it
    r_kind = client.get(
        "/api/notifications",
        params={"kind": "deadline_reminder", "unread_only": True, "limit": 200},
    )
    assert any(n["id"] == note["id"] for n in r_kind.json()["data"])

    # mark read -> unread list no longer contains it
    r_read = client.post(f"/api/notifications/{note['id']}/read")
    assert r_read.status_code == 200
    r_unread = client.get(
        "/api/notifications", params={"unread_only": True, "limit": 200}
    )
    assert all(n["id"] != note["id"] for n in r_unread.json()["data"])
    row = get_conn().execute(
        "SELECT read FROM notification WHERE id = ?", (note["id"],)
    ).fetchone()
    assert row["read"] == 1

    # idempotent re-read + 404 on unknown id
    assert client.post(f"/api/notifications/{note['id']}/read").status_code == 200
    assert client.post("/api/notifications/99999999/read").status_code == 404
