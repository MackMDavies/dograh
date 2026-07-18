"""add wallet_debit_settled_at to workflow_runs

Revision ID: d1a2b3c4e5f6
Revises: f7e8d9c0b1a2
Create Date: 2026-07-17

Adds a nullable wallet_debit_settled_at timestamp to workflow_runs, used by the
reconcile_wallet_debits ARQ cron to find completed wallet runs whose post-call
debit never reached a terminal outcome (crash / lost job / transient webhook
failure) and re-fire it. NULL => not yet settled.

All EXISTING rows are backfilled to now() so the sweep only ever considers runs
created after this deploy — it never retroactively (re-)charges historical calls.
A partial index on (created_at) WHERE wallet_debit_settled_at IS NULL keeps the
sweep query cheap once the backlog is settled.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d1a2b3c4e5f6"
down_revision = "f7e8d9c0b1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("wallet_debit_settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: treat every pre-existing run as already settled so the reconciliation
    # sweep starts from a clean slate and only reconciles runs created from here on.
    op.execute(
        "UPDATE workflow_runs SET wallet_debit_settled_at = now() "
        "WHERE wallet_debit_settled_at IS NULL"
    )
    op.create_index(
        "idx_workflow_runs_wallet_unsettled",
        "workflow_runs",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("wallet_debit_settled_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_workflow_runs_wallet_unsettled", table_name="workflow_runs")
    op.drop_column("workflow_runs", "wallet_debit_settled_at")
