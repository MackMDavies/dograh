"""Store SignalWire credentials for provider management.

Revision ID: cd20261003
Revises: cc20261003
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "cd20261003"
down_revision: Union[str, Sequence[str], None] = "cc20261003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_signalwire_dialer_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("credentials", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("platform_signalwire_dialer_credentials")
