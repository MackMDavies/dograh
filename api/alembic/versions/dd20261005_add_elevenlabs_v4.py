"""Add ElevenLabs v4 for existing provider connections.

Revision ID: dd20261005
Revises: bb20261003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "dd20261005"
down_revision: Union[str, Sequence[str], None] = "bb20261003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO org_available_models
              (connection_id, organization_id, service_type, model_id,
               is_client_available, is_default, cost_per_min_usd, native_cost_display)
            SELECT c.id, c.organization_id, 'tts', 'eleven_v4', true, false,
                   0.000045, '$0.09/M characters'
            FROM org_provider_connections c
            WHERE c.provider = 'elevenlabs' AND c.service_type = 'tts'
              AND NOT EXISTS (
                SELECT 1 FROM org_available_models m
                WHERE m.connection_id = c.id AND m.model_id = 'eleven_v4'
              )
            """
        )
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            DELETE FROM org_available_models
            WHERE model_id = 'eleven_v4'
              AND connection_id IN (
                SELECT id FROM org_provider_connections
                WHERE provider = 'elevenlabs' AND service_type = 'tts'
              )
            """
        )
    )
