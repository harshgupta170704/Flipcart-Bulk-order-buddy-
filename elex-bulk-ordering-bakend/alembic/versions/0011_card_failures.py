"""card_failures: cards that failed an order lately, by fingerprint only

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-13

Card pool rules (step C): "exclude cards that failed an order recently". A
failed order keeps no card number on its row, so the failure is remembered
here under a salted one-way fingerprint of the number, never the number.

Created only where missing: ``init_db`` creates a model's new tables at
start-up, so a database the new API started on already has it.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exists() -> bool:
    if context.is_offline_mode():
        return False
    return 'card_failures' in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _exists():
        return
    op.create_table(
        'card_failures',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('card_key', sa.String(length=64), nullable=False),
        sa.Column('failed_at', sa.DateTime(), nullable=False),
        sa.Column('reason', sa.String(length=255), nullable=True),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_card_failures_card_key'), 'card_failures', ['card_key'], unique=False)
    op.create_index(op.f('ix_card_failures_failed_at'), 'card_failures', ['failed_at'], unique=False)


def downgrade() -> None:
    if not context.is_offline_mode() and not _exists():
        return
    op.drop_index(op.f('ix_card_failures_failed_at'), table_name='card_failures')
    op.drop_index(op.f('ix_card_failures_card_key'), table_name='card_failures')
    op.drop_table('card_failures')
