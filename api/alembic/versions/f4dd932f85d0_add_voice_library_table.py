"""add voice library table

Revision ID: f4dd932f85d0
Revises: 6bd9f67ec994
Create Date: 2026-05-28 22:49:00.368350

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f4dd932f85d0'
down_revision: Union[str, None] = '6bd9f67ec994'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "voice_library",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("uuid", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_voice_id", sa.String(255), nullable=True),
        sa.Column("language", sa.String(50), nullable=True),
        sa.Column("accent", sa.String(100), nullable=True),
        sa.Column("gender", sa.String(20), nullable=True),
        sa.Column("age", sa.String(20), nullable=True),
        sa.Column("use_case", sa.String(50), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("audio_preview_url", sa.Text(), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_voice_library_id"), "voice_library", ["id"], unique=False)
    op.create_index(op.f("ix_voice_library_uuid"), "voice_library", ["uuid"], unique=True)
    op.create_index("ix_voice_library_org_public", "voice_library", ["organization_id", "is_public"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_voice_library_org_public", table_name="voice_library")
    op.drop_index(op.f("ix_voice_library_uuid"), table_name="voice_library")
    op.drop_index(op.f("ix_voice_library_id"), table_name="voice_library")
    op.drop_table("voice_library")
