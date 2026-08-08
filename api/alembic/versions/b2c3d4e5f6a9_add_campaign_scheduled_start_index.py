"""add partial index on campaigns.scheduled_start_at

Separated from a1b2c3d4e5f7 because Postgres forbids using a
freshly-added enum value, including in an index predicate, within the
same transaction that added it. a1b2c3d4e5f7 adds the 'scheduled' value
to the campaign_state enum; this migration, running in its own
transaction afterward, is free to reference it in the index predicate.

Revision ID: b2c3d4e5f6a9
Revises: a1b2c3d4e5f7
Create Date: 2026-08-07 00:00:01.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a9"
down_revision = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_campaigns_scheduled_start",
        "campaigns",
        ["scheduled_start_at"],
        postgresql_where=sa.text("state = 'scheduled'"),
    )


def downgrade() -> None:
    op.drop_index("idx_campaigns_scheduled_start", table_name="campaigns")
