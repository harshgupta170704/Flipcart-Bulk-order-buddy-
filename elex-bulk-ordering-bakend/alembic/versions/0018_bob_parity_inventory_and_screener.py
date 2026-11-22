"""BOB parity: Product Screener watchlist and Inventory tables

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-22
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = '0018'
down_revision: Union[str, Sequence[str], None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    if context.is_offline_mode():
        return False
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has('screener_items'):
        op.create_table(
            'screener_items',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('platform', sa.String(length=32), nullable=False),
            sa.Column('product_url', sa.Text(), nullable=False),
            sa.Column('product_title', sa.String(length=255), nullable=True),
            sa.Column('target_price', sa.Float(), nullable=True),
            sa.Column('listed_price', sa.Float(), nullable=True),
            sa.Column('mrp', sa.Float(), nullable=True),
            sa.Column('best_offer', sa.String(length=255), nullable=True),
            sa.Column('offer_applied', sa.String(length=255), nullable=True),
            sa.Column('offer_combos', sa.JSON(), nullable=True),
            sa.Column('net_price', sa.Float(), nullable=True),
            sa.Column('in_stock', sa.Boolean(), nullable=True),
            sa.Column('stock_text', sa.String(length=128), nullable=True),
            sa.Column('last_checked_at', sa.DateTime(), nullable=True),
            sa.Column('last_error', sa.String(length=500), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
    if not _has('inventory_batches'):
        op.create_table(
            'inventory_batches',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('name', sa.String(length=120), nullable=False),
            sa.Column('platform', sa.String(length=32), nullable=False),
            sa.Column('product_name', sa.String(length=255), nullable=False),
            sa.Column('opening_qty', sa.Integer(), nullable=False),
            sa.Column('received_qty', sa.Integer(), nullable=False),
            sa.Column('approved_qty', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(length=32), nullable=False),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('created_by', sa.String(length=120), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_inventory_batches_platform', 'inventory_batches', ['platform'], unique=False)
        op.create_index('ix_inventory_batches_product_name', 'inventory_batches', ['product_name'], unique=False)
        op.create_index('ix_inventory_batches_status', 'inventory_batches', ['status'], unique=False)
    if not _has('inventory_transfers'):
        op.create_table(
            'inventory_transfers',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('reference', sa.String(length=120), nullable=False),
            sa.Column('platform', sa.String(length=32), nullable=False),
            sa.Column('product_name', sa.String(length=255), nullable=False),
            sa.Column('quantity', sa.Integer(), nullable=False),
            sa.Column('destination', sa.String(length=255), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('created_by', sa.String(length=120), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_inventory_transfers_platform', 'inventory_transfers', ['platform'], unique=False)
        op.create_index('ix_inventory_transfers_product_name', 'inventory_transfers', ['product_name'], unique=False)


def downgrade() -> None:
    if _has('inventory_transfers'):
        op.drop_table('inventory_transfers')
    if _has('inventory_batches'):
        op.drop_table('inventory_batches')
    if _has('screener_items'):
        op.drop_table('screener_items')
