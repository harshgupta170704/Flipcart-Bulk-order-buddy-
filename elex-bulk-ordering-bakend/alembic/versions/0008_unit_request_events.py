"""unit_request_events: every step of a cancel or reschedule request

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-27

Spec C-4 records a request "with a reason and status log". The request row
holds only where it stands now, so opening a request again erased who had
closed it and when. This revision adds the log:

* the ``unit_request_events`` table — the request, the status the step left
  it in, who took the step, when, and the note written with it — indexed on
  its request, exactly as create_all builds it.

Nothing is filled here: the API writes the steps of requests older than the
table at its next start, from what their rows still say
(app.core.unit_requests.backfill_history), on SQLite and PostgreSQL alike.

The table is made only where it is missing, as in revision 0004: init_db
makes it by itself (create_all), so a database an API of this build already
started on has it, and this revision then has nothing to do. ``--sql``
(offline) writes it out, since there is no database to look at. The
downgrade drops it, the log with it.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'unit_request_events'


def upgrade() -> None:
    """Upgrade schema."""
    if not context.is_offline_mode() and TABLE in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('request_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('actor', sa.String(length=255), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['request_id'], ['unit_requests.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_unit_request_events_request_id'), TABLE, ['request_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema: the table, with the log it holds.

    Dropping a table meets the app's guard
    (app.db.base.refuse_drop_unless_throwaway): refused on any database but a
    throwaway one unless ORDERING_ALLOW_DROP=1 says so, on purpose.
    """
    op.drop_table(TABLE)
