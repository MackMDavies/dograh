"""add deactivated workflow status

Revision ID: b3f7a1c2d4e5
Revises: c7ac2f637929
Create Date: 2026-07-02

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "b3f7a1c2d4e5"
down_revision = "c7ac2f637929"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PG 12+ allows ALTER TYPE ... ADD VALUE inside a transaction as long as the
    # new value isn't *used* in the same transaction (it isn't here). Matches the
    # repo's existing enum-add migrations (e.g. 13ccd6e1f5ad_add_workflow_run_mode)
    # which run fine under the async engine — a raw COMMIT desyncs asyncpg.
    op.execute("ALTER TYPE workflow_status ADD VALUE IF NOT EXISTS 'deactivated'")


def downgrade() -> None:
    # Postgres cannot drop a single enum value; no-op (leaving the value is harmless).
    pass
