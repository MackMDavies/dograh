"""Store platform Telnyx dialer credentials encrypted.

Revision ID: aa20261003
Revises: 7941208869af
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "aa20261003"
down_revision: Union[str, Sequence[str], None] = "7941208869af"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_telnyx_dialer_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("connection_id", sa.String(length=100), nullable=False),
        sa.Column("telephony_credential_id", sa.String(length=100), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("platform_telnyx_dialer_credentials")
