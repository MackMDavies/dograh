"""add is_standing to campaigns

Revision ID: 94fd31bd5523
Revises: e2f3a4b5c6d7
Create Date: 2026-08-05

Idempotent ON PURPOSE. This branch chains from fork/main's head
(e2f3a4b5c6d7), but production runs a branch that is ahead of fork/main, whose
head is 4ac0c0ba8537 — and there e2f3a4b5c6d7 already has a child
(d1a2b3c4e5f6). Shipping this file to production as-is would give alembic TWO
heads, and boot runs `alembic upgrade head` under `set -e`, so the API
container would crash-loop.

The column is therefore added to production by direct DDL instead, and this
migration is written as a no-op when the column already exists. Whichever order
the two histories are eventually reconciled in, this cannot fail.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "94fd31bd5523"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS rather than op.add_column: the column may already have been
    # added out-of-band on an environment whose migration history diverged.
    op.execute(
        "ALTER TABLE campaigns "
        "ADD COLUMN IF NOT EXISTS is_standing BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE campaigns DROP COLUMN IF EXISTS is_standing")
