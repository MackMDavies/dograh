"""merge prod and main heads

Revision ID: a7c1e4b90d32
Revises: 4ac0c0ba8537, 94fd31bd5523
Create Date: 2026-08-06 11:05:00.000000

Merging main into the production branch brings together two migration chains
that forked at different parents:

    4ac0c0ba8537 (prod) -> e2b3c4d5f6a7
    94fd31bd5523 (main) -> e2f3a4b5c6d7

Two heads make `alembic upgrade head` abort with "Multiple head revisions are
present". Boot runs that under `set -e` (scripts/start_services_docker.sh), so
without this merge the api container crash-loops on the next restart rather
than failing at deploy time.

No schema change here - both branches' migrations still run, this only rejoins
the history. 94fd31bd5523 uses ADD COLUMN IF NOT EXISTS, so it is a no-op on
production, where campaigns.is_standing was already added out-of-band while the
revision itself was never stamped.
"""

from typing import Sequence, Union

revision: str = "a7c1e4b90d32"
down_revision: Union[str, Sequence[str], None] = ("4ac0c0ba8537", "94fd31bd5523")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
