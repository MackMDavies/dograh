"""fix minimax model ids

Revision ID: b3c1d2e4f5a6
Revises: fec0fb9a8db7
Create Date: 2026-05-29 23:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c1d2e4f5a6"
down_revision: Union[str, None] = "fec0fb9a8db7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The seed data for Minimax LLM models incorrectly used "MiniMax-Text-01"
    # which does not exist in the Minimax API. The correct model is "MiniMax-M2.7".
    # Update any existing org_available_models rows that reference the wrong name.
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE org_available_models
            SET model_id = 'MiniMax-M2.7'
            WHERE model_id = 'MiniMax-Text-01'
              AND connection_id IN (
                SELECT id FROM org_provider_connections
                WHERE provider = 'minimax' AND service_type = 'llm'
              )
            """
        )
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE org_available_models
            SET model_id = 'MiniMax-Text-01'
            WHERE model_id = 'MiniMax-M2.7'
              AND connection_id IN (
                SELECT id FROM org_provider_connections
                WHERE provider = 'minimax' AND service_type = 'llm'
              )
            """
        )
    )
