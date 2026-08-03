"""add memory_settled_at to workflow_runs

Revision ID: e2b3c4d5f6a7
Revises: d1a2b3c4e5f6
Create Date: 2026-07-17

H4: adds a nullable memory_settled_at timestamp to workflow_runs, used by the
reconcile_memory ARQ cron to find completed runs whose post-call caller-memory
extraction never reached a terminal outcome (crash / lost job / transient webhook
failure) and re-fire it. NULL => not yet settled.

All EXISTING rows are backfilled to now() so the sweep only ever considers runs
created after this deploy — it never retroactively re-fires historical memory.
Mirrors the wallet-debit reconciliation (d1a2b3c4e5f6).
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e2b3c4d5f6a7"
down_revision = "d1a2b3c4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("memory_settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE workflow_runs SET memory_settled_at = now() "
        "WHERE memory_settled_at IS NULL"
    )
    op.create_index(
        "idx_workflow_runs_memory_unsettled",
        "workflow_runs",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("memory_settled_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_workflow_runs_memory_unsettled", table_name="workflow_runs")
    op.drop_column("workflow_runs", "memory_settled_at")
