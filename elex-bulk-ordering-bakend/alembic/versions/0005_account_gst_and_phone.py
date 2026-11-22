"""a GST and a billing phone per account, and the phone each order used

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26

A run picks one address and one GST for every account in it, so a run that
mixes the accounts of two entities billed every order under one GST, and the
input credit landed on the wrong registration. The old portal kept a GST and a
phone per account; this brings both back:

* ``accounts.gst_id``: the account's own GST, which wins over the run's
  (app.jobs.tasks). A foreign key to ``gsts.id``, ON DELETE SET NULL: deleting
  a GST unlinks its accounts and keeps every one of them, as it does for
  addresses (app.api.gsts.delete_gst). Indexed, for that unlinking UPDATE and
  for the Address Book's count of accounts per GST.
* ``accounts.billing_phone``: the phone the account's orders are placed with.
* ``orders.billing_phone``: the phone an order was placed with, the ledger's
  "Billing Phone".

All three are nullable with no default: a NULL GST is "the run's", a NULL phone
is the account's own, as before.

Only what is missing is added. init_db adds the same three columns to a
database it keeps up to date (sync_missing_columns) and the index after them
(sync_missing_indexes), but never a foreign key; a database built by
create_all has everything. So on a database stamped 0001 and upgraded, each
column, the index and the foreign key are added only where absent, and a
``gst_id`` that init_db added is given its foreign key — after any id of a GST
deleted since is set to NULL, which is what the foreign key would have done.
``--sql`` cannot look, so it writes every statement out.

Batch mode, as every revision here: on SQLite adding a foreign key rebuilds
``accounts`` (the migration engine leaves foreign keys unenforced for that, see
env.py); on PostgreSQL the same block is plain ALTER statements. The foreign
key is named as PostgreSQL names the one create_all makes,
``accounts_gst_id_fkey``, so a migrated database and a built one agree.

The downgrade drops the index and the three columns; dropping ``gst_id`` takes
its foreign key with it (PostgreSQL drops a column's constraints with the
column, and the SQLite rebuild leaves out a constraint whose column is gone),
so no constraint has to be found by a name a create_all database never gave it.
What the columns held is lost on the way down.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The width of both phone columns: the ledger's cell and the account's own.
PHONE_WIDTH = 20
GST_FOREIGN_KEY = 'accounts_gst_id_fkey'
GST_INDEX = 'ix_accounts_gst_id'


def _columns(table: str) -> set[str]:
    """The columns ``table`` already has; none offline, so all are written out."""
    if context.is_offline_mode():
        return set()
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    if context.is_offline_mode():
        return set()
    return {index['name'] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _gst_foreign_key_present() -> bool:
    """Whether ``accounts.gst_id`` already points at ``gsts``."""
    if context.is_offline_mode():
        return False
    return any(
        key['referred_table'] == 'gsts' and key['constrained_columns'] == ['gst_id']
        for key in sa.inspect(op.get_bind()).get_foreign_keys('accounts')
    )


def upgrade() -> None:
    """Upgrade schema."""
    columns = _columns('accounts')
    indexes = _indexes('accounts')
    linked = _gst_foreign_key_present()
    if 'gst_id' in columns and not linked:
        # A column init_db added has no foreign key, so nothing set it to NULL
        # when a GST was deleted by other means than the app; the constraint
        # would refuse such an id. NULL is what ON DELETE SET NULL says.
        op.execute(
            sa.text(
                'UPDATE accounts SET gst_id = NULL '
                'WHERE gst_id IS NOT NULL AND gst_id NOT IN (SELECT id FROM gsts)'
            )
        )
    if not ({'gst_id', 'billing_phone'} <= columns and linked and GST_INDEX in indexes):
        with op.batch_alter_table('accounts') as batch_op:
            if 'gst_id' not in columns:
                batch_op.add_column(sa.Column('gst_id', sa.Integer(), nullable=True))
            if 'billing_phone' not in columns:
                batch_op.add_column(sa.Column('billing_phone', sa.String(length=PHONE_WIDTH), nullable=True))
            if not linked:
                batch_op.create_foreign_key(GST_FOREIGN_KEY, 'gsts', ['gst_id'], ['id'], ondelete='SET NULL')
            if GST_INDEX not in indexes:
                batch_op.create_index(GST_INDEX, ['gst_id'], unique=False)

    if 'billing_phone' not in _columns('orders'):
        with op.batch_alter_table('orders') as batch_op:
            batch_op.add_column(sa.Column('billing_phone', sa.String(length=PHONE_WIDTH), nullable=True))


def downgrade() -> None:
    """Downgrade schema: the three columns go, and what they held with them."""
    with op.batch_alter_table('orders') as batch_op:
        batch_op.drop_column('billing_phone')

    with op.batch_alter_table('accounts') as batch_op:
        batch_op.drop_index(GST_INDEX)
        batch_op.drop_column('billing_phone')
        batch_op.drop_column('gst_id')
