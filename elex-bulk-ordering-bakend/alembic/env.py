"""How an Alembic run reaches the database (docs/DEPLOY.md §5).

The address is the app's own: ``ORDERING_DATABASE_URL`` (or the ``.env``
beside the app) through app.config, the value app.db.base builds the app's
engine from, never a line in alembic.ini. So a migration can only reach the
database the app itself would use. A program that already holds a connection
(the test suite) passes it as ``config.attributes["connection"]`` instead:
see app.db.migrations.alembic_config.

The models are the target: autogenerate and ``alembic check`` compare the
database with ``Base.metadata``.

This folder must never get an ``__init__.py``: it shares the name "alembic"
with the package itself, and as a package it would hide that package from
everything run in backend/.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, event, pool

from app.config import get_settings
from app.db import models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base
from app.db.migrations import COMPARE_OPTIONS

config = context.config

# The ini's logging is for the command line only. Called from inside another
# program (the test suite), it would replace that program's own logging set-up
# and quieten loggers it has already made; and even on the command line the
# app's loggers, made at import above, are kept.
if config.cmd_opts is not None and config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

#: Every revision is written in batch mode, on any database. SQLite can alter
#: almost nothing in place, so batch mode rebuilds the table there; on
#: PostgreSQL the same block emits plain ALTER statements. A revision
#: generated against either database therefore runs on both: on SQLite in the
#: tests, on PostgreSQL in production.
RENDER_AS_BATCH = True


def _refuse_a_relative_sqlite_file_elsewhere(url: str) -> None:
    """Stop before touching a database the app would not use.

    The default address, ``sqlite:///./ordering.db``, is a file relative to
    the folder alembic runs in. Run from anywhere but backend/, it would
    quietly create or migrate some other ordering.db there. An absolute
    path, or any other database, is the operator's explicit choice.
    """
    from pathlib import Path

    from sqlalchemy.engine import make_url

    from app.db.migrations import BACKEND_DIR

    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        return
    if Path(parsed.database).is_absolute():
        return
    if Path.cwd().resolve() != BACKEND_DIR.resolve():
        raise SystemExit(
            f"refusing to run: the database address {url!r} is relative to the folder you are in "
            f"({Path.cwd()}), not backend/. Run alembic from {BACKEND_DIR}, or set ORDERING_DATABASE_URL."
        )


def _engine():
    """An engine for one migration run, made like the app's (app.db.base),
    with three differences that matter here.

    * Every PostgreSQL session is pinned to UTC, as the app's are: the plain
      timestamp columns hold UTC instants, and a data migration that writes a
      ``now()`` must write the same instant the app would.
    * SQLite does NOT enforce foreign keys here, while the app's engine asks it
      to on every connection: a batch rebuild drops and re-creates a table
      that other rows point at, and with enforcement on that drop fails (or
      deletes). Alembic's guidance is to leave enforcement off for batch work.
    * On SQLite each migration step is one real transaction, DDL included.
      Left to itself, Python's sqlite3 driver opens a transaction only at the
      first INSERT, UPDATE or DELETE, so a rebuild's ``CREATE TABLE
      _alembic_tmp_<table>`` is saved the moment it runs: a step that fails
      after it (refused by the drop guard, a "database is locked") rolls back
      everything but that table, and every retry stops on "already exists".
      SQLAlchemy's documented recipe for the driver takes over the BEGIN, so
      a failed step leaves the file exactly as it was. (A program that hands
      in its own connection keeps whatever its own engine does.)

    Statement parameters stay out of error messages, as in the app: an order
    row's carry the card number (audit F-7).
    """
    url = get_settings().database_url
    _refuse_a_relative_sqlite_file_elsewhere(url)
    engine = create_engine(
        url,
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    if engine.dialect.name == "postgresql":

        @event.listens_for(engine, "connect")
        def _pin_session_to_utc(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.close()

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _leave_begin_to_sqlalchemy(dbapi_connection, _record) -> None:
            # The driver stops starting transactions itself (it would start
            # none before DDL)...
            dbapi_connection.isolation_level = None

        @event.listens_for(engine, "begin")
        def _begin_for_real(conn) -> None:
            # ...and every transaction SQLAlchemy begins starts here, at once,
            # so the DDL that follows is inside it.
            conn.exec_driver_sql("BEGIN")

    return engine


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=RENDER_AS_BATCH,
        **COMPARE_OPTIONS,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """``--sql``: write the SQL out instead of running it, for review.

    No connection is made; the dialect comes from the address alone.
    """
    _configure(
        url=get_settings().database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        # The caller's connection, and the caller's transaction when it holds
        # one: Alembic commits only a transaction it began itself.
        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = _engine()
    try:
        with engine.connect() as connection:
            _configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
