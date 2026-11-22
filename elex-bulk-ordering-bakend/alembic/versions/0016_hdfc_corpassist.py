"""hdfc_user_ids and hdfc_child_cards: HDFC CorpAssist virtual cards

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-19

CorpAssist (app.api.corpassist): the HDFC user ids whose child-card emails
are read from the shared inbox, and the child cards read from them.

A child card keeps its number, its parent card's last four digits and its
opening amount (the owner's decision of 19 Nov 2026, amending PLAN.md §5).
There is no column for an expiry or a CVV: those still come from the run's
card sheet and are held in memory only.

As in revisions 0003-0013, each table is created only where it is missing:
``init_db`` creates a model's new tables at start-up, so a database the new
API started on already has them.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = '0016'
down_revision: Union[str, Sequence, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    if context.is_offline_mode():
        return False
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has('hdfc_user_ids'):
        op.create_table(
            'hdfc_user_ids',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('username', sa.String(length=64), nullable=False),
            sa.Column('email', sa.String(length=255), nullable=False),
            sa.Column('mobile', sa.String(length=16), nullable=True),
            sa.Column('parent_card_last4', sa.String(length=4), nullable=True),
            sa.Column('active', sa.Boolean(), nullable=False),
            sa.Column('last_fetched_at', sa.DateTime(), nullable=True),
            sa.Column('last_error', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_hdfc_user_ids_username'), 'hdfc_user_ids', ['username'], unique=True)
        op.create_index(op.f('ix_hdfc_user_ids_email'), 'hdfc_user_ids', ['email'], unique=True)
    if not _has('hdfc_child_cards'):
        op.create_table(
            'hdfc_child_cards',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('card_number', sa.String(length=32), nullable=False),
            sa.Column('parent_card_last4', sa.String(length=4), nullable=True),
            sa.Column('opening_amount', sa.Float(), nullable=True),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('error', sa.String(length=255), nullable=True),
            sa.Column('issued_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['user_id'], ['hdfc_user_ids.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_hdfc_child_cards_user_id'), 'hdfc_child_cards', ['user_id'], unique=False)
        op.create_index(op.f('ix_hdfc_child_cards_card_number'), 'hdfc_child_cards', ['card_number'], unique=True)
        op.create_index(op.f('ix_hdfc_child_cards_created_at'), 'hdfc_child_cards', ['created_at'], unique=False)


def downgrade() -> None:
    offline = context.is_offline_mode()
    if offline or _has('hdfc_child_cards'):
        op.drop_index(op.f('ix_hdfc_child_cards_created_at'), table_name='hdfc_child_cards')
        op.drop_index(op.f('ix_hdfc_child_cards_card_number'), table_name='hdfc_child_cards')
        op.drop_index(op.f('ix_hdfc_child_cards_user_id'), table_name='hdfc_child_cards')
        op.drop_table('hdfc_child_cards')
    if offline or _has('hdfc_user_ids'):
        op.drop_index(op.f('ix_hdfc_user_ids_email'), table_name='hdfc_user_ids')
        op.drop_index(op.f('ix_hdfc_user_ids_username'), table_name='hdfc_user_ids')
        op.drop_table('hdfc_user_ids')
