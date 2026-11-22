"""Copy the laptop's SQLite database into an empty PostgreSQL one (Neon, say).

Run from the API's folder, with the app stopped:

    python -m app.cli copy-to-postgres --check   # can it reach the new database, and is it empty?
    python -m app.cli copy-to-postgres           # copy

The new database's address is read from ``ORDERING_NEW_DATABASE_URL``, in the
environment or in the ``.env`` beside the app: never typed on the command line
(it would stay in the shell's history) and never printed — only its host is.

What it does, in ONE transaction on the new database, so a failure part-way
leaves it exactly as it was:

* builds the app's tables from its models and records the newest Alembic
  revision on them, as a fresh database built by the app is recorded;
* copies every row of every table, parents before children, in id order;
* moves each table's id counter past the highest id copied, so the next row
  the app adds does not collide with a copied one;
* sets the database's own time zone to UTC — what every stamp here is held in
  — so a pooled connection, which drops the app's per-connection setting,
  reads and writes the same instants (app.db.base.POOLED_HOST_MARK);
* counts the rows of every table on both sides, and commits only when they
  agree.

The SQLite file is only read, never changed: it stays as it is, the way back.
Invoice PDFs and compiled zips are files beside the app, not rows, and do not
move.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, String, create_engine, event, func, inspect, select, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.engine import Connection, make_url

from app.config import normalise_database_url

#: Where the new database's address is read from.
TARGET_SETTING = "ORDERING_NEW_DATABASE_URL"
#: Rows read and written per round trip: few enough to hold in memory, many
#: enough that a hosted database far away is not asked once per row.
BATCH = 500


class CopyRefused(RuntimeError):
    """The copy did not start, or was undone, for a reason worth saying in words."""


@dataclass
class CopyReport:
    host: str
    tables: dict[str, int] = field(default_factory=dict)
    cut: dict[str, int] = field(default_factory=dict)
    revision: str | None = None
    timezone_set: bool = False
    timezone_note: str | None = None
    startup_admin_replaced: bool = False

    @property
    def rows(self) -> int:
        return sum(self.tables.values())


#: The app's own address setting, read from the settings file when the
#: settings file already points the app at the new database.
APP_SETTING = "ORDERING_DATABASE_URL"


def _settings_files(env_file: Path | None) -> list[Path]:
    """The settings files the address may be in: the one named, else the one
    in the folder the command runs in (where the app reads its own) and the
    one beside the code."""
    if env_file is not None:
        return [env_file]
    from app.db.migrations import BACKEND_DIR

    found: list[Path] = []
    for path in (Path.cwd() / ".env", BACKEND_DIR / ".env"):
        if path.is_file() and path.resolve() not in [p.resolve() for p in found]:
            found.append(path)
    return found


def target_url(env_file: Path | None = None) -> str:
    """The new database's address: ``ORDERING_NEW_DATABASE_URL`` from the
    environment, else from the settings file. Failing that, when the settings
    file already names a PostgreSQL database as the app's own
    (``ORDERING_DATABASE_URL``) and this run reads the SQLite file instead —
    named in the environment, which wins over the file — that PostgreSQL is
    the target: an owner who switched the line first need not switch it back
    (11 Nov 2026). Refused, in words, when there is none or it is not
    PostgreSQL."""
    from dotenv import dotenv_values

    raw = os.environ.get(TARGET_SETTING, "").strip()
    files = _settings_files(env_file)
    for path in files:
        if raw:
            break
        raw = (dotenv_values(path).get(TARGET_SETTING) or "").strip()
    if not raw and os.environ.get(APP_SETTING, "").strip().startswith("sqlite"):
        for path in files:
            named = normalise_database_url((dotenv_values(path).get(APP_SETTING) or "").strip().strip("'\""))
            if named.startswith("postgresql"):
                raw = named
                break
    if not raw:
        raise CopyRefused(
            f"no address for the new database: add a line {TARGET_SETTING}=<its connection string> "
            "to the .env beside the app (the direct one, not the pooled one), then run this again"
        )
    url = normalise_database_url(raw.strip("'\""))
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 — never echo what could not be read: it holds a password
        raise CopyRefused(f"{TARGET_SETTING} is not a database address") from None
    if parsed.get_backend_name() != "postgresql":
        raise CopyRefused(f"{TARGET_SETTING} must be a PostgreSQL address (postgresql://…)")
    return url


def safe_host(url: str) -> str:
    """The address as it may be printed: host and database, no user, no password."""
    parsed = make_url(url)
    return f"{parsed.host or '?'}/{parsed.database or '?'}"


def target_engine(url: str) -> Engine:
    """An engine for the new database, made like the app's (app.db.base)."""
    from app.db.base import engine_options, pin_to_utc

    engine = create_engine(url, future=True, hide_parameters=True, **engine_options(url))
    event.listen(engine, "connect", pin_to_utc)
    return engine


def _models():
    from app.db import models  # noqa: F401  (every table on Base.metadata)
    from app.db.base import Base

    return Base.metadata


def _app_tables_in(connection: Connection) -> list[str]:
    from app.db.migrations import VERSION_TABLE

    metadata = _models()
    present = set(inspect(connection).get_table_names())
    return sorted(present & (set(metadata.tables) | {VERSION_TABLE}))


def check(source: Engine, url: str) -> dict:
    """What a copy would do, writing nothing: the rows per table on this side,
    and whether the new database answers and is empty. Raises
    :class:`CopyRefused` when it is not."""
    metadata = _models()
    with source.connect() as connection:
        rows = {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one() for table in metadata.sorted_tables}
    target = target_engine(url)
    try:
        with target.connect() as connection:
            version = connection.execute(text("SHOW server_version")).scalar_one()
            holding = _holding_rows(connection)
    except CopyRefused:
        raise
    except Exception as exc:  # noqa: BLE001 — said in words, without the address
        raise CopyRefused(_why_not_connected(exc)) from None
    finally:
        target.dispose()
    if holding:
        raise CopyRefused(_not_empty(holding))
    return {"rows": rows, "server_version": version}


def _holding_rows(connection: Connection) -> list[str]:
    """The app's tables in the new database that already hold rows. Empty
    tables are no obstacle — an app started against the database once builds
    them — and nor is Alembic's own record of the revision."""
    from app.db.migrations import VERSION_TABLE

    holding = []
    for name in _app_tables_in(connection):
        if name == VERSION_TABLE:
            continue
        if connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one():
            holding.append(name)
    if holding == ["users"] and _only_the_startup_admin(connection):
        return []
    return holding


#: The one row an app started against an empty database writes by itself:
#: the configured admin's sign-in (app.core.users), never used and its
#: password never changed. Seen on 11 Nov 2026, when the owner's settings
#: file pointed the app at Neon before the copy.
_STARTUP_ADMIN = "role = 'admin' AND last_login_at IS NULL AND password_changed_at IS NULL"


def _only_the_startup_admin(connection: Connection) -> bool:
    total = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
    untouched = connection.execute(text(f"SELECT count(*) FROM users WHERE {_STARTUP_ADMIN}")).scalar_one()
    return total == 1 and untouched == 1


def _not_empty(holding: list[str]) -> str:
    shown = ", ".join(holding[:6]) + (" …" if len(holding) > 6 else "")
    return (
        f"the new database already holds rows ({shown}). The copy only goes into an empty database, "
        "so nothing there is overwritten: use a new, empty database (in Neon, a new branch or database), "
        "or empty this one first."
    )


def _why_not_connected(exc: BaseException) -> str:
    """A connection failure in words. Never the driver's own text in full: it
    can quote the address it was given."""
    said = str(exc).lower()
    if "password authentication failed" in said:
        hint = "the user name or password in the address was refused — copy the connection string again from Neon"
    elif "timeout" in said or "timed out" in said:
        hint = "nothing answered in time — check the internet connection, and that the address is right"
    elif "could not translate host name" in said or "nodename nor servname" in said or "name or service not known" in said:
        hint = "the host name in the address does not exist — copy the connection string again"
    elif "ssl" in said:
        hint = "the secure connection failed — keep sslmode=require at the end of the address"
    elif "does not exist" in said:
        hint = "the database named in the address does not exist on that server"
    else:
        hint = "the new database could not be reached"
    return f"{hint} ({type(exc).__name__})"


def _string_widths(table) -> dict[str, int]:
    """The table's columns whose width PostgreSQL enforces and SQLite never did."""
    widths = {}
    for column in table.columns:
        length = getattr(column.type, "length", None)
        # An Enum is a String to SQLAlchemy, its width that of the longest
        # member NAME; the value read back is the member itself, whose text
        # can be longer ("over_target" holds "failed_over_target"). Cutting it
        # made a status PostgreSQL refused (found by the first trial copy).
        if isinstance(column.type, String) and not isinstance(column.type, SAEnum) and length:
            widths[column.name] = int(length)
    return widths


def _fit(row: dict, table_name: str, widths: dict[str, int], cut: dict[str, int]) -> dict:
    """The row with any text longer than its column cut to fit, and counted: a
    value PostgreSQL would refuse whole. SQLite kept whatever it was given."""
    for column, width in widths.items():
        value = row.get(column)
        if type(value) is str and len(value) > width:
            row[column] = value[:width]
            key = f"{table_name}.{column}"
            cut[key] = cut.get(key, 0) + 1
    return row


def copy(source: Engine, url: str, *, say: Callable[[str], None] = print) -> CopyReport:
    """Copy every row from ``source`` (the app's SQLite database) into the
    empty PostgreSQL database at ``url``. See the module's docstring."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app.db.migrations import alembic_config

    if source.dialect.name != "sqlite":
        raise CopyRefused(
            "the app's own database is not the laptop's SQLite file — "
            "this copy is for moving off SQLite (ORDERING_DATABASE_URL still has to point at it)"
        )
    metadata = _models()
    report = CopyReport(host=safe_host(url))
    target = target_engine(url)
    try:
        try:
            outer = target.connect()
        except Exception as exc:  # noqa: BLE001
            raise CopyRefused(_why_not_connected(exc)) from None
        with outer as connection, source.connect() as reading:
            transaction = connection.begin()
            try:
                holding = _holding_rows(connection)
                if holding:
                    raise CopyRefused(_not_empty(holding))
                if "users" in _app_tables_in(connection) and _only_the_startup_admin(connection):
                    # The app's own first-start row; the copy brings the real one.
                    connection.execute(text(f"DELETE FROM users WHERE {_STARTUP_ADMIN}"))
                    report.startup_admin_replaced = True
                    say("the new database held only the admin sign-in the app makes at its first start; replaced by yours")
                say(f"building the tables on {report.host} …")
                metadata.create_all(connection)
                for table in metadata.sorted_tables:
                    widths = _string_widths(table)
                    order = list(table.primary_key.columns) or list(table.columns)[:1]
                    copied = 0
                    result = reading.execution_options(yield_per=BATCH).execute(select(table).order_by(*order))
                    for chunk in result.mappings().partitions(BATCH):
                        rows = [_fit(dict(row), table.name, widths, report.cut) for row in chunk]
                        connection.execute(table.insert(), rows)
                        copied += len(rows)
                    report.tables[table.name] = copied
                    if copied:
                        say(f"  {table.name}: {copied}")
                    _move_counter(connection, table)
                # The rows on both sides, table by table, before anything is kept.
                for table in metadata.sorted_tables:
                    here = reading.execute(select(func.count()).select_from(table)).scalar_one()
                    there = connection.execute(select(func.count()).select_from(table)).scalar_one()
                    if here != there:
                        raise CopyRefused(
                            f"{table.name}: {here} rows here but {there} there after copying — nothing was kept"
                        )
                script = ScriptDirectory.from_config(alembic_config())
                context = MigrationContext.configure(connection)
                context.stamp(script, "head")
                report.revision = context.get_current_revision()
                report.timezone_set, report.timezone_note = _database_in_utc(connection)
                transaction.commit()
            except BaseException:
                transaction.rollback()
                raise
    finally:
        target.dispose()
    return report


def _move_counter(connection: Connection, table) -> None:
    """Set the table's id counter past the highest id copied. Copied rows keep
    their ids, so the counter would otherwise hand out 1 next and collide."""
    keys = list(table.primary_key.columns)
    if len(keys) != 1 or not keys[0].autoincrement:
        return
    try:
        if keys[0].type.python_type is not int:
            return
    except NotImplementedError:
        return
    name, column = table.name, keys[0].name
    connection.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('\"{name}\"', '{column}'), "
            f"COALESCE((SELECT max(\"{column}\") FROM \"{name}\"), 1), "
            f"(SELECT max(\"{column}\") FROM \"{name}\") IS NOT NULL) "
            f"WHERE pg_get_serial_sequence('\"{name}\"', '{column}') IS NOT NULL"
        )
    )


def _database_in_utc(connection: Connection) -> tuple[bool, str | None]:
    """``ALTER DATABASE … SET timezone TO 'UTC'`` on the new database, in a
    savepoint: a user that does not own the database may not, and the copy
    stands without it (the app pins every direct connection itself)."""
    name = connection.execute(text("SELECT current_database()")).scalar_one()
    quoted = connection.execute(text("SELECT quote_ident(:name)"), {"name": name}).scalar_one()
    savepoint = connection.begin_nested()
    try:
        connection.execute(text(f"ALTER DATABASE {quoted} SET timezone TO 'UTC'"))
        savepoint.commit()
        return True, None
    except Exception as exc:  # noqa: BLE001
        savepoint.rollback()
        return False, f"the database's own time zone could not be set to UTC ({type(exc).__name__}); use the direct address"


def utc_for_a_new_connection(url: str) -> str:
    """The time zone a brand-new connection to the database gets on its own —
    without the app's pin — which is what a pooled connection sees."""
    engine = create_engine(url, future=True, hide_parameters=True, connect_args={"connect_timeout": 15})
    try:
        with engine.connect() as connection:
            return str(connection.execute(text("SHOW TimeZone")).scalar_one())
    finally:
        engine.dispose()
