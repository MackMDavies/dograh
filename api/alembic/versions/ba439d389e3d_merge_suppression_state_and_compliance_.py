"""merge suppression-state and compliance-acknowledgement branches

Revision ID: ba439d389e3d
Revises: c4a9b2d0e5f6, d4e5f6a7b2c3
Create Date: 2026-08-10 07:01:22.158547

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ba439d389e3d'
down_revision: Union[str, None] = ('c4a9b2d0e5f6', 'd4e5f6a7b2c3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
