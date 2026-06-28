"""add platform twilio credentials

Revision ID: b7f3a1c2d9e4
Revises: f4dd932f85d0
Create Date: 2026-06-28 01:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b7f3a1c2d9e4'
down_revision: Union[str, None] = 'f4dd932f85d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_twilio_credentials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_sid", sa.String(64), nullable=False),
        sa.Column("auth_token_encrypted", sa.String(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_platform_twilio_credentials_id"),
        "platform_twilio_credentials",
        ["id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_platform_twilio_credentials_id"),
        table_name="platform_twilio_credentials",
    )
    op.drop_table("platform_twilio_credentials")
