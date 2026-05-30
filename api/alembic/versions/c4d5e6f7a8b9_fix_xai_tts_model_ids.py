"""fix xai tts model ids

Revision ID: c4d5e6f7a8b9
Revises: b3c1d2e4f5a6
Create Date: 2026-05-30 02:00:00.000000

xAI TTS models were incorrectly seeded as voice IDs ("eve", "ara", "rex", "sal", "leo").
The correct TTS model ID is "grok-voice-latest". This migration removes the wrong entries
and inserts the correct one for every org that has an xAI TTS connection.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c1d2e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_XAI_TTS_WRONG_IDS = ("eve", "ara", "rex", "sal", "leo")


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Find all xAI TTS connections (we'll need connection_id to re-insert)
    xai_tts_connections = conn.execute(
        sa.text(
            "SELECT id, organization_id FROM org_provider_connections "
            "WHERE provider = 'xai' AND service_type = 'tts' AND is_active = true"
        )
    ).fetchall()

    # 2. Delete the wrong voice-ID-as-model-ID entries for xAI TTS
    conn.execute(
        sa.text(
            """
            DELETE FROM org_available_models
            WHERE model_id IN :wrong_ids
              AND connection_id IN (
                SELECT id FROM org_provider_connections
                WHERE provider = 'xai' AND service_type = 'tts'
              )
            """
        ).bindparams(sa.bindparam("wrong_ids", expanding=True)),
        {"wrong_ids": list(_XAI_TTS_WRONG_IDS)},
    )

    # 3. Insert the correct "grok-voice-latest" entry for each xAI TTS connection
    for row in xai_tts_connections:
        connection_id, organization_id = row[0], row[1]
        existing = conn.execute(
            sa.text(
                "SELECT id FROM org_available_models "
                "WHERE connection_id = :cid AND model_id = 'grok-voice-latest'"
            ),
            {"cid": connection_id},
        ).fetchone()
        if existing:
            continue
        conn.execute(
            sa.text(
                """
                INSERT INTO org_available_models
                  (connection_id, organization_id, service_type, model_id,
                   display_name, is_client_available, is_default)
                VALUES
                  (:cid, :oid, 'tts', 'grok-voice-latest',
                   'Grok Voice', true, true)
                """
            ),
            {"cid": connection_id, "oid": organization_id},
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Remove grok-voice-latest entries inserted by this migration
    conn.execute(
        sa.text(
            """
            DELETE FROM org_available_models
            WHERE model_id = 'grok-voice-latest'
              AND connection_id IN (
                SELECT id FROM org_provider_connections
                WHERE provider = 'xai' AND service_type = 'tts'
              )
            """
        )
    )

    # Re-insert the old (wrong) voice ID entries
    xai_tts_connections = conn.execute(
        sa.text(
            "SELECT id, organization_id FROM org_provider_connections "
            "WHERE provider = 'xai' AND service_type = 'tts'"
        )
    ).fetchall()

    for row in xai_tts_connections:
        connection_id, organization_id = row[0], row[1]
        for voice_id in _XAI_TTS_WRONG_IDS:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO org_available_models
                      (connection_id, organization_id, service_type, model_id,
                       is_client_available, is_default)
                    VALUES
                      (:cid, :oid, 'tts', :mid, true, false)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"cid": connection_id, "oid": organization_id, "mid": voice_id},
            )
