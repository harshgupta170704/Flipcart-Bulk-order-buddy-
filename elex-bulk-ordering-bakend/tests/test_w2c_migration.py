"""Revision 0004 (unit_events and users.changes_seen_at) on the way down.

The downgrade drops a whole table, so on a database that is not a throwaway
it meets the drop guard (app.db.base.refuse_drop_unless_throwaway) and is
refused unless ORDERING_ALLOW_DROP=1 says so. It must be refused before it
has changed anything: the retry the refusal asks for has to go through.

Each test works on a temporary SQLite file of its own, handed to Alembic as a
connection whose driver saves each DDL statement the moment it runs (the
command line runs a step in one transaction, alembic/env.py; a program
passing its own connection does not), so whatever a refused step left behind
shows here. Alembic must be installed (tests/test_w1c_migrations.py skips alike).
"""

from __future__ import annotations

import importlib.util

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from app.db import base as db_base
from app.db import migrations
from tests.test_db_safety import judged_as_the_real_file


def _alembic_installed() -> bool:
    try:
        return importlib.util.find_spec("alembic.command") is not None
    except ImportError:
        return False


needs_alembic = pytest.mark.skipif(not _alembic_installed(), reason="alembic is not installed")


@pytest.fixture
def scratch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'feed.db'}", poolclass=NullPool)
    yield engine
    engine.dispose()


def _alembic(engine, name: str, revision: str) -> None:
    from alembic import command

    with engine.begin() as connection:
        getattr(command, name)(migrations.alembic_config(connection), revision)


def _feed_schema(engine) -> tuple[str | None, bool, bool, list[str]]:
    """(the revision, whether unit_events is there, whether users has
    changes_seen_at, any copy Alembic left behind)."""
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    with engine.connect() as conn:
        revision = conn.execute(text(f"SELECT version_num FROM {migrations.VERSION_TABLE}")).scalar()
    seen = "changes_seen_at" in {column["name"] for column in inspector.get_columns("users")}
    copies = [name for name in tables if name.startswith(db_base.ALEMBIC_COPY_PREFIX)]
    return revision, "unit_events" in tables, seen, copies


@needs_alembic
def test_the_downgrade_is_refused_before_it_changes_anything(scratch):
    """On a file the guard judges as the laptop's, dropping unit_events is
    refused. The table goes first, so the refusal comes before any statement:
    ``users`` is not half rebuilt and no ``_alembic_tmp_users`` is left to
    stop the retry on "already exists". The retry with the override, as the
    refusal asks, goes through."""
    _alembic(scratch, "upgrade", "0004")

    with judged_as_the_real_file(scratch.url) as patch:
        with pytest.raises(RuntimeError, match="refusing to drop tables"):
            _alembic(scratch, "downgrade", "0003")
        assert _feed_schema(scratch) == ("0004", True, True, [])

        patch.setenv(db_base.ALLOW_DROP_ENV, "1")  # on purpose, and said so
        _alembic(scratch, "downgrade", "0003")

    assert _feed_schema(scratch) == ("0003", False, False, [])


@needs_alembic
def test_the_downgrade_drops_the_column_only_where_it_is(scratch):
    """A database at 0004 whose users table has no changes_seen_at (stamped
    there by hand, say) still goes down, as 0003's and 0006's downgrades do."""
    _alembic(scratch, "upgrade", "0004")
    with scratch.begin() as conn:
        conn.execute(text("ALTER TABLE users DROP COLUMN changes_seen_at"))

    _alembic(scratch, "downgrade", "0003")

    assert _feed_schema(scratch) == ("0003", False, False, [])
