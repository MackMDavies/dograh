"""Remove dialer-only settings from AI Voice managed Twilio accounts.

Revision ID: e26a1015c001
Revises: af741b6799bd
Create Date: 2026-10-16

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e26a1015c001"
down_revision: Union[str, None] = "af741b6799bd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("platform_twilio_credentials", "dialer_api_key_sid")
    op.drop_column("platform_twilio_credentials", "dialer_api_key_secret_encrypted")
    op.drop_column("platform_twilio_credentials", "dialer_twiml_app_sid")
    op.drop_column("platform_twilio_credentials", "dialer_default_caller_id")


def downgrade() -> None:
    op.add_column("platform_twilio_credentials", sa.Column("dialer_api_key_sid", sa.String(length=64), nullable=True))
    op.add_column("platform_twilio_credentials", sa.Column("dialer_api_key_secret_encrypted", sa.String(), nullable=True))
    op.add_column("platform_twilio_credentials", sa.Column("dialer_twiml_app_sid", sa.String(length=64), nullable=True))
    op.add_column("platform_twilio_credentials", sa.Column("dialer_default_caller_id", sa.String(length=32), nullable=True))
