"""order_units.cancel_reason: why the platform cancelled a unit

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26

The read-back keeps the line Flipkart's order page prints under a cancelled
order's "Cancelled, <date>" step ("Sorry, your order was cancelled as per
seller policy restrictions applicable to this brand.", seen live 24–25 Oct
2026), so a cancellation by Flipkart is no longer counted as the tool's own
failure. One nullable VARCHAR(255) column: nothing to backfill, and every row
already there simply has no reason recorded.

The upgrade adds it only where it is missing. ``init_db`` adds a model's new
nullable columns at start-up (``sync_missing_columns``), so on a database the
new API started on before this ran the column is already there; adding it
again would stop on "already exists" (docs/DEPLOY.md §5). With ``--sql``
(offline) there is no column to look at, so the ALTER is written out. The
downgrade drops the column, and with it any reasons recorded.

Where there is no order_units table at all, both directions do nothing, as
revision 0002 does for compiled_archives: init_db creates the table, column
and all, so there is nothing to add — and nothing for a downgrade to take.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cancel_reason_column() -> bool | None:
    """Whether order_units has the column; None when there is no such table."""
    inspector = sa.inspect(op.get_bind())
    if 'order_units' not in inspector.get_table_names():
        return None
    return any(column['name'] == 'cancel_reason' for column in inspector.get_columns('order_units'))


def upgrade() -> None:
    """Upgrade schema."""
    if not context.is_offline_mode() and _cancel_reason_column() is not False:
        return
    with op.batch_alter_table('order_units', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cancel_reason', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    if not context.is_offline_mode() and _cancel_reason_column() is not True:
        return
    with op.batch_alter_table('order_units', schema=None) as batch_op:
        batch_op.drop_column('cancel_reason')
