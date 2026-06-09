"""add pricing fields to org_available_models

Revision ID: b2c3d4e5f6a7
Revises: e1f2a3b4c5d6, c4d5e6f7a8b9
Create Date: 2026-06-03 17:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, tuple[str, ...]] = ("e1f2a3b4c5d6", "c4d5e6f7a8b9")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("org_available_models", sa.Column("cost_per_min_usd", sa.Float(), nullable=True))
    op.add_column("org_available_models", sa.Column("native_cost_display", sa.String(200), nullable=True))
    op.add_column("org_available_models", sa.Column("our_price_per_min_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("org_available_models", "our_price_per_min_usd")
    op.drop_column("org_available_models", "native_cost_display")
    op.drop_column("org_available_models", "cost_per_min_usd")
