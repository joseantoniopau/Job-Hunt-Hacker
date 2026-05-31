"""Hypothesis property tests covering the parser + text utilities.

These tests assert invariants that must hold for ANY input:
  - parsers never raise
  - return types are always the documented shapes
  - dedupe/round-trip properties hold

Property generation is capped (max_examples=50, deadline=None) so the whole
file finishes well under 30 seconds even on CI.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `backend.app...` importable when pytest is run from the repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings as hsettings, strategies as st  # noqa: E402

from backend.app.matching.salary_parser import parse_salary  # noqa: E402
from backend.app.matching.seniority_parser import (  # noqa: E402
    SENIORITY_ORDER,
    detect_seniority,
)
from backend.app.matching.location_parser import parse_location  # noqa: E402
from backend.app.matching.skills_extractor import extract_skills  # noqa: E402
from backend.app.routers.profile import _clean_title  # noqa: E402
from backend.app.utils.text import (  # noqa: E402
    dedupe_preserve_order,
    keyword_tokens,
    normalize,
    slug,
)


# Shared Hypothesis profile — keep example count low + deadlines off so the
# suite never flakes on slow CI containers.
PROFILE = hsettings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


# A reasonably broad text strategy that mixes ASCII, unicode, punctuation,
# whitespace, and short strings — exercising tricky parser inputs.
TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=200,
)


# ---------------------------------------------------------------- parse_salary
SALARY_KEYS = {"min", "max", "currency", "period"}


@PROFILE
@given(text=TEXT)
def test_parse_salary_returns_dict_with_required_keys(text: str) -> None:
    out = parse_salary(text)
    assert isinstance(out, dict)
    assert SALARY_KEYS.issubset(out.keys())
    # min/max are either None or int
    for k in ("min", "max"):
        assert out[k] is None or isinstance(out[k], int)
    # currency is always a string
    assert isinstance(out["currency"], str)
    # period is None or a known string
    assert out["period"] is None or out["period"] in {"hour", "month", "year"}


@PROFILE
@given(text=TEXT)
def test_parse_salary_never_raises(text: str) -> None:
    # Re-asserting non-raising via separate test for clearer failure mode.
    parse_salary(text)


def test_parse_salary_handles_none() -> None:
    # Explicit edge-case: None input — Hypothesis text strategy never yields None.
    out = parse_salary(None)  # type: ignore[arg-type]
    assert isinstance(out, dict)
    assert SALARY_KEYS.issubset(out.keys())


# -------------------------------------------------------------- detect_seniority
ALLOWED_LEVELS = set(SENIORITY_ORDER) | {""}


@PROFILE
@given(title=TEXT, description=TEXT)
def test_detect_seniority_returns_known_level(title: str, description: str) -> None:
    level = detect_seniority(title, description)
    assert isinstance(level, str)
    # detect_seniority defaults to "mid" so empty string is unusual but tolerated.
    assert level in ALLOWED_LEVELS


# ---------------------------------------------------------------- parse_location
LOCATION_KEYS = {"city", "region", "country", "remote", "hybrid"}


@PROFILE
@given(text=TEXT)
def test_parse_location_returns_dict(text: str) -> None:
    out = parse_location(text)
    assert isinstance(out, dict)
    assert LOCATION_KEYS.issubset(out.keys())
    assert isinstance(out["remote"], bool)
    assert isinstance(out["hybrid"], bool)
    # city/region/country are either None or strings
    for k in ("city", "region", "country"):
        assert out[k] is None or isinstance(out[k], str)


# ---------------------------------------------------------------- extract_skills
@PROFILE
@given(text=TEXT)
def test_extract_skills_no_duplicates(text: str) -> None:
    skills = extract_skills(text)
    assert isinstance(skills, list)
    # Order-preserving dedupe is part of the contract.
    assert len(skills) == len(set(skills))
    # All entries must be non-empty strings.
    for s in skills:
        assert isinstance(s, str) and s


# ---------------------------------------------------------------- _clean_title
@PROFILE
@given(raw=st.text(min_size=0, max_size=200))
def test_clean_title_length_does_not_grow(raw: str) -> None:
    cleaned = _clean_title(raw)
    assert isinstance(cleaned, str)
    # The function only strips — never appends — so length must not grow.
    assert len(cleaned) <= len(raw)


# ---------------------------------------------------------------- normalize / slug
@PROFILE
@given(text=TEXT)
def test_normalize_is_idempotent(text: str) -> None:
    once = normalize(text)
    twice = normalize(once)
    assert once == twice
    # normalize lowercases, so the output never contains uppercase letters.
    assert once == once.lower()


@PROFILE
@given(text=TEXT)
def test_slug_charset_and_length(text: str) -> None:
    s = slug(text)
    assert isinstance(s, str)
    assert len(s) <= 80
    # slug must only contain a-z, 0-9, and hyphens; no leading/trailing hyphens.
    if s:
        assert all(c.isalnum() or c == "-" for c in s)
        assert not s.startswith("-")
        assert not s.endswith("-")


@PROFILE
@given(text=TEXT)
def test_slug_is_idempotent(text: str) -> None:
    once = slug(text)
    twice = slug(once)
    assert once == twice


# ---------------------------------------------------------------- keyword_tokens
@PROFILE
@given(text=TEXT)
def test_keyword_tokens_returns_list_of_short_words(text: str) -> None:
    toks = keyword_tokens(text)
    assert isinstance(toks, list)
    for t in toks:
        assert isinstance(t, str)
        # min length 2 per implementation
        assert len(t) >= 2
        # lower-cased
        assert t == t.lower()


# ---------------------------------------------------------------- dedupe round-trip
@PROFILE
@given(items=st.lists(st.text(min_size=0, max_size=20), max_size=30))
def test_dedupe_preserve_order_is_idempotent(items: list[str]) -> None:
    first = dedupe_preserve_order(items)
    second = dedupe_preserve_order(first)
    assert first == second
    # No duplicates (case-insensitive, stripped) in the output.
    seen: set[str] = set()
    for it in first:
        k = it.strip().lower()
        assert k not in seen
        seen.add(k)
