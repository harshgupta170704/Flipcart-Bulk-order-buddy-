"""order_units: the first delivery promise, kept apart from the latest

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26

``order_units.promised_date`` (0006) follows Flipkart's promise: a delivery
that slips gets a later "Delivery expected by" date, and the read-back writes
it over the earlier one. A unit that then arrives on the moved day read "on
time", although it came days after the first promise. The unit now also keeps
the first promise it was given, written once and never replaced, and lateness
is counted from it:

* ``first_promised_date`` (DateTime), nullable.

The upgrade fills it from ``promised_date`` where it is empty. That is the
best there is for a row read back before this: an earlier promise that was
already written over cannot be told any more.

The column is added only where it is missing, as in revisions 0003–0006:
``init_db`` adds a model's new nullable columns at start-up
(``sync_missing_columns``), so a database the new API started on before this
ran already has it, empty, and adding it again would stop on "already
exists"; the fill runs either way. With no ``order_units`` table at all both
directions do nothing (``init_db`` creates the table with the column).
``--sql`` (offline) cannot look, so it writes every statement out. The
downgrade drops the column, and what it held.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMN = 'first_promised_date'


def _present() -> set[str] | None:
    """The order_units columns already there; None when there is no such
    table; every name missing offline, so all is written out."""
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if 'order_units' not in inspector.get_table_names():
        return None
    return {column['name'] for column in inspector.get_columns('order_units')}


def upgrade() -> None:
    """Upgrade schema."""
    present = _present()
    if present is None:
        return
    if COLUMN not in present:
        with op.batch_alter_table('order_units', schema=None) as batch_op:
            batch_op.add_column(sa.Column(COLUMN, sa.DateTime(), nullable=True))
    op.execute(
        sa.text(
            f"UPDATE order_units SET {COLUMN} = promised_date "
            f"WHERE {COLUMN} IS NULL AND promised_date IS NOT NULL"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    present = _present()
    if present is None:
        return
    if not (context.is_offline_mode() or COLUMN in present):
        return
    with op.batch_alter_table('order_units', schema=None) as batch_op:
        batch_op.drop_column(COLUMN)
