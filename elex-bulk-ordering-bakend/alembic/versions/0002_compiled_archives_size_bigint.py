"""compiled_archives.size: BIGINT on every PostgreSQL database

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26

The model has declared ``compiled_archives.size`` a BigInteger since 24 Oct
2026: a quarter's invoices zip past 2 GiB, more than PostgreSQL's 4-byte
INTEGER holds, and saving such an archive's row fails. init_db never retypes a
column, so a PostgreSQL table created by an older build still has INTEGER, and
until now it needed a hand-run ALTER (docs/DEPLOY.md §5, audit F-33). This
revision is that ALTER, made only where it is needed:

* PostgreSQL, column INTEGER (a table made before the model changed, in a
  database stamped 0001): widened to BIGINT. Every INTEGER value fits, so
  nothing is lost; the table is small (a row per compiled zip).
* PostgreSQL, column already BIGINT (a database built by the baseline, a table
  created after the change, or one already fixed by hand): nothing.
* SQLite: nothing. An INTEGER column there holds 8-byte values whatever its
  declared type says, so the table is right as it stands, and rebuilding it
  only to change the declaration would buy nothing.
* ``--sql`` (offline): the ALTER is written out, since there is no column to
  look at. On a column that is already BIGINT it changes nothing.

The downgrade leaves the column alone: revision 0001 already declares BIGINT,
and narrowing it again would fail on an archive over 2 GiB.
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _size_is_already_bigint() -> bool:
    inspector = sa.inspect(op.get_bind())
    if 'compiled_archives' not in inspector.get_table_names():
        # Nothing to widen; init_db creates the table, already BIGINT.
        return True
    size = next(c for c in inspector.get_columns('compiled_archives') if c['name'] == 'size')
    return isinstance(size['type'], sa.BigInteger)


def upgrade() -> None:
    """Upgrade schema."""
    if op.get_context().dialect.name != 'postgresql':
        return
    if not context.is_offline_mode() and _size_is_already_bigint():
        return
    op.alter_column(
        'compiled_archives',
        'size',
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema: nothing (see above)."""
