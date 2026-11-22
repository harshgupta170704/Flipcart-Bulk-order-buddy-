"""Alembic migrations (audit F-33): the revisions build exactly the schema the
models declare, on SQLite and, when the suite is pointed at one, PostgreSQL.

This is the guard that makes every model change come with a migration: change
a model without a revision and the comparisons here fail. Each test works on
a temporary database of its own, handed to Alembic as a connection
(app.db.migrations.alembic_config) or, for the command line's own engine,
named in the app's settings for the test — never on the owner's; the
PostgreSQL one uses the scratch database ORDERING_TEST_DATABASE_URL names,
and only that.

Alembic must be installed (requirements.txt). Without it the Alembic tests
are skipped; the ones about starting up without it still run.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, create_mock_engine, inspect, text
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import base as db_base
from app.db import migrations
from app.db import models  # noqa: F401  (every table on Base.metadata before ENUM_TYPES is read)
from app.db.base import Base
from tests.test_db_safety import judged_as_the_real_file

BACKEND = Path(__file__).resolve().parents[1]
COMPOSE = BACKEND / "docker-compose.yml"


def _alembic_installed() -> bool:
    # The migrations folder is itself called "alembic" and sits on sys.path
    # (pytest.ini's pythonpath), so with the real package missing a bare
    # "import alembic" finds that folder as an empty namespace package. Only
    # the real package has alembic.command.
    try:
        return importlib.util.find_spec("alembic.command") is not None
    except ImportError:
        return False


needs_alembic = pytest.mark.skipif(
    not _alembic_installed(), reason="alembic is not installed: pip install -r requirements.txt"
)

PG_URL = os.environ.get("ORDERING_TEST_DATABASE_URL", "")
needs_postgresql = pytest.mark.skipif(
    not PG_URL.startswith("postgresql"),
    reason="ORDERING_TEST_DATABASE_URL does not name a scratch PostgreSQL",
)

ENUM_TYPES = sorted(
    {col.type.name for table in Base.metadata.tables.values() for col in table.columns if isinstance(col.type, sa.Enum)}
)


# --- helpers ------------------------------------------------------------------


@pytest.fixture
def sqlite_engine(tmp_path):
    """An empty SQLite file of the test's own."""
    engine = create_engine(f"sqlite:///{tmp_path / 'migrated.db'}", poolclass=NullPool)
    yield engine
    engine.dispose()


def _alembic(engine, name: str, *args) -> None:
    """Run one Alembic command (upgrade, downgrade, stamp) on ``engine``."""
    from alembic import command

    with engine.begin() as connection:
        getattr(command, name)(migrations.alembic_config(connection), *args)


def _differences(engine) -> list:
    """What autogenerate would write for this database: [] when it matches the models."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts=dict(migrations.COMPARE_OPTIONS))
        return compare_metadata(context, Base.metadata)


def _revision(engine) -> str | None:
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _app_tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) & set(Base.metadata.tables)


def _sqlite_structure(engine) -> dict:
    """Each table's columns (name, declared type, NOT NULL, default, primary
    key), foreign keys and indexes (a partial one's WHERE included), as SQLite
    reports them. In no particular order: a column a later revision adds sits
    at the end of its table, where create_all puts it in the model's order."""
    structure = {}
    with engine.connect() as conn:
        tables = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name != ?",
            (migrations.VERSION_TABLE,),
        ).scalars().all()
        for table in tables:
            columns = sorted(tuple(row[1:]) for row in conn.exec_driver_sql(f"PRAGMA table_info('{table}')"))
            foreign_keys = sorted(tuple(row[2:]) for row in conn.exec_driver_sql(f"PRAGMA foreign_key_list('{table}')"))
            indexes = []
            for _seq, name, unique, origin, partial in conn.exec_driver_sql(f"PRAGMA index_list('{table}')"):
                covered = tuple(row[2] for row in conn.exec_driver_sql(f"PRAGMA index_info('{name}')"))
                sql = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).scalar()
                # A UNIQUE constraint's own index is numbered by its place in
                # the table, so it is known by what it covers, not its name.
                label = "" if name.startswith("sqlite_autoindex_") else name
                indexes.append((label, unique, origin, partial, covered, sql or ""))
            structure[table] = {"columns": columns, "foreign_keys": foreign_keys, "indexes": sorted(indexes)}
    return structure


def _postgresql_structure(engine) -> dict:
    """The same from PostgreSQL's catalogues: columns, every constraint's and
    index's definition, and the enum types with their members in order."""
    version = {"version": migrations.VERSION_TABLE}
    with engine.connect() as conn:
        columns = conn.execute(
            text(
                "SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default, "
                "character_maximum_length, numeric_precision FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name <> :version"
            ),
            version,
        ).all()
        constraints = conn.execute(
            text(
                "SELECT rel.relname, con.conname, con.contype, pg_get_constraintdef(con.oid) "
                "FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
                "WHERE ns.nspname = current_schema() AND rel.relname <> :version"
            ),
            version,
        ).all()
        indexes = conn.execute(
            text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = current_schema() AND tablename <> :version"
            ),
            version,
        ).all()
        enums = conn.execute(
            text(
                "SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_type t "
                "JOIN pg_enum e ON e.enumtypid = t.oid JOIN pg_namespace ns ON ns.oid = t.typnamespace "
                "WHERE ns.nspname = current_schema() GROUP BY t.typname"
            )
        ).all()
    return {
        "columns": sorted(tuple(row) for row in columns),
        "constraints": sorted(tuple(row) for row in constraints),
        "indexes": sorted(tuple(row) for row in indexes),
        "enums": sorted((name, tuple(labels)) for name, labels in enums),
    }


def _postgresql_enum_types(engine) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text(
                    "SELECT t.typname FROM pg_type t JOIN pg_namespace ns ON ns.oid = t.typnamespace "
                    "WHERE t.typtype = 'e' AND ns.nspname = current_schema()"
                )
            ).scalars()
        )


def _run_alembic_command(cwd: Path, database_url: str, *args: str) -> subprocess.CompletedProcess:
    """The alembic command line, as compose's migrate service runs it, with the
    address in ORDERING_DATABASE_URL. Run from another folder, so neither the
    working directory nor a .env beside the app plays a part."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(migrations.ALEMBIC_INI), *args],
        cwd=cwd,
        env={**os.environ, "ORDERING_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
    )


# --- the revisions against the models -------------------------------------------


@needs_alembic
def test_upgrade_head_on_an_empty_database_builds_exactly_the_models(sqlite_engine):
    """(a) The guard: a model changed without a revision shows up here."""
    _alembic(sqlite_engine, "upgrade", "head")

    assert _revision(sqlite_engine) == migrations.head_revision()
    differences = _differences(sqlite_engine)
    assert differences == [], (
        f"the models and alembic/versions disagree: {differences}. Write a revision "
        "(docs/DEPLOY.md section 5, 'Every model change gets a revision')."
    )


@needs_alembic
def test_a_migrated_database_is_the_one_create_all_builds(sqlite_engine, tmp_path):
    """init_db stamps a database create_all has just built as the newest
    revision; that is true only while the two build the same thing. This
    also checks what the comparison above does not: a partial index's WHERE,
    column defaults."""
    built = create_engine(f"sqlite:///{tmp_path / 'built.db'}", poolclass=NullPool)
    try:
        Base.metadata.create_all(built)
        _alembic(sqlite_engine, "upgrade", "head")

        migrated = _sqlite_structure(sqlite_engine)
        assert set(migrated) == set(Base.metadata.tables)
        assert migrated == _sqlite_structure(built)
        open_kind = [ix for ix in migrated["unit_requests"]["indexes"] if ix[0] == "uq_unit_requests_open_kind"]
        assert open_kind and "WHERE status = 'open'" in open_kind[0][-1]
    finally:
        built.dispose()


@needs_alembic
def test_downgrade_to_base_and_upgrade_again(sqlite_engine):
    """(b)"""
    _alembic(sqlite_engine, "upgrade", "head")
    _alembic(sqlite_engine, "downgrade", "base")

    assert _app_tables(sqlite_engine) == set()
    assert _revision(sqlite_engine) is None

    _alembic(sqlite_engine, "upgrade", "head")
    assert _revision(sqlite_engine) == migrations.head_revision()
    assert _differences(sqlite_engine) == []


@needs_alembic
def test_the_revisions_form_one_line_from_the_baseline():
    """Two revisions written side by side (the packages merged after a wave)
    both claim the same parent and give two heads; "upgrade head" then stops.
    Merge them with "alembic merge heads"."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(migrations.alembic_config())
    assert len(script.get_heads()) == 1, script.get_heads()
    line = [rev.revision for rev in script.walk_revisions("base", "heads")][::-1]
    assert line[:2] == ["0001", "0002"]
    # alembic_version.version_num is VARCHAR(32).
    assert all(len(rev) <= 32 for rev in line)


@needs_alembic
def test_the_baseline_refuses_a_database_that_already_has_the_tables(sqlite_engine):
    """What init_db or an older build left is stamped, never run through the
    baseline, and the refusal says so before anything is created."""
    from alembic.util import CommandError

    Base.metadata.create_all(sqlite_engine)
    with pytest.raises(CommandError, match="alembic stamp 0001"):
        _alembic(sqlite_engine, "upgrade", "head")
    assert _revision(sqlite_engine) is None

    # The documented way in (DEPLOY.md section 5): stamp, upgrade, check.
    _alembic(sqlite_engine, "stamp", "0001")
    _alembic(sqlite_engine, "upgrade", "head")
    assert _revision(sqlite_engine) == migrations.head_revision()
    assert _differences(sqlite_engine) == []


@needs_alembic
def test_revision_0002_leaves_sqlite_alone(sqlite_engine):
    """SQLite holds 8-byte integers in any INTEGER column, so 0002 rebuilds nothing there."""

    def ddl() -> list:
        with sqlite_engine.connect() as conn:
            return conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE name != ? ORDER BY name", (migrations.VERSION_TABLE,)
            ).all()

    _alembic(sqlite_engine, "upgrade", "0001")
    before = ddl()
    _alembic(sqlite_engine, "upgrade", "0002")
    assert ddl() == before


@needs_alembic
def test_the_command_line_migrates_the_database_the_settings_name(tmp_path):
    """What compose's migrate service runs: the address comes from
    ORDERING_DATABASE_URL, alembic.ini names none."""
    url = f"sqlite:///{tmp_path / 'cli.db'}"

    upgraded = _run_alembic_command(tmp_path, url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr
    checked = _run_alembic_command(tmp_path, url, "check")
    assert checked.returncode == 0, checked.stderr
    assert "No new upgrade operations detected" in checked.stdout
    current = _run_alembic_command(tmp_path, url, "current")
    assert f"{migrations.head_revision()} (head)" in current.stdout


def _command_line(name: str, *args) -> None:
    """``alembic <name>`` as an operator runs it from backend/, in this
    process: no connection handed in, so env.py makes its own engine from the
    app's settings (which the test points at its file)."""
    from alembic import command

    getattr(command, name)(migrations.alembic_config(), *args)


def _copies_left(engine) -> list[str]:
    """The rebuild copies (``_alembic_tmp_<table>``) in the file."""
    return [name for name in inspect(engine).get_table_names() if name.startswith(db_base.ALEMBIC_COPY_PREFIX)]


@needs_alembic
def test_a_step_refused_part_way_leaves_the_sqlite_file_as_it_was(tmp_path):
    """The command line runs each step in one real transaction on SQLite
    (alembic/env.py). The sqlite3 driver alone saves a rebuild's CREATE TABLE
    _alembic_tmp_<table> the moment it runs, so a step that failed after it
    rolled back everything but that copy, and the retry the refusal asks for
    stopped on "table _alembic_tmp_order_units already exists".

    The failure part-way here is the drop guard refusing the swap, as it did
    before it knew Alembic's copy-and-swap, on a file it judges as the
    laptop's: 0006's downgrade drops four columns, a rebuild of order_units on
    SQLite. A "database is locked" part-way would end the same."""
    url = f"sqlite:///{tmp_path / 'ordering.db'}"
    engine = create_engine(url, poolclass=NullPool)
    new_columns = {"promised_date", "delivered_date", "payment_method", "amount_paid"}

    def unit_columns() -> set[str]:
        return {column["name"] for column in inspect(engine).get_columns("order_units")}

    try:
        with judged_as_the_real_file(url) as patch:
            patch.setattr(get_settings(), "database_url", url)
            _command_line("upgrade", "0006")
            patch.setattr(db_base, "_alembic_swaps_a_copy", lambda *args: False)

            with pytest.raises(RuntimeError, match="refusing to drop tables"):
                _command_line("downgrade", "0005")
            assert _copies_left(engine) == []
            assert _revision(engine) == "0006"
            assert new_columns <= unit_columns()

            patch.setenv(db_base.ALLOW_DROP_ENV, "1")  # the retry the refusal asks for
            _command_line("downgrade", "0005")

        assert _revision(engine) == "0005"
        assert not new_columns & unit_columns()
        assert _copies_left(engine) == []
    finally:
        engine.dispose()


@needs_alembic
def test_the_command_line_goes_down_and_up_again_on_a_file_that_is_not_a_throwaway(tmp_path):
    """base → head → base → head through env.py's own engine (one transaction
    a step), on a file the drop guard judges as the laptop's. The upgrades
    need no ORDERING_ALLOW_DROP, nor do the downgrades of 0006 and 0005: on
    SQLite they rebuild tables, and the guard lets Alembic swap a table for
    its full copy. Dropping whole tables, down to the base, needs it."""
    url = f"sqlite:///{tmp_path / 'ordering.db'}"
    engine = create_engine(url, poolclass=NullPool)
    try:
        with judged_as_the_real_file(url) as patch:
            patch.setattr(get_settings(), "database_url", url)
            _command_line("upgrade", "head")
            patch.setenv(db_base.ALLOW_DROP_ENV, "1")
            _command_line("downgrade", "base")
            patch.delenv(db_base.ALLOW_DROP_ENV)
            assert _app_tables(engine) == set()

            _command_line("upgrade", "0006")
            _command_line("downgrade", "0004")  # rebuilds only, no override
            _command_line("upgrade", "head")
            _command_line("check")  # raises when the database and the models differ

        assert _revision(engine) == migrations.head_revision()
        assert _copies_left(engine) == []
    finally:
        engine.dispose()


@needs_alembic
def test_the_drop_guard_lets_no_explicit_drop_past_a_partial_copy(tmp_path):
    """The guard's one exception is Alembic swapping a table for the copy it
    has just filled with every row. A revision's own drop of a table beside a
    copy that holds fewer rows — one an earlier failed rebuild left behind —
    is no swap, and stays refused."""
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    engine = create_engine(f"sqlite:///{tmp_path / 'ordering.db'}", poolclass=NullPool)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tags (id, name, created_at) VALUES (1, 'kept', CURRENT_TIMESTAMP)"))
            conn.execute(text(f"CREATE TABLE {db_base.ALEMBIC_COPY_PREFIX}tags AS SELECT * FROM tags WHERE 0"))

        with judged_as_the_real_file(engine.url):
            with pytest.raises(RuntimeError, match="refusing to drop tables"):
                with engine.begin() as conn:
                    Operations(MigrationContext.configure(conn)).drop_table("tags")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT name FROM tags")).scalars().all() == ["kept"]
    finally:
        engine.dispose()


@needs_alembic
def test_the_postgresql_sql_has_the_enum_types_the_bigint_and_the_partial_index(tmp_path):
    """``--sql`` writes the PostgreSQL statements out without connecting (the
    address points nowhere), so their shape is checked even where no
    PostgreSQL runs; and a --sql downgrade gets past the drop guard, since
    nothing is dropped."""
    url = "postgresql+psycopg://reviewer@127.0.0.1:9/never_contacted"

    upgrade = _run_alembic_command(tmp_path, url, "upgrade", "head", "--sql")
    assert upgrade.returncode == 0, upgrade.stderr
    sql = upgrade.stdout
    for name in ENUM_TYPES:
        assert f"CREATE TYPE {name} AS ENUM" in sql, name
    assert "size BIGINT NOT NULL" in sql
    assert "CREATE UNIQUE INDEX uq_unit_requests_open_kind ON unit_requests (unit_pk, kind) WHERE status = 'open'" in sql
    # 0002 cannot look at the column offline, so it writes its ALTER out.
    assert "ALTER TABLE compiled_archives ALTER COLUMN size TYPE BIGINT" in sql

    # From head: a table a later revision added is dropped by that revision.
    downgrade = _run_alembic_command(tmp_path, url, "downgrade", "head:base", "--sql")
    assert downgrade.returncode == 0, downgrade.stderr
    # Every model table is dropped once. Whether alembic also writes out a DROP
    # of its own alembic_version table offline depends on its version (1.20
    # deletes the row instead), so that one is not counted.
    dropped = downgrade.stdout.count("DROP TABLE") - downgrade.stdout.count("DROP TABLE alembic_version")
    assert dropped == len(Base.metadata.tables)
    for name in ENUM_TYPES:
        assert f"DROP TYPE IF EXISTS {name}" in downgrade.stdout, name


@needs_alembic
@needs_postgresql
def test_the_same_on_postgresql(monkeypatch):
    """(c) On the scratch PostgreSQL the suite was pointed at: the migrated
    schema is the one create_all builds and the models declare, down and up
    again works (the enum types go with the tables), and 0002 widens an
    INTEGER size column an older build left, keeping its rows.

    That database is the suite's own: the fixtures built it with create_all
    (which this compares against), and it is emptied first and left at head.
    """
    monkeypatch.setenv(db_base.ALLOW_DROP_ENV, "1")  # the suite's named scratch database
    engine = create_engine(PG_URL, poolclass=NullPool)
    try:
        built = _postgresql_structure(engine)
        assert [name for name, _members in built["enums"]] == ENUM_TYPES
        assert {row[0] for row in built["indexes"]} == set(Base.metadata.tables)
        Base.metadata.drop_all(engine)
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {migrations.VERSION_TABLE}"))
            for name in ENUM_TYPES:
                conn.execute(text(f"DROP TYPE IF EXISTS {name}"))

        _alembic(engine, "upgrade", "head")
        assert _differences(engine) == []
        assert _postgresql_structure(engine) == built

        _alembic(engine, "downgrade", "base")
        assert _app_tables(engine) == set()
        assert _postgresql_enum_types(engine) & set(ENUM_TYPES) == set()
        _alembic(engine, "upgrade", "head")
        assert _differences(engine) == []

        # A compiled_archives table as a build before 24 Oct 2026 made it.
        _alembic(engine, "downgrade", "0001")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE compiled_archives ALTER COLUMN size TYPE INTEGER"))
            conn.execute(
                text(
                    "INSERT INTO compiled_archives (name, prefix, path, size, orders, units, invoices, "
                    "left_out, missing_files, created_at) VALUES ('q3.zip', 'q3', 'q3.zip', 7, 0, 0, 0, 0, 0, now())"
                )
            )
        assert _differences(engine) != []  # the old type is a difference from the models
        _alembic(engine, "upgrade", "head")
        with engine.begin() as conn:
            kind = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'compiled_archives' AND column_name = 'size'"
                )
            ).scalar()
            assert kind == "bigint"
            assert conn.execute(text("SELECT size FROM compiled_archives WHERE name = 'q3.zip'")).scalar() == 7
            conn.execute(text("UPDATE compiled_archives SET size = 3000000000 WHERE name = 'q3.zip'"))  # past 2 GiB
        assert _differences(engine) == []
        # Run again on a column that is already right, it changes nothing.
        _alembic(engine, "downgrade", "0001")
        _alembic(engine, "upgrade", "head")
        assert _differences(engine) == []
    finally:
        engine.dispose()


# --- init_db and Alembic ----------------------------------------------------------


@needs_alembic
def test_init_db_stamps_a_database_it_built_from_nothing_and_no_other():
    """On the suite's own database: emptied, init_db builds it and stamps it
    at head; with tables already there (the laptop's case), it never stamps."""
    engine = db_base.engine

    def drop_version_table() -> None:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {migrations.VERSION_TABLE}"))

    drop_version_table()
    Base.metadata.drop_all(engine)
    db_base.init_db()
    assert _revision(engine) == migrations.head_revision()

    drop_version_table()
    db_base.init_db()
    assert migrations.VERSION_TABLE not in inspect(engine).get_table_names()


def test_init_db_starts_without_alembic_and_only_goes_without_the_stamp(monkeypatch, caplog):
    """A laptop whose environment predates Alembic must still start."""
    for name in [module for module in sys.modules if module == "alembic" or module.startswith("alembic.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "alembic", None)  # import alembic now fails
    engine = db_base.engine
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {migrations.VERSION_TABLE}"))
    Base.metadata.drop_all(engine)

    with caplog.at_level(logging.INFO, logger="ordering.db"):
        db_base.init_db()

    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    assert migrations.VERSION_TABLE not in tables
    assert any("alembic is not installed" in record.getMessage() for record in caplog.records)


def test_a_database_that_had_tables_is_never_stamped(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'existing.db'}", poolclass=NullPool)
    try:
        assert migrations.stamp_new_database(engine, {"accounts", "orders"}) is None
        assert migrations.stamp_new_database(engine, {migrations.VERSION_TABLE}) is None
        assert migrations.VERSION_TABLE not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_the_drop_guard_lets_a_sql_writing_stand_in_through():
    """``alembic ... --sql`` writes statements through a stand-in connection
    with no database (and no address) behind it; the guard used to fail on
    it. A real connection is judged as before (tests/test_db_safety.py)."""
    written: list[str] = []
    stand_in = create_mock_engine(
        "postgresql+psycopg://", lambda statement, *a, **kw: written.append(str(statement.compile(dialect=stand_in.dialect)))
    )
    # Tested without the override; put back by hand before the fixtures drop
    # their own database (a scratch PostgreSQL needs it), as test_db_safety does.
    allowed = os.environ.pop(db_base.ALLOW_DROP_ENV, None)
    try:
        Base.metadata.tables["tags"].drop(stand_in)
    finally:
        if allowed is not None:
            os.environ[db_base.ALLOW_DROP_ENV] = allowed
    assert any("DROP TABLE tags" in statement for statement in written)


# --- shipping it ----------------------------------------------------------------------


def test_alembic_is_a_requirement():
    lines = [line.split("#", 1)[0].strip() for line in (BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert any(line.startswith("alembic") for line in lines)


def test_the_image_carries_the_migrations():
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY alembic ./alembic" in dockerfile
    ignored = {line.strip().strip("/") for line in (BACKEND / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    assert not ignored & {"alembic", "alembic.ini", "alembic/versions"}


def test_compose_migrates_before_the_api_and_the_workers_start():
    yaml = pytest.importorskip("yaml")
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]

    migrate = services["migrate"]
    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate.get("restart") == "no"  # one-shot: it runs, and exits
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"
    assert migrate["environment"]["ORDERING_DATABASE_URL"] == services["api"]["environment"]["ORDERING_DATABASE_URL"]
    for name in ("api", "worker"):
        assert services[name]["depends_on"]["migrate"]["condition"] == "service_completed_successfully", name
