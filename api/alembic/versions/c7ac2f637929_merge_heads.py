"""merge heads

Revision ID: c7ac2f637929
Revises: b2c3d4e5f6a7, b7f3a1c2d9e4
Create Date: 2026-06-28 16:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c7ac2f637929'
down_revision: Union[str, Sequence[str], None] = ('b2c3d4e5f6a7', 'b7f3a1c2d9e4')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
