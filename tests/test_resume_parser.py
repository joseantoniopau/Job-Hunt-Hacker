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


def test_parse_title_date_bullets_layout_keeps_date_out_of_company():
    """Title/Date/Bullets layout (no company line): the date line must not
    be classified as the company name."""
    text = (
        "Jane Smith\n"
        "janesmith@example.com\n"
        "\n"
        "EXPERIENCE\n"
        "Senior Engineer\n"
        "Jan 2020 - Present\n"
        "- Built things\n"
        "- Did stuff\n"
        "\n"
        "Software Engineer\n"
        "Mar 2015 - Dec 2019\n"
        "- Other things\n"
    )
    out = parse(text)
    exp = out.get("experience") or []
    assert len(exp) == 2

    first, second = exp[0], exp[1]
    assert first["title"] == "Senior Engineer"
    assert first["company"] == ""
    assert first["dates"] == "Jan 2020 - Present"
    assert first["bullets"] == ["Built things", "Did stuff"]

    assert second["title"] == "Software Engineer"
    assert second["company"] == ""
    assert second["dates"] == "Mar 2015 - Dec 2019"


def test_parse_title_company_date_layout_still_gets_company():
    """The classic Title/Company/Date layout keeps working: the company line
    is preserved and the pure date line is still excluded everywhere."""
    text = (
        "Jane Smith\n"
        "janesmith@example.com\n"
        "\n"
        "EXPERIENCE\n"
        "Senior Engineer\n"
        "Acme Corp\n"
        "Jan 2020 - Present\n"
        "- Built things\n"
    )
    out = parse(text)
    exp = out.get("experience") or []
    assert len(exp) == 1
    assert exp[0]["title"] == "Senior Engineer"
    assert exp[0]["company"] == "Acme Corp"
    assert exp[0]["dates"] == "Jan 2020 - Present"


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
