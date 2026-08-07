"""add suppressed_rows to campaigns

Revision ID: c4a9b2d0e5f6
Revises: b3f8a1c9d2e4
Create Date: 2026-08-07 00:00:01.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4a9b2d0e5f6"
down_revision: Union[str, None] = "b3f8a1c9d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("suppressed_rows", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "suppressed_rows")
