"""assign phone numbers to an agent for outbound, and pin a campaign's caller ID

Until now ``inbound_workflow_id`` was the only link between a phone number and
an agent, so "this agent's outbound numbers" could not be expressed at all and
every active number on a telephony config was dialled from indiscriminately.

Two additions:

* ``telephony_phone_numbers.outbound_workflow_id`` — mirrors the inbound column.
  A number carrying only ``inbound_workflow_id`` is now inbound-only, which is
  what makes a dedicated, never-dialled-from inbound line possible.
* ``campaigns.from_phone_number_id`` — the specific number a campaign sends
  with. Safe now that a single number can carry many concurrent calls; under
  the old one-call-per-DID pool it would have throttled the campaign to 1.

The backfill sets ``outbound_workflow_id = inbound_workflow_id`` so behaviour
is unchanged on deploy: every number that dials today keeps dialling. Marking a
number inbound-only is then a deliberate action in the UI, not a silent
side effect of this migration.

Revision ID: c3d4e5f6a7b1
Revises: b2c3d4e5f6a9
Create Date: 2026-08-09 01:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b1"
down_revision = "b2c3d4e5f6a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telephony_phone_numbers",
        sa.Column("outbound_workflow_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_telephony_phone_numbers_outbound_workflow_id",
        "telephony_phone_numbers",
        "workflows",
        ["outbound_workflow_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Preserve today's behaviour: everything that currently dials keeps dialling.
    op.execute(
        """
        UPDATE telephony_phone_numbers
           SET outbound_workflow_id = inbound_workflow_id
         WHERE inbound_workflow_id IS NOT NULL
        """
    )
    op.create_index(
        "idx_telephony_phone_numbers_outbound_workflow",
        "telephony_phone_numbers",
        ["outbound_workflow_id"],
        postgresql_where=sa.text("outbound_workflow_id IS NOT NULL"),
    )

    op.add_column(
        "campaigns",
        sa.Column("from_phone_number_id", sa.Integer(), nullable=True),
    )
    # SET NULL rather than CASCADE: releasing a number must not delete the
    # campaign's history. A null here falls back to the config-wide pool.
    op.create_foreign_key(
        "fk_campaigns_from_phone_number_id",
        "campaigns",
        "telephony_phone_numbers",
        ["from_phone_number_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_campaigns_from_phone_number_id", "campaigns", type_="foreignkey"
    )
    op.drop_column("campaigns", "from_phone_number_id")
    op.drop_index(
        "idx_telephony_phone_numbers_outbound_workflow",
        table_name="telephony_phone_numbers",
    )
    op.drop_constraint(
        "fk_telephony_phone_numbers_outbound_workflow_id",
        "telephony_phone_numbers",
        type_="foreignkey",
    )
    op.drop_column("telephony_phone_numbers", "outbound_workflow_id")
