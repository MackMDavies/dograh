"""add campaign scheduling columns and 'scheduled' state

Revision ID: a1b2c3d4e5f7
Revises: a7c1e4b90d32
Create Date: 2026-08-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f7"
down_revision = "a7c1e4b90d32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("campaigns", sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("campaigns", sa.Column("scheduled_timezone", sa.String(), nullable=True))

    # New enum value. Postgres forbids *using* a freshly-added enum value
    # (including in an index predicate) within the same transaction that
    # added it, and this repo runs one migration per transaction
    # (transaction_per_migration=True in alembic/env.py). So the partial
    # index on state = 'scheduled' cannot live here — it's deliberately
    # split into the next migration (b2c3d4e5f6a9), which runs in its own
    # transaction after this one commits.
    op.execute("ALTER TYPE campaign_state ADD VALUE IF NOT EXISTS 'scheduled'")


def downgrade() -> None:
    op.drop_column("campaigns", "scheduled_timezone")
    op.drop_column("campaigns", "scheduled_start_at")
    # Postgres cannot drop a single enum value; downgrading the enum itself
    # is intentionally not attempted here.
