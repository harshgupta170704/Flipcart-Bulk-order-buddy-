"""Orders: processing stage and error code per attempt (``processing_stage``, ``error_code``)

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-22

Every order attempt records which of the 25 checkout stages it reached and its
classified error code (``app.core.order_outcomes``) so the Orders ledger can
filter and count attempts by stage and error code.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
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
    if present is None:
        return
    with op.batch_alter_table('orders', schema=None) as batch_op:
        if 'processing_stage' not in present:
            batch_op.add_column(sa.Column('processing_stage', sa.String(length=64), nullable=True))
        if 'error_code' not in present:
            batch_op.add_column(sa.Column('error_code', sa.String(length=128), nullable=True))


def downgrade() -> None:
    present = _columns('orders')
    if present is None:
        return
    with op.batch_alter_table('orders', schema=None) as batch_op:
        if 'error_code' in present:
            batch_op.drop_column('error_code')
        if 'processing_stage' in present:
            batch_op.drop_column('processing_stage')
