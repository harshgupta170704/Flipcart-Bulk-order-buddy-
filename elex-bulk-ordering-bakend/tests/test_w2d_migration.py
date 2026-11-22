"""Revision 0005 and the start-up sync for an account's GST and billing phone.

Two ways the three new columns reach a database, and both must end right:

* Alembic (a PostgreSQL deployment): revision 0005 adds the columns, the
  index and the foreign key — or, on a database init_db already gave the bare
  columns, only what is missing, the foreign key among it.
* init_db (the laptop's SQLite file): sync_missing_columns adds the three
  nullable columns and sync_missing_indexes the index, never a foreign key —
  so deleting a GST must unlink its accounts in the app itself.

Each test works on a temporary SQLite file of its own; the app's engine is
pointed at it only where the start-up sync is under test, and the app's
settings only where the alembic command line's own engine is. Alembic must be
installed for the revision tests (tests/test_w1c_migrations.py skips alike).
"""

from __future__ import annotations

import contextlib
import importlib.util

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.api import gsts as gsts_api
from app.config import get_settings
from app.db import base as db_base
from app.db import migrations
from app.db.base import Base
from tests.test_db_safety import judged_as_the_real_file


def _alembic_installed() -> bool:
    try:
        return importlib.util.find_spec("alembic.command") is not None
    except ImportError:
        return False


needs_alembic = pytest.mark.skipif(not _alembic_installed(), reason="alembic is not installed")


@pytest.fixture
def scratch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'w2d.db'}", poolclass=NullPool)
    yield engine
    engine.dispose()


def _alembic(engine, name: str, *args) -> None:
    from alembic import command

    with engine.begin() as connection:
        getattr(command, name)(migrations.alembic_config(connection), *args)


def _differences(engine) -> list:
    """What autogenerate would still write about this package's columns, their
    index and their foreign key: [] when they match the models. Only these —
    the whole schema is tests/test_w1c_migrations.py's to hold to the models,
    and the revisions merged beside this one are theirs to answer for."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts=dict(migrations.COMPARE_OPTIONS))
        found = compare_metadata(context, Base.metadata)
    ours = ("billing_phone", "gst_id")
    return [difference for difference in found if any(name in repr(difference) for name in ours)]


def _columns(engine, table: str) -> dict[str, dict]:
    return {column["name"]: column for column in inspect(engine).get_columns(table)}


def _gst_foreign_keys(engine) -> list[tuple]:
    """accounts' foreign keys onto gsts, as (from, to, on delete)."""
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA foreign_key_list('accounts')").all()
    return [(row[3], row[4], row[6]) for row in rows if row[2] == "gsts"]


def _start_up_sync(engine, monkeypatch) -> list[str]:
    """What init_db does to an existing database's columns and indexes."""
    monkeypatch.setattr(db_base, "engine", engine)
    added = db_base.sync_missing_columns()
    added += db_base.sync_missing_indexes()
    return added


def _insert_gst_and_accounts(engine, *, gst_id: int = 1, account_ids=(1, 2)) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gsts (id, label, gstin, business_name, is_default, created_at) "
                "VALUES (:id, 'SONIC', '33AAECS4321F1ZK', 'Sonic Kommerce', 0, CURRENT_TIMESTAMP)"
            ),
            {"id": gst_id},
        )
        for n in account_ids:
            conn.execute(
                text(
                    "INSERT INTO accounts (id, email, status, gst_id, created_at, updated_at) "
                    "VALUES (:n, :email, 'new', :gst, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"n": n, "email": f"a{n}@test.com", "gst": gst_id},
            )


# --- what the start-up sync adds to an older database ----------------------------------
def test_init_db_adds_the_three_columns_to_an_older_database_without_a_foreign_key(scratch, monkeypatch):
    """Tables as a build before this package left them (the columns that
    matter here, at least): the sync adds the three, nullable, and the index —
    and no foreign key, which it never does."""
    with scratch.begin() as conn:
        conn.execute(text("CREATE TABLE accounts (id INTEGER NOT NULL PRIMARY KEY, email VARCHAR(255) NOT NULL)"))
        conn.execute(text("CREATE TABLE orders (id INTEGER NOT NULL PRIMARY KEY, account_id INTEGER NOT NULL)"))
    Base.metadata.tables["gsts"].create(scratch)

    added = _start_up_sync(scratch, monkeypatch)

    assert {"accounts.gst_id", "accounts.billing_phone", "orders.billing_phone", "accounts.ix_accounts_gst_id"} <= set(added)
    accounts = _columns(scratch, "accounts")
    assert accounts["gst_id"]["nullable"] and accounts["billing_phone"]["nullable"]
    assert _columns(scratch, "orders")["billing_phone"]["nullable"]
    assert _gst_foreign_keys(scratch) == []  # the sync never adds one


def test_without_the_foreign_key_deleting_a_gst_still_unlinks_its_accounts(scratch):
    """The laptop's case: nothing in the database sets the column to NULL —
    here because this engine, unlike the app's, never asks SQLite to enforce
    foreign keys — so the app does, in the same transaction as the delete."""
    Base.metadata.create_all(scratch)
    _insert_gst_and_accounts(scratch, gst_id=7, account_ids=(5,))
    with scratch.begin() as conn:  # the database alone leaves the account naming a GST that is gone
        conn.execute(text("DELETE FROM gsts WHERE id = 7"))
        assert conn.execute(text("SELECT gst_id FROM accounts WHERE id = 5")).scalar() == 7
    _insert_gst_and_accounts(scratch)

    with Session(scratch) as session:
        gsts_api.delete_gst(1, db=session)

    with scratch.connect() as conn:
        assert conn.execute(text("SELECT id, gst_id FROM accounts WHERE id IN (1, 2) ORDER BY id")).all() == [
            (1, None),
            (2, None),
        ]
        assert conn.execute(text("SELECT COUNT(*) FROM gsts")).scalar() == 0


def _insert_kept_and_dangling_accounts(engine) -> None:
    """GST 1, an account linked to it with a phone, one naming a GST that is
    gone (99), and an order placed with the phone."""
    _insert_gst_and_accounts(engine, account_ids=())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (id, email, status, gst_id, billing_phone, created_at, updated_at) VALUES "
                "(1, 'kept@test.com', 'new', 1, '9811111111', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "(2, 'dangling@test.com', 'new', 99, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO orders (id, account_id, payment_mode, status, billing_phone) "
                "VALUES (1, 1, 'cod', 'placed', '9811111111')"
            )
        )


def _the_rows_are_kept_and_the_dangling_id_nulled(engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id, gst_id, billing_phone FROM accounts ORDER BY id")).all() == [
            (1, 1, "9811111111"),
            (2, None, None),
        ]
        assert conn.execute(text("SELECT billing_phone FROM orders")).scalar() == "9811111111"


# --- revision 0005 -------------------------------------------------------------------------
@needs_alembic
@pytest.mark.parametrize("laptops_file", [False, True], ids=["temp-file", "laptops-file"])
def test_0005_gives_a_column_init_db_added_its_foreign_key_and_keeps_the_rows(scratch, monkeypatch, laptops_file):
    """A file init_db kept up to date, then taken over by Alembic: the columns
    are there, the foreign key is not. 0005 adds only that, nulls an id whose
    GST is gone (what the key would have done), and keeps every row.

    Run on a temp file, and on one the drop guard judges as the laptop's own
    (not a throwaway, no ORDERING_ALLOW_DROP): adding the key rebuilds
    ``accounts`` on SQLite — a copy, a drop, a rename — and the guard must let
    Alembic swap the table for its full copy."""
    with judged_as_the_real_file(scratch.url) if laptops_file else contextlib.nullcontext():
        _alembic(scratch, "upgrade", "0002")
        _start_up_sync(scratch, monkeypatch)
        _insert_kept_and_dangling_accounts(scratch)

        _alembic(scratch, "upgrade", "head")

    assert _gst_foreign_keys(scratch) == [("gst_id", "id", "SET NULL")]
    _the_rows_are_kept_and_the_dangling_id_nulled(scratch)
    assert _differences(scratch) == []


def _command_line(name: str, *args) -> None:
    """``alembic <name>`` as an operator runs it from backend/: no connection
    handed in, so env.py makes its own engine from the app's settings."""
    from alembic import command

    getattr(command, name)(migrations.alembic_config(), *args)


@needs_alembic
def test_the_documented_way_to_put_the_laptops_file_under_alembic(tmp_path, monkeypatch):
    """docs/DEPLOY.md §5, "A database init_db built", step by step, on a file
    the drop guard judges as the laptop's own: made by an older build and never
    stamped, kept up to date by init_db since (new tables, columns and
    indexes, never a foreign key); then, the app stopped, ``alembic stamp
    0004``, ``alembic upgrade head`` and ``alembic check`` — with no
    ORDERING_ALLOW_DROP, which the procedure never mentions. The guard used to
    refuse 0005's rebuild of ``accounts`` there, and the procedure could not
    finish."""
    url = f"sqlite:///{tmp_path / 'ordering.db'}"
    engine = create_engine(url, poolclass=NullPool)
    try:
        _alembic(engine, "upgrade", "0001")  # an older build's tables...
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE {migrations.VERSION_TABLE}"))  # ...never stamped
        Base.metadata.create_all(engine)  # and init_db at every start since
        _start_up_sync(engine, monkeypatch)
        _insert_kept_and_dangling_accounts(engine)

        with judged_as_the_real_file(url) as patch:
            patch.setattr(get_settings(), "database_url", url)
            _command_line("stamp", "0004")
            _command_line("upgrade", "head")
            _command_line("check")  # raises when the database and the models differ

        assert _gst_foreign_keys(engine) == [("gst_id", "id", "SET NULL")]
        _the_rows_are_kept_and_the_dangling_id_nulled(engine)
        tables = inspect(engine).get_table_names()
        assert not [name for name in tables if name.startswith(db_base.ALEMBIC_COPY_PREFIX)]
        with engine.connect() as conn:
            assert conn.execute(text(f"SELECT version_num FROM {migrations.VERSION_TABLE}")).scalar() == (
                migrations.head_revision()
            )
    finally:
        engine.dispose()


@needs_alembic
def test_0005_down_and_up_again(scratch):
    _alembic(scratch, "upgrade", "head")
    with scratch.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (id, email, status, created_at, updated_at) "
                "VALUES (1, 'stays@test.com', 'new', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    _alembic(scratch, "downgrade", "0002")

    assert not {"gst_id", "billing_phone"} & set(_columns(scratch, "accounts"))
    assert "billing_phone" not in _columns(scratch, "orders")
    assert "ix_accounts_gst_id" not in {ix["name"] for ix in inspect(scratch).get_indexes("accounts")}
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT email FROM accounts")).scalar() == "stays@test.com"

    _alembic(scratch, "upgrade", "head")
    assert _differences(scratch) == []
    assert _gst_foreign_keys(scratch) == [("gst_id", "id", "SET NULL")]


@needs_alembic
def test_the_comparison_above_would_see_a_missing_foreign_key(scratch, monkeypatch):
    """The scoped comparison is not blind: at 0002 plus the start-up sync, the
    foreign key the models declare is missing, and it says so."""
    _alembic(scratch, "upgrade", "0002")
    _start_up_sync(scratch, monkeypatch)

    assert any("gst_id" in repr(difference) for difference in _differences(scratch))
