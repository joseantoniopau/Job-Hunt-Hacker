"""Tests for the heuristic resume parser."""
from __future__ import annotations

from backend.app.services.resume_parser import parse


def test_parse_extracts_email_phone():
    text = (
        "Jane Smith\n"
        "janesmith@example.com\n"
        "+1 (555) 222-3333\n"
        "\n"
        "EXPERIENCE\n"
        "Engineer\n"
        "Acme\n"
        "2020 - Present\n"
    )
    out = parse(text)
    assert out["email"] == "janesmith@example.com"
    # phone present (allow loose formatting around the literal digits)
    digits = "".join(ch for ch in out["phone"] if ch.isdigit())
    assert "5552223333" in digits


def test_parse_extracts_experience_blocks():
    text = (
        "John Doe\n"
        "john@example.com\n"
        "\n"
        "EXPERIENCE\n"
        "Senior Engineer\n"
        "Acme Corp\n"
        "Jan 2020 - Present\n"
        "- Built things\n"
        "- Did stuff\n"
        "\n"
        "Software Engineer\n"
        "Beta Inc\n"
        "Jun 2018 - Dec 2019\n"
        "- More things\n"
    )
    out = parse(text)
    exp = out.get("experience") or []
    assert len(exp) == 2
    titles = {(e.get("title") or "").lower() for e in exp}
    companies = {(e.get("company") or "").lower() for e in exp}
    assert any("senior engineer" in t for t in titles)
    assert any("software engineer" in t for t in titles)
    assert any("acme" in c for c in companies)
    assert any("beta" in c for c in companies)


def test_parse_skills_section():
    text = (
        "John Doe\n"
        "john@example.com\n"
        "\n"
        "SKILLS\n"
        "Python, Java, JavaScript, AWS, Docker, Kubernetes\n"
    )
    out = parse(text)
    skills = out.get("skills") or []
    # Comma-separated list → at least 4 entries
    assert len(skills) >= 4
    lower = {s.lower() for s in skills}
    assert "python" in lower
    assert "aws" in lower
