"""add org provider connections and available models

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-05-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "org_provider_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("service_type", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("extra_config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "service_type", "provider", name="uq_org_provider_service"),
    )
    op.create_index("ix_org_provider_connections_org", "org_provider_connections", ["organization_id"])
    op.create_index("ix_org_provider_connections_service_type", "org_provider_connections", ["service_type"])

    op.create_table(
        "org_available_models",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("service_type", sa.String(20), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("is_client_available", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["org_provider_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "model_id", name="uq_connection_model"),
    )
    op.create_index("ix_org_available_models_org_service", "org_available_models", ["organization_id", "service_type"])
    op.create_index("ix_org_available_models_client", "org_available_models", ["organization_id", "service_type", "is_client_available"])


def downgrade() -> None:
    op.drop_index("ix_org_available_models_client", table_name="org_available_models")
    op.drop_index("ix_org_available_models_org_service", table_name="org_available_models")
    op.drop_table("org_available_models")
    op.drop_index("ix_org_provider_connections_service_type", table_name="org_provider_connections")
    op.drop_index("ix_org_provider_connections_org", table_name="org_provider_connections")
    op.drop_table("org_provider_connections")
