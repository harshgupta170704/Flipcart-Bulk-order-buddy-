"""HDFC card automation: card batches, the parent card's Aan, and the SMS relay phones

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-22

* hdfc_user_ids: ``aan`` (the parent card's Alternate A/c No., the first
  column of HDFC's bulk virtual-card sheet) and ``portal_user_id`` (HDFC's
  own "Virtual Card User ID").
* hdfc_child_cards: ``batch_id``, the card batch a card was asked for in.
* new table ``hdfc_card_batches``: a request for virtual cards, sent to the
  CorpAssist portal or downloaded as HDFC's bulk sheet.
* new tables ``otp_phones`` and ``sms_messages``: phones running an SMS
  forwarder app, and the texts they relay (kept for a day, for the OTPs).

As in revisions 0003-0014, every column and table is added only where it is
missing: ``init_db`` adds a model's new nullable columns and new tables at
start-up, so a database the new API started on already has them.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = '0015'
down_revision: Union[str, Sequence[str], None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

USER_ID_COLUMNS = {
    'aan': sa.String(length=32),
    'portal_user_id': sa.String(length=16),
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
    tables = _tables()
    if 'hdfc_card_batches' not in tables:
        op.create_table(
            'hdfc_card_batches',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('status', sa.String(length=16), nullable=False),
            sa.Column('send', sa.String(length=16), nullable=False),
            sa.Column('rows', sa.JSON(), nullable=False),
            sa.Column('card_count', sa.Integer(), nullable=False),
            sa.Column('cards_received', sa.Integer(), nullable=False),
            sa.Column('error', sa.String(length=500), nullable=True),
            sa.Column('portal_report', sa.Text(), nullable=True),
            sa.Column('started_by', sa.String(length=255), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('submitted_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_hdfc_card_batches_created_at', 'hdfc_card_batches', ['created_at'], unique=False)

    present = _columns('hdfc_user_ids')
    if present is not None:
        missing = [name for name in USER_ID_COLUMNS if name not in present]
        if missing:
            with op.batch_alter_table('hdfc_user_ids', schema=None) as batch_op:
                for name in missing:
                    batch_op.add_column(sa.Column(name, USER_ID_COLUMNS[name], nullable=True))

    present = _columns('hdfc_child_cards')
    if present is not None and 'batch_id' not in present:
        with op.batch_alter_table('hdfc_child_cards', schema=None) as batch_op:
            batch_op.add_column(sa.Column('batch_id', sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                'fk_hdfc_child_cards_batch_id', 'hdfc_card_batches', ['batch_id'], ['id'], ondelete='SET NULL'
            )
            batch_op.create_index('ix_hdfc_child_cards_batch_id', ['batch_id'], unique=False)

    if 'otp_phones' not in tables:
        op.create_table(
            'otp_phones',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('label', sa.String(length=64), nullable=False),
            sa.Column('phone_number', sa.String(length=16), nullable=False),
            sa.Column('employee_id', sa.String(length=64), nullable=True),
            sa.Column('corporate_id', sa.String(length=64), nullable=True),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('token_hint', sa.String(length=8), nullable=False),
            sa.Column('active', sa.Boolean(), nullable=False),
            sa.Column('last_ping_at', sa.DateTime(), nullable=True),
            sa.Column('last_sms_at', sa.DateTime(), nullable=True),
            sa.Column('last_sender', sa.String(length=64), nullable=True),
            sa.Column('last_otp', sa.String(length=12), nullable=True),
            sa.Column('last_otp_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_otp_phones_phone_number', 'otp_phones', ['phone_number'], unique=True)
        op.create_index('ix_otp_phones_token_hash', 'otp_phones', ['token_hash'], unique=True)
    if 'sms_messages' not in tables:
        op.create_table(
            'sms_messages',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('phone_id', sa.Integer(), nullable=False),
            sa.Column('sender', sa.String(length=64), nullable=True),
            sa.Column('body', sa.String(length=1000), nullable=False),
            sa.Column('otp', sa.String(length=12), nullable=True),
            sa.Column('received_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['phone_id'], ['otp_phones.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_sms_messages_phone_id', 'sms_messages', ['phone_id'], unique=False)
        op.create_index('ix_sms_messages_received_at', 'sms_messages', ['received_at'], unique=False)


def downgrade() -> None:
    offline = context.is_offline_mode()
    tables = _tables()
    if offline or 'sms_messages' in tables:
        op.drop_index('ix_sms_messages_received_at', table_name='sms_messages')
        op.drop_index('ix_sms_messages_phone_id', table_name='sms_messages')
        op.drop_table('sms_messages')
    if offline or 'otp_phones' in tables:
        op.drop_index('ix_otp_phones_token_hash', table_name='otp_phones')
        op.drop_index('ix_otp_phones_phone_number', table_name='otp_phones')
        op.drop_table('otp_phones')

    present = _columns('hdfc_child_cards')
    if offline or (present is not None and 'batch_id' in present):
        with op.batch_alter_table('hdfc_child_cards', schema=None) as batch_op:
            batch_op.drop_index('ix_hdfc_child_cards_batch_id')
            batch_op.drop_constraint('fk_hdfc_child_cards_batch_id', type_='foreignkey')
            batch_op.drop_column('batch_id')

    present = _columns('hdfc_user_ids')
    if present is not None:
        gone = [name for name in USER_ID_COLUMNS if offline or name in present]
        if gone:
            with op.batch_alter_table('hdfc_user_ids', schema=None) as batch_op:
                for name in reversed(gone):
                    batch_op.drop_column(name)

    if offline or 'hdfc_card_batches' in tables:
        op.drop_index('ix_hdfc_card_batches_created_at', table_name='hdfc_card_batches')
        op.drop_table('hdfc_card_batches')
