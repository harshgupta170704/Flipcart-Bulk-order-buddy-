"""Moving the laptop's database to a hosted PostgreSQL (Neon), 11 Nov 2026.

The address rules and the refusals run everywhere. The copy itself needs a
PostgreSQL server it may create and drop databases on: name one in
``ORDERING_COPY_TEST_SERVER_URL`` (``postgresql://user@host:port/postgres``)
and these tests make a database of their own there for each test, and drop it
after. Never a server holding real data: the tests create and drop databases.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from app.config import Settings, normalise_database_url
from app.db import copy_database as tool
from app.db.base import engine as app_engine
from app.db.base import engine_options
from app.db.models import Account, AccountStatus, Alert, Job, JobType, Order, OrderStatus

SERVER = os.environ.get("ORDERING_COPY_TEST_SERVER_URL", "").strip()
needs_server = pytest.mark.skipif(not SERVER, reason="set ORDERING_COPY_TEST_SERVER_URL to a throwaway PostgreSQL server")


# --- the address -------------------------------------------------------------------
@pytest.mark.parametrize(
    "given",
    ["postgresql://u:p@ep-x.ap-southeast-1.aws.neon.tech/neondb?sslmode=require", "postgres://u:p@h/db"],
)
def test_a_hosts_own_postgres_address_works_as_given(given):
    assert normalise_database_url(given).startswith("postgresql+psycopg://")
    assert Settings(database_url=given).database_url.startswith("postgresql+psycopg://")


def test_other_addresses_are_kept_as_they_are():
    for url in ("sqlite:///./ordering.db", "postgresql+psycopg://u@h/db", "sqlite://"):
        assert normalise_database_url(url) == url


def test_a_postgres_engine_tests_its_connections_and_gives_up_on_a_silent_server():
    options = engine_options("postgresql+psycopg://u@h/db")
    assert options["pool_pre_ping"] is True
    assert options["connect_args"]["connect_timeout"] > 0
    assert "pool_pre_ping" not in engine_options("sqlite:///x.db")


def test_the_new_address_is_read_from_the_env_file_and_never_shown(monkeypatch, tmp_path):
    monkeypatch.delenv(tool.TARGET_SETTING, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"ORDERING_JWT_SECRET=x\n{tool.TARGET_SETTING}=postgresql://owner:s3cret@ep-x.neon.tech/neondb?sslmode=require\n")

    url = tool.target_url(env)

    assert url.startswith("postgresql+psycopg://")
    assert tool.safe_host(url) == "ep-x.neon.tech/neondb"
    assert "s3cret" not in tool.safe_host(url)


def test_a_settings_file_already_pointing_the_app_at_postgres_is_the_target(monkeypatch, tmp_path):
    """The owner switched ORDERING_DATABASE_URL to Neon before the copy; this
    run reads the SQLite file, named in the environment, and copies into the
    database the file names."""
    monkeypatch.delenv(tool.TARGET_SETTING, raising=False)
    monkeypatch.setenv("ORDERING_DATABASE_URL", "sqlite:////tmp/ordering.db")
    env = tmp_path / ".env"
    env.write_text("ORDERING_DATABASE_URL=postgresql://owner:s3cret@ep-x.neon.tech/neondb?sslmode=require\n")

    assert tool.safe_host(tool.target_url(env)) == "ep-x.neon.tech/neondb"


def test_the_apps_own_postgres_is_never_its_own_target(monkeypatch, tmp_path):
    """Without the SQLite file named for this run, the app's database is the
    source, and the same database cannot be the target."""
    monkeypatch.delenv(tool.TARGET_SETTING, raising=False)
    monkeypatch.delenv("ORDERING_DATABASE_URL", raising=False)
    env = tmp_path / ".env"
    env.write_text("ORDERING_DATABASE_URL=postgresql://owner:s3cret@ep-x.neon.tech/neondb\n")
    with pytest.raises(tool.CopyRefused, match="no address for the new database"):
        tool.target_url(env)


def test_no_address_is_refused_in_words(monkeypatch, tmp_path):
    monkeypatch.delenv(tool.TARGET_SETTING, raising=False)
    with pytest.raises(tool.CopyRefused, match="add a line ORDERING_NEW_DATABASE_URL="):
        tool.target_url(tmp_path / "missing.env")


def test_an_address_that_is_not_postgres_is_refused(monkeypatch):
    monkeypatch.setenv(tool.TARGET_SETTING, "sqlite:///other.db")
    with pytest.raises(tool.CopyRefused, match="must be a PostgreSQL address"):
        tool.target_url()


def test_a_server_that_does_not_answer_is_said_in_words_without_the_password(monkeypatch):
    url = "postgresql+psycopg://owner:s3cret@127.0.0.1:9/neondb"
    monkeypatch.setattr("app.db.base.PG_CONNECT_TIMEOUT", 2)
    with pytest.raises(tool.CopyRefused) as refused:
        tool.check(app_engine, url)
    assert "s3cret" not in str(refused.value)
    assert "OperationalError" in str(refused.value)


# --- the copy, on a real PostgreSQL -------------------------------------------------
@pytest.fixture
def target():
    """A database of the test's own on the throwaway server, dropped after."""
    name = f"copytest_{uuid.uuid4().hex[:10]}"
    admin = create_engine(normalise_database_url(SERVER), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(normalise_database_url(SERVER)).set(database=name).render_as_string(hide_password=False)
    yield url
    with admin.connect() as connection:
        connection.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}'"))
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    admin.dispose()


def _seed(db) -> None:
    account = Account(email="buyer@example.com", phone="9000000001", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    job = Job(type=JobType.order)
    db.add(job)
    db.commit()
    for status in (OrderStatus.placed, OrderStatus.over_target, OrderStatus.unconfirmed, OrderStatus.failed):
        db.add(
            Order(
                account_id=account.id,
                job_id=job.id,
                status=status,
                qty=2,
                amount=638.0,
                model="x" * 600,  # longer than its column: SQLite kept it, PostgreSQL would refuse it
                order_id="OD338996689296590100" if status == OrderStatus.placed else None,
                placed_at=datetime(2026, 10, 27, 14, 33, 15, 493773),
            )
        )
    db.commit()


@needs_server
def test_every_row_is_copied_with_its_ids_times_and_statuses(db, target):
    if app_engine.dialect.name != "sqlite":
        pytest.skip("the copy reads the suite's own SQLite database")
    _seed(db)
    from app.core import alerts

    alerts.evaluate(db)
    said = []

    report = tool.copy(app_engine, target, say=said.append)

    assert report.revision is not None
    assert report.timezone_set
    assert report.tables["orders"] == 4 and report.tables["order_units"] == 8
    assert report.cut == {"orders.model": 4}
    copied = create_engine(target)
    with copied.connect() as connection:
        rows = connection.execute(text("SELECT id, status, placed_at, order_id FROM orders ORDER BY id")).all()
        assert [row.status for row in rows] == ["placed", "over_target", "unconfirmed", "failed"]
        assert rows[0].placed_at == datetime(2026, 10, 27, 14, 33, 15, 493773)
        assert connection.execute(text("SELECT count(*) FROM alerts")).scalar() == db.query(Alert).count()
        # A brand-new connection — no app setting, as through a pooler — reads UTC.
        assert connection.execute(text("SHOW TimeZone")).scalar() == "UTC"
        # The next row the app adds does not collide with a copied one.
        nxt = connection.execute(text("SELECT nextval(pg_get_serial_sequence('orders', 'id'))")).scalar()
        assert nxt == max(row.id for row in rows) + 1
    copied.dispose()
    assert not any("buyer@example.com" in line or "s3cret" in line for line in said)


@needs_server
def test_a_database_that_already_holds_rows_is_never_written_to(db, target):
    if app_engine.dialect.name != "sqlite":
        pytest.skip("the copy reads the suite's own SQLite database")
    _seed(db)
    tool.copy(app_engine, target, say=lambda _line: None)

    with pytest.raises(tool.CopyRefused, match="already holds rows"):
        tool.copy(app_engine, target, say=lambda _line: None)
    with pytest.raises(tool.CopyRefused, match="already holds rows"):
        tool.check(app_engine, target)


@needs_server
def test_a_copy_that_fails_part_way_leaves_nothing_behind(db, target, monkeypatch):
    if app_engine.dialect.name != "sqlite":
        pytest.skip("the copy reads the suite's own SQLite database")
    _seed(db)
    real_move = tool._move_counter

    def breaks_at_orders(connection, table):
        if table.name == "orders":
            raise RuntimeError("the connection dropped")
        real_move(connection, table)

    monkeypatch.setattr(tool, "_move_counter", breaks_at_orders)
    with pytest.raises(RuntimeError):
        tool.copy(app_engine, target, say=lambda _line: None)

    copied = create_engine(target)
    with copied.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")).scalar() == 0
    copied.dispose()
    assert tool.check(app_engine, target)["rows"]["orders"] == 4  # and it is still ready for the copy


@needs_server
def test_the_app_reconnects_after_the_server_closes_its_connections(target):
    """What a hosted database does when it sleeps after a quiet spell."""
    options = engine_options(target)
    engine = create_engine(target, **options)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    admin = create_engine(normalise_database_url(SERVER), isolation_level="AUTOCOMMIT")
    name = make_url(target).database
    with admin.connect() as connection:
        connection.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}'"))
    admin.dispose()

    with engine.connect() as connection:
        assert connection.execute(select(1)).scalar() == 1
    engine.dispose()


@needs_server
def test_a_database_the_app_was_started_against_once_is_still_copied_into(db, target):
    """The app, started against the empty database, built its tables and its
    admin sign-in; that row alone does not stop the copy, and is replaced."""
    if app_engine.dialect.name != "sqlite":
        pytest.skip("the copy reads the suite's own SQLite database")
    _seed(db)
    from app.db.base import Base

    started = create_engine(target)
    Base.metadata.create_all(started)
    with started.begin() as connection:
        connection.execute(text("INSERT INTO users (email, name, password_hash, role, active, must_change_password, created_at, updated_at) VALUES ('admin', 'Administrator', 'x', 'admin', true, false, now(), now())"))
    assert tool.check(app_engine, target)["rows"]["orders"] == 4

    report = tool.copy(app_engine, target, say=lambda _line: None)

    assert report.startup_admin_replaced and report.tables["orders"] == 4
    with started.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM users")).scalar() == db.execute(text("SELECT count(*) FROM users")).scalar()
    started.dispose()


@needs_server
def test_a_used_sign_in_is_never_removed(db, target):
    if app_engine.dialect.name != "sqlite":
        pytest.skip("the copy reads the suite's own SQLite database")
    _seed(db)
    from app.db.base import Base

    started = create_engine(target)
    Base.metadata.create_all(started)
    with started.begin() as connection:
        connection.execute(text("INSERT INTO users (email, name, password_hash, role, active, must_change_password, created_at, updated_at, last_login_at) VALUES ('admin', 'Administrator', 'x', 'admin', true, false, now(), now(), now())"))
    started.dispose()
    with pytest.raises(tool.CopyRefused, match="already holds rows"):
        tool.copy(app_engine, target, say=lambda _line: None)
