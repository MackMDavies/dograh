"""Store Telnyx WebRTC credentials per dialer user.

Revision ID: bb20261003
Revises: aa20261003
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "bb20261003"
down_revision: Union[str, Sequence[str], None] = "aa20261003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_telnyx_user_credentials",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("connection_id", sa.String(length=100), nullable=False),
        sa.Column("telephony_credential_id", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "telephony_credential_id",
            name="uq_platform_telnyx_user_credentials_credential_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("platform_telnyx_user_credentials")
