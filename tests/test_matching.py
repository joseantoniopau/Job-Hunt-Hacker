"""Tests for the matching/scoring/ATS layer."""
import pytest

from backend.app.matching.skills_extractor import extract_skills
from backend.app.matching.seniority_parser import detect_seniority
from backend.app.matching.salary_parser import parse_salary
from backend.app.matching.location_parser import parse_location
from backend.app.matching.ats_analyzer import analyze_job
from backend.app.matching.scorer import default_weights


def test_extract_python_aws_docker():
    skills = extract_skills(
        "Senior Python engineer at Acme. Built with FastAPI, PostgreSQL, AWS, Docker, k8s. Mentored juniors."
    )
    s = set(skills)
    assert "Python" in s
    assert "FastAPI" in s
    assert "PostgreSQL" in s
    assert "AWS" in s
    assert "Docker" in s
    assert "Kubernetes" in s


def test_extract_does_not_substring_match():
    # "Java" should not match because the only relevant token is "JavaScript"
    skills = extract_skills("Built with JavaScript and TypeScript.")
    assert "JavaScript" in skills
    assert "TypeScript" in skills
    assert "Java" not in skills


def test_seniority_levels():
    assert detect_seniority("Staff Software Engineer") == "staff"
    assert detect_seniority("Principal Engineer") == "principal"
    assert detect_seniority("Senior Backend Engineer") == "senior"
    assert detect_seniority("Software Engineer") in ("mid", "entry")
    assert detect_seniority("Engineering Manager") == "manager"


def test_salary_basic():
    r = parse_salary("$120k - $160k USD")
    assert r["min"] == 120000
    assert r["max"] == 160000
    assert r["currency"] == "USD"


def test_salary_hourly():
    r = parse_salary("$75/hr")
    assert r["min"] is not None
    # 75 * 2080 = 156000
    assert 150000 <= r["min"] <= 160000


def test_location_remote_detected():
    r = parse_location("Remote · US Only")
    assert r["remote"] is True


def test_ats_high_risk_when_no_evidence():
    r = analyze_job({"description": "Required: Python, AWS, Kubernetes. Preferred: GraphQL."}, [])
    assert r["coverage"]["unsupported"] > 0
    assert r["ats_risk"] in ("medium", "high")


def test_default_weights_sum_to_one():
    w = default_weights()
    total = sum(w.values())
    assert abs(total - 1.0) < 0.01
