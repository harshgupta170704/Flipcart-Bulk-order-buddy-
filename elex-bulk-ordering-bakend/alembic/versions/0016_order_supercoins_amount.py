"""Orders: the rupees of SuperCoins a checkout took off (``supercoins_amount``)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19

The dashboard's "SuperCoins applied" counted orders, not rupees (audit of
19 Nov 2026). The ordering flow now reads the total before and after the
SuperCoins toggle and keeps the difference on the order.

As in revisions 0003–0015, the column is added only where it is missing:
``init_db`` adds a model's new nullable columns at start-up.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = '0016'
down_revision: Union[str, Sequence[str], None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str] | None:
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return None
    return {column['name'] for column in inspector.get_columns(table)}


def upgrade() -> None:
    present = _columns('orders')
    if present is not None and 'supercoins_amount' not in present:
        with op.batch_alter_table('orders', schema=None) as batch_op:
            batch_op.add_column(sa.Column('supercoins_amount', sa.Float(), nullable=True))


def downgrade() -> None:
    present = _columns('orders')
    if present is not None and 'supercoins_amount' in present:
        with op.batch_alter_table('orders', schema=None) as batch_op:
            batch_op.drop_column('supercoins_amount')
