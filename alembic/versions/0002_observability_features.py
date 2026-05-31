"""Observability + adapter-cache tables.

Adds tables introduced by the observability work:

  - ``audit_retention_state``   — per-category retention bookkeeping.
  - ``adapter_cache``           — memoizes outbound adapter fetches.
  - ``gap_event``               — records resume/skill gaps surfaced to the user.
  - ``effectiveness_event``     — measures action -> outcome ratios.

Each ``CREATE TABLE`` is wrapped in ``IF NOT EXISTS`` so this migration is
idempotent and safe to run on a database that already has those tables
created by the runtime bootstrap.

Revision ID: 0002_observability_features
Revises: 0001_initial_baseline
Create Date: 2025-06-01 00:00:01.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa  # noqa: F401  — imported for symmetry with templates


revision: str = "0002_observability_features"
down_revision: Union[str, None] = "0001_initial_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ----------------------------------------------------------------------------
# Idempotent DDL — we don't know the exact column shape Subagents A + D will
# settle on, so we provision generous, generic columns. The app's
# ``_ensure_column`` helper can layer additional columns on top at runtime
# without breaking this baseline.
# ----------------------------------------------------------------------------
_DDL = [
    # audit_retention_state
    """
    CREATE TABLE IF NOT EXISTS audit_retention_state (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        category        TEXT NOT NULL,
        last_pruned_at  REAL,
        retention_days  INTEGER,
        rows_remaining  INTEGER,
        notes           TEXT,
        created_at      REAL DEFAULT (strftime('%s','now')),
        updated_at      REAL DEFAULT (strftime('%s','now')),
        UNIQUE(category)
    )
    """,
    # adapter_cache
    """
    CREATE TABLE IF NOT EXISTS adapter_cache (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        adapter         TEXT NOT NULL,
        cache_key       TEXT NOT NULL,
        payload         BLOB,
        etag            TEXT,
        status          INTEGER,
        expires_at      REAL,
        created_at      REAL DEFAULT (strftime('%s','now')),
        updated_at      REAL DEFAULT (strftime('%s','now')),
        UNIQUE(adapter, cache_key)
    )
    """,
    # gap_event
    """
    CREATE TABLE IF NOT EXISTS gap_event (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id          TEXT,
        gap_kind        TEXT NOT NULL,
        skill           TEXT,
        severity        REAL,
        detected_at     REAL DEFAULT (strftime('%s','now')),
        resolved_at     REAL,
        metadata        TEXT
    )
    """,
    # effectiveness_event
    """
    CREATE TABLE IF NOT EXISTS effectiveness_event (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        action          TEXT NOT NULL,
        subject_id      TEXT,
        outcome         TEXT,
        score           REAL,
        occurred_at     REAL DEFAULT (strftime('%s','now')),
        metadata        TEXT
    )
    """,
]

# Useful indexes for the high-volume tables.
_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_adapter_cache_expires ON adapter_cache (expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_gap_event_job ON gap_event (job_id)",
    "CREATE INDEX IF NOT EXISTS idx_gap_event_kind ON gap_event (gap_kind)",
    "CREATE INDEX IF NOT EXISTS idx_effect_event_action ON effectiveness_event (action)",
    "CREATE INDEX IF NOT EXISTS idx_effect_event_when ON effectiveness_event (occurred_at)",
]

_DROP_INDEXES = [
    "DROP INDEX IF EXISTS idx_effect_event_when",
    "DROP INDEX IF EXISTS idx_effect_event_action",
    "DROP INDEX IF EXISTS idx_gap_event_kind",
    "DROP INDEX IF EXISTS idx_gap_event_job",
    "DROP INDEX IF EXISTS idx_adapter_cache_expires",
]

_DROP_TABLES = [
    "DROP TABLE IF EXISTS effectiveness_event",
    "DROP TABLE IF EXISTS gap_event",
    "DROP TABLE IF EXISTS adapter_cache",
    "DROP TABLE IF EXISTS audit_retention_state",
]


def upgrade() -> None:
    for stmt in _DDL:
        op.execute(stmt)
    for stmt in _INDEXES:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _DROP_INDEXES:
        op.execute(stmt)
    for stmt in _DROP_TABLES:
        op.execute(stmt)
