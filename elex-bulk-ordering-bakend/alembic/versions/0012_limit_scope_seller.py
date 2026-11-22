"""unit_limits: a "seller" scope

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-13

Step D: a unit limit can cap the units bought from one seller. On PostgreSQL
the scope is a native enum type, which gains the value; SQLite stores the
name as text and needs nothing. A PostgreSQL enum value cannot be dropped, so
the downgrade leaves it (no row uses it once seller limits are deleted).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_context().dialect.name == 'postgresql':
        op.execute("ALTER TYPE limitscope ADD VALUE IF NOT EXISTS 'seller'")


def downgrade() -> None:
    pass
