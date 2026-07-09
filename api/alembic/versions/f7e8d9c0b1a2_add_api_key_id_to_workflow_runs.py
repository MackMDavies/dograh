"""add api_key_id to workflow_runs

Revision ID: f7e8d9c0b1a2
Revises: b3f7a1c2d4e5
Create Date: 2026-07-07

Adds a nullable api_key_id FK on workflow_runs so runs initiated via an API key can
be attributed to the postpaid API billing account (Phase 2b). Nullable => safe on
existing rows.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f7e8d9c0b1a2"
down_revision = "b3f7a1c2d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column("api_key_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_workflow_runs_api_key_id",
        "workflow_runs",
        "api_keys",
        ["api_key_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_workflow_runs_api_key_id", "workflow_runs", type_="foreignkey"
    )
    op.drop_column("workflow_runs", "api_key_id")
