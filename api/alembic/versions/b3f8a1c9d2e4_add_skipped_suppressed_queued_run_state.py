"""add skipped_suppressed state to queued runs

Revision ID: b3f8a1c9d2e4
Revises: a7c1e4b90d32
Create Date: 2026-08-07 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "b3f8a1c9d2e4"
down_revision: Union[str, None] = "a7c1e4b90d32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE queued_run_state ADD VALUE IF NOT EXISTS 'skipped_suppressed'")


def downgrade() -> None:
    # Postgres cannot drop a single enum value without rebuilding the type;
    # matches the existing precedent (f952c9c1105a) of leaving this a no-op.
    pass
