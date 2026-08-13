"""add label to platform twilio credentials

Revision ID: 7941208869af
Revises: ba439d389e3d
Create Date: 2026-08-11 04:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7941208869af'
down_revision: Union[str, None] = 'ba439d389e3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "platform_twilio_credentials",
        sa.Column("label", sa.String(120), nullable=True),
    )
    op.add_column(
        "platform_twilio_credentials",
        sa.Column("dialer_api_key_sid", sa.String(64), nullable=True),
    )
    op.add_column(
        "platform_twilio_credentials",
        sa.Column("dialer_api_key_secret_encrypted", sa.String(), nullable=True),
    )
    op.add_column(
        "platform_twilio_credentials",
        sa.Column("dialer_twiml_app_sid", sa.String(64), nullable=True),
    )
    op.add_column(
        "platform_twilio_credentials",
        sa.Column("dialer_default_caller_id", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_twilio_credentials", "dialer_default_caller_id")
    op.drop_column("platform_twilio_credentials", "dialer_twiml_app_sid")
    op.drop_column("platform_twilio_credentials", "dialer_api_key_secret_encrypted")
    op.drop_column("platform_twilio_credentials", "dialer_api_key_sid")
    op.drop_column("platform_twilio_credentials", "label")
