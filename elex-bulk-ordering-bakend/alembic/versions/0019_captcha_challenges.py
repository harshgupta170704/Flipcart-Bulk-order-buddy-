"""Captcha challenges: the captchas the browser flows met, and who answered them

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-23

* new table ``captcha_challenges``: one row per captcha or "unusual activity"
  page a Flipkart or HDFC CorpAssist flow met, with its outcome (answered by a
  person on the Captchas page, skipped, expired, blocked). The run's automatic
  stop counts these rows; the picture itself is never stored.

As in the earlier revisions, the table is added only where it is missing:
``init_db`` adds a model's new tables at start-up, so a database the new API
started on already has it.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = '0019'
down_revision: Union[str, Sequence[str], None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'captcha_challenges'
INDEXES = ('account_id', 'job_id', 'status', 'created_at')


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
        sa.Column('site', sa.String(length=16), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('step', sa.String(length=64), nullable=False),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('detail', sa.String(length=300), nullable=True),
        sa.Column('answered_by', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    for column in INDEXES:
        op.create_index(f'ix_{TABLE}_{column}', TABLE, [column])


def downgrade() -> None:
    # Offline (``--sql``) nothing can be looked at, so the drops are written out.
    if not context.is_offline_mode() and TABLE not in _tables():
        return
    for column in INDEXES:
        op.drop_index(f'ix_{TABLE}_{column}', table_name=TABLE)
    op.drop_table(TABLE)
