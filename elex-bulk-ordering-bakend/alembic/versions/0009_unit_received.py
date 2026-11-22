"""order_units: who marked the unit's goods as received, and when

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27

The spec's ledger carries "Received" — the goods physically in hand (FR-001,
FR-017) — and nothing held it. A person now marks a unit received, or takes
the mark back, from the ledger (POST /orders/received):

* ``received_at`` (DateTime) and ``received_by`` (String 255), nullable.

Nothing is filled: no unit has been marked yet.

The columns are added only where they are missing, as in revisions 0003–0007:
``init_db`` adds a model's new nullable columns at start-up
(``sync_missing_columns``), so a database the new API started on before this
ran already has them, and adding them again would stop on "already exists".
With no ``order_units`` table at all both directions do nothing (``init_db``
creates the table with the columns). ``--sql`` (offline) cannot look, so it
writes every statement out. The downgrade drops the columns, and the marks.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = {
    'received_at': sa.DateTime(),
    'received_by': sa.String(length=255),
}


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
    missing = [name for name in COLUMNS if name not in present]
    if missing:
        with op.batch_alter_table('order_units', schema=None) as batch_op:
            for name in missing:
                batch_op.add_column(sa.Column(name, COLUMNS[name], nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    present = _present()
    if present is None:
        return
    there = [name for name in COLUMNS if context.is_offline_mode() or name in present]
    if there:
        with op.batch_alter_table('order_units', schema=None) as batch_op:
            for name in there:
                batch_op.drop_column(name)
