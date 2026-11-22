"""Parity with the competitor's video: run references, balances, the agent
fleet, gift vouchers, order phone numbers and the CSV Downloads log

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-17

* orders: ``ref_code``, ``po_number``, ``rtgs_source``, ``rtgs_beneficiary``,
  ``ewb_number``, ``used_supercoins``.
* accounts: ``supercoins_balance``, ``gift_voucher_balance``,
  ``balances_read_at``, ``is_parent``, ``parent_email``.
* new tables: ``agents``, ``gift_vouchers``, ``order_phones``, ``export_logs``.
* jobs: the job type ``balances`` (a native enum on PostgreSQL gains the
  value; SQLite stores the name as text).

As in revisions 0003–0012, every column and table is added only where it is
missing: ``init_db`` adds a model's new nullable columns and new tables at
start-up, so a database the new API started on already has them.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = {
    'orders': {
        'ref_code': sa.String(length=64),
        'po_number': sa.String(length=64),
        'rtgs_source': sa.String(length=128),
        'rtgs_beneficiary': sa.String(length=128),
        'ewb_number': sa.String(length=32),
        'used_supercoins': sa.Boolean(),
    },
    'accounts': {
        'supercoins_balance': sa.Integer(),
        'gift_voucher_balance': sa.Float(),
        'balances_read_at': sa.DateTime(),
        'is_parent': sa.Boolean(),
        'parent_email': sa.String(length=255),
    },
}

JOB_TYPES = ('login', 'coupon', 'order', 'sync', 'compile', 'balances')

INDEXES = {
    ('orders', 'ref_code'): 'ix_orders_ref_code',
    ('accounts', 'supercoins_balance'): 'ix_accounts_supercoins_balance',
    ('accounts', 'parent_email'): 'ix_accounts_parent_email',
}


def _inspector():
    return None if context.is_offline_mode() else sa.inspect(op.get_bind())


def _tables() -> set[str]:
    inspector = _inspector()
    return set() if inspector is None else set(inspector.get_table_names())


def _columns(table: str) -> set[str] | None:
    inspector = _inspector()
    if inspector is None:
        return set()
    if table not in inspector.get_table_names():
        return None
    return {column['name'] for column in inspector.get_columns(table)}


def upgrade() -> None:
    for table, columns in COLUMNS.items():
        present = _columns(table)
        if present is None:
            continue
        missing = [name for name in columns if name not in present]
        if missing:
            with op.batch_alter_table(table, schema=None) as batch_op:
                for name in missing:
                    batch_op.add_column(sa.Column(name, columns[name], nullable=True))
                    index = INDEXES.get((table, name))
                    if index:
                        batch_op.create_index(index, [name], unique=False)

    tables = _tables()
    if 'agents' not in tables:
        op.create_table(
            'agents',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=255), nullable=False),
            sa.Column('kind', sa.String(length=16), nullable=False),
            sa.Column('host', sa.String(length=255), nullable=True),
            sa.Column('enabled', sa.Boolean(), nullable=False),
            sa.Column('isolated', sa.Boolean(), nullable=False),
            sa.Column('last_seen_at', sa.DateTime(), nullable=True),
            sa.Column('last_test_at', sa.DateTime(), nullable=True),
            sa.Column('last_test_ok', sa.Boolean(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_agents_name', 'agents', ['name'], unique=True)
    if 'gift_vouchers' not in tables:
        op.create_table(
            'gift_vouchers',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('label', sa.String(length=64), nullable=False),
            sa.Column('code_last4', sa.String(length=8), nullable=True),
            sa.Column('value', sa.Float(), nullable=False),
            sa.Column('balance', sa.Float(), nullable=False),
            sa.Column('account_id', sa.Integer(), sa.ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True),
            sa.Column('expires_on', sa.Date(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_gift_vouchers_account_id', 'gift_vouchers', ['account_id'], unique=False)
    if 'order_phones' not in tables:
        op.create_table(
            'order_phones',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('number', sa.String(length=16), nullable=False),
            sa.Column('label', sa.String(length=64), nullable=True),
            sa.Column('active', sa.Boolean(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_order_phones_number', 'order_phones', ['number'], unique=True)
    if 'export_logs' not in tables:
        op.create_table(
            'export_logs',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('kind', sa.String(length=32), nullable=False),
            sa.Column('file_name', sa.String(length=128), nullable=False),
            sa.Column('query', sa.Text(), nullable=False),
            sa.Column('created_by', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_export_logs_created_at', 'export_logs', ['created_at'], unique=False)

    if op.get_context().dialect.name == 'postgresql':
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'balances'")
    elif 'jobs' in tables or context.is_offline_mode():
        # SQLite keeps the enum as text sized to its longest name: "balances"
        # is one character longer than "compile".
        with op.batch_alter_table('jobs', schema=None) as batch_op:
            batch_op.alter_column(
                'type',
                existing_type=sa.Enum(*JOB_TYPES[:-1], name='jobtype'),
                type_=sa.Enum(*JOB_TYPES, name='jobtype'),
                existing_nullable=False,
            )


def downgrade() -> None:
    tables = _tables() if not context.is_offline_mode() else {'agents', 'gift_vouchers', 'order_phones', 'export_logs'}
    for table in ('export_logs', 'order_phones', 'gift_vouchers', 'agents'):
        if table in tables:
            op.drop_table(table)
    for table, columns in COLUMNS.items():
        present = _columns(table)
        if present is None:
            continue
        gone = [name for name in columns if name in present]
        if gone:
            with op.batch_alter_table(table, schema=None) as batch_op:
                for name in gone:
                    index = INDEXES.get((table, name))
                    if index:
                        batch_op.drop_index(index)
                    batch_op.drop_column(name)
