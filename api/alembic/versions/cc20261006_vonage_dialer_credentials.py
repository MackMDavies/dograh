"""Store platform Vonage Client SDK dialer credentials.

Revision ID: cc20261006
Revises: bb20261003
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "cc20261006"
down_revision: Union[str, Sequence[str], None] = "bb20261003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_vonage_dialer_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("application_id", sa.String(length=100), nullable=False),
        sa.Column("api_key", sa.String(length=32), nullable=False),
        sa.Column("api_secret", sa.String(), nullable=True),
        sa.Column("private_key", sa.String(), nullable=False),
        sa.Column("signature_secret", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_platform_vonage_dialer_singleton"),
    )


def downgrade() -> None:
    op.drop_table("platform_vonage_dialer_credentials")
