"""HTTP tests for the profile router (GET/PUT /api/profile + POST /api/profile/infer)."""
from __future__ import annotations

import io
import time

from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)


def test_get_profile_returns_singleton():
    r = client.get("/api/profile")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    data = body.get("data") or {}
    assert data.get("id") == 1


def test_put_profile_persists_lists():
    # Use timestamp-suffixed strings so we don't collide with anything else
    tag = f"_t{int(time.time() * 1000)}"
    titles = [f"Engineer{tag}_A", f"Engineer{tag}_B"]
    r = client.put("/api/profile", json={"target_titles": titles})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True

    r2 = client.get("/api/profile")
    assert r2.status_code == 200
    got = (r2.json().get("data") or {}).get("target_titles") or []
    assert isinstance(got, list)
    assert titles[0] in got
    assert titles[1] in got


def test_infer_returns_empty_when_nothing_supplied():
    r = client.post("/api/profile/infer")
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    # Empty draft = no inferred fields
    assert body.get("inferred_fields") == []
    # And a friendly note explaining why
    notes = body.get("notes") or []
    assert any("nothing supplied" in n.lower() for n in notes)


def test_infer_extracts_from_resume_text():
    resume = (
        "Jane Smith\n"
        "janesmith@example.com\n"
        "+1 (555) 222-3333\n"
        "\n"
        "EXPERIENCE\n"
        "Senior Software Engineer\n"
        "Acme Corp\n"
        "Jan 2020 - Present\n"
        "- Built Python services\n"
        "- Deployed on AWS\n"
        "\n"
        "SKILLS\n"
        "Python, AWS, Docker, Kubernetes\n"
    )
    files = {"resume_file": ("jane_resume.txt", io.BytesIO(resume.encode()), "text/plain")}
    r = client.post("/api/profile/infer", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    fields = set(body.get("inferred_fields") or [])
    # The fields the resume provides
    assert "name" in fields
    assert "email" in fields
    assert "target_keywords" in fields
    # And sources_used should record the resume
    used = body.get("sources_used") or []
    assert any(s.get("kind") == "resume" for s in used)
