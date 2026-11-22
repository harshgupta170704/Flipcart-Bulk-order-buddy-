"""order_units: what an order report says of a unit's refund, its courier and who cancelled it

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-27

Spec FR-022: a later order report updates the refunds and the cancellations
of the units it holds. The report's refund, courier and last-tracking columns
were read past as "not kept", and its ``cancelled_by_user`` without a word, so
a report saying a unit was cancelled by the user and refunded left the CSV's
cells blank and the row reading "cancelled by Flipkart" (review of 27 Oct
2026). A unit now keeps them:

* ``refund_status`` (String 64), ``refund_amount`` (Float), ``refund_method``
  (String 100): the refund, as the report gives it;
* ``shipment_partner`` (String 128): the courier;
* ``last_tracking_text`` (String 255): the report's own last tracking update;
* ``cancelled_by_user`` (Boolean): whether the user cancelled the unit, as the
  report says.

All nullable, nothing to fill: no report has kept them before.

The columns are added only where they are missing, as in revisions 0003–0009:
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
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ('refund_status', lambda: sa.String(length=64)),
    ('refund_amount', sa.Float),
    ('refund_method', lambda: sa.String(length=100)),
    ('shipment_partner', lambda: sa.String(length=128)),
    ('last_tracking_text', lambda: sa.String(length=255)),
    ('cancelled_by_user', sa.Boolean),
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
