"""Developers console: API key expiry + per-request API log.

Revision ID: dv20261025a
Revises: ef20261016

* ``api_keys.expires_at`` (nullable): keys created with an expiry stop validating
  after that instant. Existing keys stay NULL = never expire.
* ``api_request_logs``: one row per request authenticated with an organisation API key
  (REST, Public Agent API, Voice workflow MCP). Written in batches by
  ``api.services.api_request_logging``; read by the Developers page; rows older than
  the retention window are purged by an ARQ cron. No request bodies, no credentials.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "dv20261025a"
down_revision: Union[str, Sequence[str], None] = "ef20261016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "api_request_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "api_key_id",
            sa.Integer(),
            sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("surface", sa.String(length=16), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("mcp_method", sa.String(length=160), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("user_agent", sa.String(length=160), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_api_request_logs_org_created",
        "api_request_logs",
        ["organization_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_api_request_logs_key_created",
        "api_request_logs",
        ["api_key_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_api_request_logs_key_created", table_name="api_request_logs")
    op.drop_index("ix_api_request_logs_org_created", table_name="api_request_logs")
    op.drop_table("api_request_logs")
    op.drop_column("api_keys", "expires_at")
