"""unique queued_run source_uuid per campaign

Revision ID: 4ac0c0ba8537
Revises: e2b3c4d5f6a7
Create Date: 2026-08-03 23:40:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4ac0c0ba8537"
down_revision: Union[str, None] = "e2b3c4d5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # source_uuid is already deterministic per row (e.g. CSV sync derives it
    # from a hash of the file plus row index), so this constraint is safe to
    # add — no genuine duplicate is ever supposed to exist. It lets
    # bulk_create_queued_runs use an upsert (ON CONFLICT DO NOTHING) so a
    # retried sync task is idempotent instead of double-inserting every row
    # in the campaign.
    op.create_unique_constraint(
        "uq_queued_runs_campaign_source_uuid",
        "queued_runs",
        ["campaign_id", "source_uuid"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_queued_runs_campaign_source_uuid", "queued_runs", type_="unique"
    )
