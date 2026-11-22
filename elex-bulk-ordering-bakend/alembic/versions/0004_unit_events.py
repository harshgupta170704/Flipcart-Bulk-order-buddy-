"""unit_events and users.changes_seen_at: what changed since the operator last looked

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26

The read-back now writes one ``unit_events`` row per change in a unit's
delivery state — delivered, out for delivery, cancelled, returned, failed —
and the dashboard lists the rows each user has not marked as seen
(app.core.unit_events, app.api.changes). Two additions, nothing changed:

* the ``unit_events`` table, indexed on ``at`` (the feed's window) and on its
  three foreign keys, exactly as create_all builds it;
* ``users.changes_seen_at``, nullable, so every existing user starts with no
  mark and sees the last 7 days.

Each is made only where it is missing. init_db makes both by itself
(create_all for the table, sync_missing_columns for the column), so a
database it has already brought up to date — the laptop's, or a PostgreSQL an
API of this build started on before the migration ran, then stamped at an
older revision (docs/DEPLOY.md §5) — has them, and this revision then has
nothing to do rather than stopping on "already exists". ``--sql`` (offline)
writes both out, since there is no database to look at.

The downgrade drops both, events included: the table first, then the column
where it is there.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _missing() -> tuple[bool, bool]:
    """Whether the database still lacks (the table, the column)."""
    if context.is_offline_mode():
        return True, True
    inspector = sa.inspect(op.get_bind())
    table = 'unit_events' not in inspector.get_table_names()
    column = 'changes_seen_at' not in {c['name'] for c in inspector.get_columns('users')}
    return table, column


def upgrade() -> None:
    """Upgrade schema."""
    table, column = _missing()
    if table:
        op.create_table(
            'unit_events',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('unit_pk', sa.Integer(), nullable=False),
            sa.Column('order_pk', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('at', sa.DateTime(), nullable=False),
            sa.Column('kind', sa.String(length=32), nullable=False),
            sa.Column('from_state', sa.String(length=32), nullable=True),
            sa.Column('to_state', sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(['unit_pk'], ['order_units.id']),
            sa.ForeignKeyConstraint(['order_pk'], ['orders.id']),
            sa.ForeignKeyConstraint(['account_id'], ['accounts.id']),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_unit_events_account_id'), 'unit_events', ['account_id'], unique=False)
        op.create_index(op.f('ix_unit_events_at'), 'unit_events', ['at'], unique=False)
        op.create_index(op.f('ix_unit_events_order_pk'), 'unit_events', ['order_pk'], unique=False)
        op.create_index(op.f('ix_unit_events_unit_pk'), 'unit_events', ['unit_pk'], unique=False)

    if column:
        # Batch mode, like every revision here (alembic/env.py): a plain ADD
        # COLUMN on both SQLite and PostgreSQL, nothing rebuilt.
        with op.batch_alter_table('users') as batch_op:
            batch_op.add_column(sa.Column('changes_seen_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema: the table with its indexes, then the column.

    Dropping a table meets the app's guard
    (app.db.base.refuse_drop_unless_throwaway): refused on any database but a
    throwaway one unless ORDERING_ALLOW_DROP=1 says so, on purpose. The table
    goes first so that a refusal comes before anything else has been sent:
    dropping the column first rebuilt ``users`` on SQLite, and where the step
    was not one transaction, the refusal left Alembic's copy
    ``_alembic_tmp_users`` behind, which stopped the very retry the refusal
    asks for on "already exists".

    The column goes only where it is there, as the downgrades of 0003 and
    0006 check theirs; ``--sql`` (offline) cannot look, so it writes the drop
    out.
    """
    op.drop_table('unit_events')
    if context.is_offline_mode() or 'changes_seen_at' in {
        c['name'] for c in sa.inspect(op.get_bind()).get_columns('users')
    }:
        with op.batch_alter_table('users') as batch_op:
            batch_op.drop_column('changes_seen_at')
