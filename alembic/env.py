"""Alembic environment.

Pulls the SQLite path from ``backend.app.config.settings.db_path`` so
migrations always target the same database as the running app. The path
can be overridden via the standard ``-x dburl=sqlite:///alt.db`` argument:

    alembic -x dburl=sqlite:///tmp/alt.db upgrade head
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---- make `backend.app.config` importable when alembic is invoked from CWD ----
_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

config = context.config

# Set up Python logging via alembic.ini.
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # noqa: BLE001
        pass


def _resolved_db_url() -> str:
    """Resolve the SQLite URL with precedence: CLI -x dburl > settings."""
    x_args = context.get_x_argument(as_dictionary=True) or {}
    if "dburl" in x_args and x_args["dburl"]:
        return x_args["dburl"]
    try:
        from backend.app.config import settings  # late import — keeps alembic load light

        return f"sqlite:///{settings.db_path}"
    except Exception:  # noqa: BLE001 — fall back to whatever alembic.ini says
        return config.get_main_option("sqlalchemy.url") or "sqlite:///data/jhh.db"


# No declarative models are imported — JHH uses raw sqlite3, not SQLAlchemy
# ORM — so autogenerate is intentionally a no-op. Hand-authored migrations
# live under alembic/versions/.
target_metadata = None


def run_migrations_offline() -> None:
    url = _resolved_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _resolved_db_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
