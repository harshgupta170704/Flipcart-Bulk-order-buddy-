"""order_units: the promised and the actual delivery date, and how the unit was paid

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26

``order_units.delivery_date`` holds the promised day while a unit is on its
way and the read-back overwrites it with the delivery day, so whether a unit
came late could not be told afterwards. The read-back now reads the two apart,
and the order page's price details as well (app.automation.sync, 26 Oct 2026),
and the unit keeps all four:

* ``promised_date``, ``delivered_date`` (DateTime);
* ``payment_method`` (VARCHAR(100)): "Cash On Delivery", a card;
* ``amount_paid`` (FLOAT, as ``orders.amount``): the unit page's "Total amount".

All nullable, nothing to backfill: a row read back before simply has none
until its next read-back.

Each column is added only where it is missing, as in revisions 0003–0005:
``init_db`` adds a model's new nullable columns at start-up
(``sync_missing_columns``), so a database the new API started on before this
ran already has them, and adding them again would stop on "already exists".
With no ``order_units`` table at all both directions do nothing (``init_db``
creates the table with the columns). ``--sql`` (offline) cannot look, so it
writes every statement out. The downgrade drops the columns, and what they held.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ('promised_date', sa.DateTime),
    ('delivered_date', sa.DateTime),
    ('payment_method', lambda: sa.String(length=100)),
    ('amount_paid', sa.Float),
)


def _present() -> set[str] | None:
    """The order_units columns already there; None when there is no such
    table; every name missing offline, so all are written out."""
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
    missing = [(name, kind) for name, kind in COLUMNS if name not in present]
    if not missing:
        return
    with op.batch_alter_table('order_units', schema=None) as batch_op:
        for name, kind in missing:
            batch_op.add_column(sa.Column(name, kind(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    present = _present()
    if present is None:
        return
    there = [name for name, _kind in COLUMNS if context.is_offline_mode() or name in present]
    if not there:
        return
    with op.batch_alter_table('order_units', schema=None) as batch_op:
        for name in reversed(there):
            batch_op.drop_column(name)
