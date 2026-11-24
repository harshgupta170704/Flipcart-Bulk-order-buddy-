"""Email swaps: requested Flipkart login-email changes (old -> new)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-24

* new table ``email_swaps``: one row per requested email change, fed by the
  Swap Email Address CSV upload. A background worker signs in with the account's
  stored session, drives Flipkart's profile email change, and fills the
  verification code(s) the modal asks for. ``status`` walks
  pending -> running -> done / failed.

As in the earlier revisions, the table is added only where it is missing:
``init_db`` adds a model's new tables at start-up (``Base.metadata.create_all``),
so a database the new API started on already has it.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = '0020'
down_revision: Union[str, Sequence[str], None] = '0019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'email_swaps'
INDEXES = ('batch', 'old_email', 'new_email', 'status')


def _tables() -> set[str]:
    if context.is_offline_mode():
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE in _tables():
        return
    op.create_table(
        TABLE,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('batch', sa.String(length=64), nullable=True),
        sa.Column('old_email', sa.String(length=255), nullable=False),
        sa.Column('new_email', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('stage', sa.String(length=32), nullable=True),
        sa.Column('message', sa.String(length=500), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    for column in INDEXES:
        op.create_index(f'ix_{TABLE}_{column}', TABLE, [column])


def downgrade() -> None:
    if not context.is_offline_mode() and TABLE not in _tables():
        return
    for column in INDEXES:
        op.drop_index(f'ix_{TABLE}_{column}', table_name=TABLE)
    op.drop_table(TABLE)
