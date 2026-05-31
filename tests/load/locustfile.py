"""Locust load-test scaffold for the Job Hunt Hacker API.

Run against a locally-running server (default http://127.0.0.1:8731):

    pip install locust
    locust -f tests/load/locustfile.py --host=http://127.0.0.1:8731 \\
        -u 50 -r 5 -t 2m

Three user shapes are mixed:
  - BrowsingUser   (weight 4) — read-mostly traffic against dashboards.
  - SearchUser     (weight 2) — periodic POSTs to /api/search.
  - TailorUser     (weight 1) — the expensive LLM path.
"""
from __future__ import annotations

import random

# Locust is optional; importing should still succeed when locust is not
# installed so the module can be smoke-imported in CI.
try:
    from locust import HttpUser, between, constant_pacing, task
except Exception:  # noqa: BLE001 — locust missing in unit-test env is ok
    HttpUser = object  # type: ignore[assignment, misc]

    def task(*a, **kw):  # type: ignore[no-redef]
        def deco(fn):
            return fn

        # Allow @task and @task(weight=...)
        if a and callable(a[0]) and not kw:
            return a[0]
        return deco

    def between(*a, **kw):  # type: ignore[no-redef]
        return None

    def constant_pacing(*a, **kw):  # type: ignore[no-redef]
        return None


# ---------------- realistic search payloads ----------------
SEARCH_QUERIES = [
    {
        "query": "staff backend engineer",
        "location": "Remote, US",
        "limit": 25,
    },
    {
        "query": "senior product manager",
        "location": "San Francisco, CA",
        "limit": 25,
    },
    {
        "query": "platform engineer kubernetes",
        "location": "Remote",
        "limit": 25,
    },
    {
        "query": "ml engineer",
        "location": "New York, NY",
        "limit": 25,
    },
]

TAILOR_PAYLOADS = [
    {
        "job_id": "demo-job-1",
        "resume_id": "default",
        "tone": "confident",
    },
    {
        "job_id": "demo-job-2",
        "resume_id": "default",
        "tone": "concise",
    },
]


class BrowsingUser(HttpUser):
    """Simulates a user clicking around the dashboard."""

    weight = 4
    wait_time = between(2, 6)

    @task(3)
    def health(self) -> None:
        self.client.get("/api/health", name="GET /api/health")

    @task(2)
    def stats(self) -> None:
        self.client.get("/api/stats", name="GET /api/stats")

    @task(2)
    def jobs(self) -> None:
        self.client.get("/api/jobs", name="GET /api/jobs")

    @task(2)
    def applications_board(self) -> None:
        self.client.get(
            "/api/applications/board",
            name="GET /api/applications/board",
        )

    @task(1)
    def settings(self) -> None:
        self.client.get("/api/settings", name="GET /api/settings")

    @task(1)
    def vault_summary(self) -> None:
        self.client.get(
            "/api/vault/summary",
            name="GET /api/vault/summary",
        )


class SearchUser(HttpUser):
    """Periodically issues a job-board search."""

    weight = 2
    # constant_pacing — fires task every 30s regardless of response time.
    wait_time = constant_pacing(30)

    @task
    def search(self) -> None:
        payload = random.choice(SEARCH_QUERIES)
        self.client.post("/api/search", json=payload, name="POST /api/search")


class TailorUser(HttpUser):
    """Periodically requests a resume tailor — the most expensive path."""

    weight = 1
    wait_time = constant_pacing(60)

    @task
    def tailor(self) -> None:
        payload = random.choice(TAILOR_PAYLOADS)
        self.client.post(
            "/api/resume/tailor",
            json=payload,
            name="POST /api/resume/tailor",
        )
