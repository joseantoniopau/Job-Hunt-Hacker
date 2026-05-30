"""Tests for text utilities — the only module guaranteed to exist."""
from backend.app.utils.text import normalize, slug, keyword_tokens, content_hash, dedupe_preserve_order, truncate


def test_normalize_basic():
    assert normalize("  Hello   WORLD  ") == "hello world"
    assert normalize("") == ""


def test_slug_basic():
    assert slug("Senior Python Engineer @ Acme!") == "senior-python-engineer-acme"


def test_keyword_tokens():
    tokens = keyword_tokens("Python, AWS, Kubernetes (k8s)")
    assert "python" in tokens
    assert "aws" in tokens
    assert "k8s" in tokens


def test_content_hash_stable():
    a = content_hash("x", "y")
    b = content_hash("x", "y")
    c = content_hash("y", "x")
    assert a == b
    assert a != c


def test_dedupe_preserve_order():
    assert dedupe_preserve_order(["a", "B", "a", "c", "b"]) == ["a", "B", "c"]


def test_truncate():
    assert truncate("hello world", 5).endswith("…")
    assert truncate("hi", 10) == "hi"
