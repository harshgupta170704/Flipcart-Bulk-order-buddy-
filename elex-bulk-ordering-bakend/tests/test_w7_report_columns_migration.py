"""Revision 0010 and the start-up sync for what an order report says of a
unit's refund, its courier and who cancelled it (spec FR-022, review of
27 Oct 2026).

Two ways the six new ``order_units`` columns reach a database, and both must
end right:

* Alembic (a PostgreSQL deployment): revision 0010 adds them — or, on a
  database init_db already gave some of them, only what is missing — and its
  downgrade takes them away again, keeping the rows.
* init_db (the laptop's SQLite file): sync_missing_columns adds them, nullable.

The whole schema is tests/test_w1c_migrations.py's to hold to the models, on
SQLite and PostgreSQL. Each test here works on a temporary SQLite file of its
own; the app's engine is pointed at it only where the start-up sync is under
test. Alembic must be installed for the revision tests (test_w1c skips alike).
"""

from __future__ import annotations

import importlib.util

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from app.db import base as db_base
from app.db import migrations

NEW = {
    "refund_status": "VARCHAR(64)",
    "refund_amount": "FLOAT",
    "refund_method": "VARCHAR(100)",
    "shipment_partner": "VARCHAR(128)",
    "last_tracking_text": "VARCHAR(255)",
    "cancelled_by_user": "BOOLEAN",
}


def _alembic_installed() -> bool:
    try:
        return importlib.util.find_spec("alembic.command") is not None
    except ImportError:
        return False


needs_alembic = pytest.mark.skipif(not _alembic_installed(), reason="alembic is not installed")


@pytest.fixture
def scratch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'w7.db'}", poolclass=NullPool)
    yield engine
    engine.dispose()


def _alembic(engine, name: str, revision: str) -> None:
    from alembic import command

    with engine.begin() as connection:
        getattr(command, name)(migrations.alembic_config(connection), revision)


def _unit_columns(engine) -> dict[str, dict]:
    return {column["name"]: column for column in inspect(engine).get_columns("order_units")}


def _a_unit(engine) -> None:
    """One account, order and unit, as a database at 0009 holds them."""
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO accounts (id, email, status, created_at, updated_at) VALUES (1, 'a@test.com', 'new', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        )
        conn.execute(
            text("INSERT INTO orders (id, account_id, payment_mode, status, order_id) VALUES (1, 1, 'unknown', 'placed', 'OD338967210239333101')")
        )
        conn.execute(
            text(
                "INSERT INTO order_units (id, order_pk, platform, position, unit_id, source, platform_status, "
                "created_at, updated_at) VALUES (1, 1, 'flipkart', 1, '439123456789012345601', 'upload', 'Cancelled', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )


@needs_alembic
def test_0010_adds_the_six_columns_and_its_downgrade_takes_them_away(scratch):
    _alembic(scratch, "upgrade", "0009")
    _a_unit(scratch)
    assert not set(NEW) & set(_unit_columns(scratch))

    _alembic(scratch, "upgrade", "0010")

    columns = _unit_columns(scratch)
    assert {name: str(columns[name]["type"]) for name in NEW} == NEW
    assert all(columns[name]["nullable"] for name in NEW)
    with scratch.begin() as conn:
        conn.execute(
            text(
                "UPDATE order_units SET refund_status = 'Refund Completed', refund_amount = 18207.0, "
                "shipment_partner = 'Ekart Logistics', cancelled_by_user = 1 WHERE id = 1"
            )
        )

    _alembic(scratch, "downgrade", "0009")

    assert not set(NEW) & set(_unit_columns(scratch))
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT unit_id, platform_status FROM order_units")).all() == [
            ("439123456789012345601", "Cancelled")
        ]


@needs_alembic
def test_0010_adds_only_what_the_start_up_sync_has_not(scratch):
    """A database the new API started on before the migration ran: init_db
    gave it some of the columns already, and adding them again would stop on
    "already exists"."""
    _alembic(scratch, "upgrade", "0009")
    with scratch.begin() as conn:
        conn.execute(text("ALTER TABLE order_units ADD COLUMN refund_status VARCHAR(64)"))
        conn.execute(text("ALTER TABLE order_units ADD COLUMN cancelled_by_user BOOLEAN"))

    _alembic(scratch, "upgrade", "0010")

    assert set(NEW) <= set(_unit_columns(scratch))
    with scratch.connect() as conn:
        assert conn.execute(text(f"SELECT version_num FROM {migrations.VERSION_TABLE}")).scalar() == "0010"


@needs_alembic
def test_the_start_up_sync_adds_the_six_columns_to_an_older_database(scratch, monkeypatch):
    """The laptop's file is never migrated by Alembic: init_db's
    sync_missing_columns gives its order_units the six columns, nullable."""
    _alembic(scratch, "upgrade", "0009")
    _a_unit(scratch)
    monkeypatch.setattr(db_base, "engine", scratch)

    added = db_base.sync_missing_columns()

    assert {f"order_units.{name}" for name in NEW} <= set(added)
    columns = _unit_columns(scratch)
    assert all(columns[name]["nullable"] for name in NEW)
    with scratch.connect() as conn:
        assert conn.execute(text("SELECT shipment_partner, cancelled_by_user FROM order_units")).all() == [(None, None)]
