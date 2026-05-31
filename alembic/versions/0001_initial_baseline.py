"""Initial baseline.

The runtime app creates its schema via ``backend.app.db._init_schema``
on first launch. This migration deliberately performs *no* DDL — it just
stamps the database so subsequent migrations have a starting point.

Revision ID: 0001_initial_baseline
Revises:
Create Date: 2025-06-01 00:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

# alembic imports kept for parity with the script template even though the
# migration is a no-op.
from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401


revision: str = "0001_initial_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op. The current schema is created by ``backend.app.db._init_schema``;
    this migration just establishes a baseline that future migrations can
    chain onto. Stamping the DB with ``alembic stamp 0001_initial_baseline``
    is the recommended first step for users adopting alembic mid-stream.
    """
    pass


def downgrade() -> None:
    """No-op — see ``upgrade`` above."""
    pass
