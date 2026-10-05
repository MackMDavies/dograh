"""Store the platform Vonage account used by number management.

Revision ID: cc20261003
Revises: dd20261005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "cc20261003"
down_revision: Union[str, Sequence[str], None] = "dd20261005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_vonage_dialer_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("credentials", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("platform_vonage_dialer_credentials")
