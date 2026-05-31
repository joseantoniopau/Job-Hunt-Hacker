"""Smoke tests that prove the new quality / testing infra is wired up.

These tests don't exercise behavior — they just make sure the new files
import cleanly, parse cleanly, and would be discoverable by their
respective tools (locust, alembic, pytest).
"""
from __future__ import annotations

import configparser
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_locustfile_imports() -> None:
    """The locustfile must import cleanly even when locust is not installed.

    The module guards its `from locust import ...` so a fresh dev box can
    still load the file (e.g. for editor / linter introspection).
    """
    locustfile = ROOT / "tests" / "load" / "locustfile.py"
    assert locustfile.exists(), f"missing {locustfile}"

    spec = importlib.util.spec_from_file_location(
        "tests.load.locustfile", str(locustfile)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    for cls_name in ("BrowsingUser", "SearchUser", "TailorUser"):
        assert hasattr(module, cls_name), f"locustfile missing class {cls_name}"


def test_load_readme_present() -> None:
    readme = ROOT / "tests" / "load" / "README.md"
    assert readme.exists()
    body = readme.read_text()
    # Must mention the install command + the basic locust invocation.
    assert "pip install locust" in body
    assert "locust" in body and "--host" in body


def test_alembic_config_present() -> None:
    cfg_path = ROOT / "alembic.ini"
    assert cfg_path.exists()

    parser = configparser.ConfigParser()
    parser.read(cfg_path)
    assert parser.has_section("alembic"), "alembic.ini missing [alembic] section"
    assert parser.get("alembic", "script_location"), "script_location not set"

    # Versions directory + the two baseline migrations are present.
    versions_dir = ROOT / "alembic" / "versions"
    assert versions_dir.is_dir()
    assert (versions_dir / "0001_initial_baseline.py").exists()
    assert (versions_dir / "0002_observability_features.py").exists()


def test_alembic_config_loadable_via_alembic_api() -> None:
    """If alembic is installed, the config should parse via its own API."""
    alembic_config = pytest.importorskip("alembic.config")
    cfg = alembic_config.Config(str(ROOT / "alembic.ini"))
    assert cfg.get_main_option("script_location") == "alembic"


def test_alembic_versions_import_cleanly() -> None:
    """Each migration module must at least parse + import."""
    pytest.importorskip("alembic")
    versions_dir = ROOT / "alembic" / "versions"
    for path in sorted(versions_dir.glob("0*.py")):
        spec = importlib.util.spec_from_file_location(
            f"alembic_versions_{path.stem}", str(path)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        assert hasattr(module, "upgrade")
        assert hasattr(module, "downgrade")


def test_pyproject_present() -> None:
    pyproj = ROOT / "pyproject.toml"
    assert pyproj.exists()

    # Python 3.11+ ships tomllib; fall back to a tolerant parse otherwise.
    try:
        import tomllib  # type: ignore[attr-defined]

        data = tomllib.loads(pyproj.read_text())
    except Exception:  # noqa: BLE001
        data = None

    if data is not None:
        assert "tool" in data
        for key in ("ruff", "mypy", "pytest"):
            assert key in data["tool"], f"pyproject.toml missing [tool.{key}]"
    else:
        body = pyproj.read_text()
        for needle in ("[tool.ruff]", "[tool.mypy]", "[tool.pytest.ini_options]"):
            assert needle in body
