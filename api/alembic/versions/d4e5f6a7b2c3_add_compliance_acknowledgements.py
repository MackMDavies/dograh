"""append-only record of compliance acknowledgements

The calling-hours override is already enforced — the wizard blocks Continue and
the API rejects ``mode='off'`` without ``off_acknowledged_at``. What it lacked
was evidence. The timestamp lived in ``campaigns.orchestrator_metadata``, a
mutable JSON blob, which answers "was this campaign acknowledged" and none of
the questions actually asked in a dispute:

* WHO agreed — the blob has no actor, and ``campaigns.created_by`` names the
  creator, who may not be whoever later switched the campaign to 'off'.
* WHAT they were shown — the warning wording was never captured, so a later
  copy change makes it unprovable.
* WHEN it changed — an update overwrote the previous timestamp, so toggling
  off → on → off erased the original acknowledgement entirely.

This table is append-only by design. Nothing updates a row; a change of mind is
a new row, so the sequence is the history.

``campaign_id`` is ON DELETE SET NULL rather than CASCADE: deleting a campaign
must not delete the proof that someone accepted the risk of running it. The
denormalised ``campaign_name`` survives that deletion for the same reason.

Revision ID: d4e5f6a7b2c3
Revises: c3d4e5f6a7b1
Create Date: 2026-08-09 05:50:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d4e5f6a7b2c3"
down_revision = "c3d4e5f6a7b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "compliance_acknowledgements",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        # The actor. Not nullable — an acknowledgement nobody made is not an
        # acknowledgement, and a NULL here would be indistinguishable from an
        # unattributed system write.
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Kept so the record still identifies its subject after the campaign row
        # is gone.
        sa.Column("campaign_name", sa.String(255), nullable=True),
        # What was agreed to, e.g. "calling_hours_off". A string rather than an
        # enum: a new acknowledgement type must never require a migration that
        # could fail on an ALTER TYPE (see the enum-ownership trap).
        sa.Column("acknowledgement_type", sa.String(64), nullable=False),
        # The exact wording the user saw, as reported by the client, and the
        # server's canonical version at the time. Both are stored: they should
        # agree, and a mismatch is itself worth having on record.
        sa.Column("statement_text", sa.Text(), nullable=True),
        sa.Column("statement_version", sa.String(64), nullable=True),
        sa.Column("client_statement_text", sa.Text(), nullable=True),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Request provenance (ip, user agent) plus the settings in force at the
        # moment of acknowledgement.
        sa.Column(
            "context",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # The three ways this gets read: everything for an org, the history for one
    # campaign, and "show me every override in this period".
    op.create_index(
        "idx_compliance_ack_org_time",
        "compliance_acknowledgements",
        ["organization_id", "acknowledged_at"],
    )
    op.create_index(
        "idx_compliance_ack_campaign",
        "compliance_acknowledgements",
        ["campaign_id"],
    )
    op.create_index(
        "idx_compliance_ack_type_time",
        "compliance_acknowledgements",
        ["acknowledgement_type", "acknowledged_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_compliance_ack_type_time", table_name="compliance_acknowledgements"
    )
    op.drop_index(
        "idx_compliance_ack_campaign", table_name="compliance_acknowledgements"
    )
    op.drop_index(
        "idx_compliance_ack_org_time", table_name="compliance_acknowledgements"
    )
    op.drop_table("compliance_acknowledgements")
