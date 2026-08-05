"""add is_standing to campaigns

Revision ID: 94fd31bd5523
Revises: e2f3a4b5c6d7
Create Date: 2026-08-05

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "94fd31bd5523"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("is_standing", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "is_standing")
