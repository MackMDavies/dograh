"""Store independent sales dialer Twilio credentials.

Revision ID: ef20261016
Revises: cd20261003
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "ef20261016"
down_revision: Union[str, Sequence[str], None] = "cd20261003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dialer_twilio_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_sid", sa.String(length=64), nullable=False),
        sa.Column("auth_token_encrypted", sa.Text(), nullable=False),
        sa.Column("api_key_sid", sa.String(length=64), nullable=False),
        sa.Column("api_key_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("twiml_app_sid", sa.String(length=64), nullable=False),
        sa.Column("default_caller_id", sa.String(length=32), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("dialer_twilio_credentials")
