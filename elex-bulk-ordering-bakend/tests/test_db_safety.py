"""The two guards around the real database.

Added after 25 Oct 2026, when the development database — accounts, a signed-in
Flipkart session, the Address Book, the order history — was emptied by a table
drop that reached it from beside the app, with no copy to go back to. Now a
drop is refused at the engine unless the database is a throwaway one, and a
copy of a SQLite file is taken before start-up touches its schema.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db import base as db_base
from app.db.models import Address


def test_only_a_throwaway_database_may_be_dropped():
    temp = os.path.join(tempfile.gettempdir(), "ordering-test-x", "test.db")
    assert db_base.is_throwaway(make_url("sqlite://"))  # in memory
    assert db_base.is_throwaway(make_url(f"sqlite:///{temp}"))
    assert db_base.is_throwaway(make_url("sqlite:////tmp/verify-ledger/t.db"))
    assert not db_base.is_throwaway(make_url("sqlite:////srv/ordering/data/ordering.db"))
    assert not db_base.is_throwaway(make_url("sqlite:///./ordering.db"))  # relative to the app's folder
    assert not db_base.is_throwaway(make_url("postgresql+psycopg://u:p@db:5432/ordering"))


def test_a_drop_against_a_real_database_is_refused_at_the_engine(monkeypatch, tmp_path):
    """A scratch file stands in for the real one: the throwaway check is told
    it is not. The refusal sits on the metadata, so every path to ``drop_all``
    — a fixture, a script, a one-liner — meets it."""
    scratch = create_engine(f"sqlite:///{tmp_path / 'real.db'}")
    _pretend_real(monkeypatch, scratch)
    db_base.Base.metadata.create_all(scratch)
    with Session(scratch) as session:
        session.add(Address(label="hq", name="HQ", line1="1 Main Road", city="Lucknow", state="UP", pincode="226001"))
        session.commit()

    # The suite itself may run with the override on (a scratch PostgreSQL named
    # by ORDERING_TEST_DATABASE_URL); the refusal is tested without it, and the
    # override is put back before the fixtures drop their own database.
    allowed = os.environ.pop(db_base.ALLOW_DROP_ENV, None)
    try:
        with pytest.raises(RuntimeError, match="refusing to drop tables"):
            db_base.Base.metadata.drop_all(scratch)
        with pytest.raises(RuntimeError, match="refusing to drop tables"):
            Address.__table__.drop(scratch)  # one table's own drop meets it too
        with scratch.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM addresses")).scalar() == 1  # untouched

        os.environ[db_base.ALLOW_DROP_ENV] = "1"  # on purpose, and said so
        db_base.Base.metadata.drop_all(scratch)
        with scratch.connect() as conn:
            assert not conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
    finally:
        if allowed is None:
            os.environ.pop(db_base.ALLOW_DROP_ENV, None)
        else:
            os.environ[db_base.ALLOW_DROP_ENV] = allowed


def _pretend_real(monkeypatch, scratch) -> None:
    """Make the throwaway check say "real" for one scratch engine only, so the
    suite's own temp database (dropped by the autouse fixture after the test,
    while the patch is still in force) is judged as before."""
    original = db_base.is_throwaway
    monkeypatch.setattr(db_base, "is_throwaway", lambda url: False if url == scratch.url else original(url))


@contextlib.contextmanager
def judged_as_the_real_file(url):
    """For the length of the block, the guard judges the database at ``url``
    as it judges the laptop's backend/ordering.db — not a throwaway — and
    ORDERING_ALLOW_DROP is out of the environment. Yields the patcher, for a
    retry that sets the override on purpose. Both are put back when the block
    ends, before the fixtures drop their own database (a scratch PostgreSQL
    needs the override for that)."""
    url = make_url(url)
    original = db_base.is_throwaway
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db_base, "is_throwaway", lambda candidate: False if candidate == url else original(candidate))
        patch.delenv(db_base.ALLOW_DROP_ENV, raising=False)
        yield patch


def test_a_copy_beside_a_table_does_not_let_a_drop_through(tmp_path):
    """The guard lets one drop through: Alembic swapping a rebuilt SQLite
    table for the full copy it has just made. A copy lying beside a table —
    left by an earlier failed rebuild, say; this one even holds every row —
    must not become a way past the guard: one table's ``drop()`` and
    ``drop_all`` are SQLAlchemy's, never Alembic's swap, and stay refused."""
    scratch = create_engine(f"sqlite:///{tmp_path / 'real.db'}")
    db_base.Base.metadata.create_all(scratch)
    _add_address(scratch, "hq")
    with scratch.begin() as conn:
        conn.execute(text(f"CREATE TABLE {db_base.ALEMBIC_COPY_PREFIX}addresses AS SELECT * FROM addresses"))

    with judged_as_the_real_file(scratch.url):
        with pytest.raises(RuntimeError, match="refusing to drop tables"):
            Address.__table__.drop(scratch)
        with pytest.raises(RuntimeError, match="refusing to drop tables"):
            db_base.Base.metadata.drop_all(scratch)

    with scratch.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM addresses")).scalar() == 1  # untouched


def _add_address(engine, label: str) -> None:
    with Session(engine) as session:
        session.add(Address(label=label, name="X", line1=f"{label} Road", city="Lucknow", state="UP", pincode="226001"))
        session.commit()


def test_a_copy_is_taken_before_start_up_and_empties_and_repeats_are_not(monkeypatch, tmp_path):
    path = tmp_path / "ordering.db"
    scratch = create_engine(f"sqlite:///{path}")
    db_base.Base.metadata.create_all(scratch)
    monkeypatch.setattr(db_base, "engine", scratch)
    _pretend_real(monkeypatch, scratch)
    folder = tmp_path / db_base.BACKUP_DIRNAME

    # A fresh (or emptied) file is not worth a copy: it would only push a good one out.
    assert db_base.backup_sqlite(now=datetime(2026, 10, 25, 1, 0, 0)) is None
    assert not folder.exists()

    _add_address(scratch, "one")
    first = db_base.backup_sqlite(now=datetime(2026, 10, 25, 1, 0, 0))
    assert first == str(folder / "ordering-20261025-010000.db")
    import contextlib

    with contextlib.closing(sqlite3.connect(first)) as copy:
        assert copy.execute("SELECT COUNT(*) FROM addresses").fetchone()[0] == 1
    # Unchanged since: a restart does not make another copy.
    assert db_base.backup_sqlite(now=datetime(2026, 10, 25, 1, 5, 0)) is None

    # Changes make copies; only the newest `keep` stay, with their checksums.
    # A copy cut short earlier (a .part left behind) is swept and never counted.
    (folder / "ordering-20261025-010500.db.part").write_bytes(b"half a file")
    _add_address(scratch, "two")
    db_base.backup_sqlite(keep=2, now=datetime(2026, 10, 25, 1, 10, 0))
    _add_address(scratch, "three")
    db_base.backup_sqlite(keep=2, now=datetime(2026, 10, 25, 1, 20, 0))
    kept = sorted(name for name in os.listdir(folder) if name.endswith(".db"))
    assert kept == ["ordering-20261025-011000.db", "ordering-20261025-012000.db"]
    assert all(os.path.exists(folder / (name + ".sha256")) for name in kept)
    assert not os.path.exists(folder / "ordering-20261025-010000.db.sha256")
    assert not any(name.endswith(".part") for name in os.listdir(folder))


def test_the_backup_ring_keeps_each_days_newest_copy_for_a_month(monkeypatch, tmp_path):
    """Ten restarts in an hour must not push last week's copy out (audit F-16,
    25 Oct 2026): beside the newest few, the newest copy of every day stays
    for thirty days."""
    path = tmp_path / "ordering.db"
    scratch = create_engine(f"sqlite:///{path}")
    db_base.Base.metadata.create_all(scratch)
    monkeypatch.setattr(db_base, "engine", scratch)
    _pretend_real(monkeypatch, scratch)
    folder = tmp_path / db_base.BACKUP_DIRNAME

    for day in (20, 21, 22):  # one copy a day
        _add_address(scratch, f"day{day}")
        assert db_base.backup_sqlite(keep=2, now=datetime(2026, 10, day, 9, 0, 0)) is not None
    for minute in range(5):  # a burst of restarts on the 25th
        _add_address(scratch, f"burst{minute}")
        db_base.backup_sqlite(keep=2, now=datetime(2026, 10, 25, 1, minute, 0))

    kept = sorted(name for name in os.listdir(folder) if name.endswith(".db"))
    assert kept == [
        "ordering-20261020-090000.db",
        "ordering-20261021-090000.db",
        "ordering-20261022-090000.db",
        "ordering-20261025-010300.db",
        "ordering-20261025-010400.db",
    ]
    assert all(os.path.exists(folder / (name + ".sha256")) for name in kept)

    # A day older than the tier is let go once the month has passed.
    _add_address(scratch, "late")
    db_base.backup_sqlite(keep=2, now=datetime(2026, 11, 30, 9, 0, 0))
    kept = sorted(name for name in os.listdir(folder) if name.endswith(".db"))
    assert "ordering-20261020-090000.db" not in kept
    assert kept[-1] == "ordering-20261130-090000.db"


def test_a_copy_cut_short_never_wears_a_trusted_name(monkeypatch, tmp_path):
    """The copy is written as .part and renamed only after its checksum is
    on disk; a failure half-way leaves a .part, not a half file that sorts as
    the newest good copy."""
    path = tmp_path / "ordering.db"
    scratch = create_engine(f"sqlite:///{path}")
    db_base.Base.metadata.create_all(scratch)
    _add_address(scratch, "one")
    monkeypatch.setattr(db_base, "engine", scratch)
    _pretend_real(monkeypatch, scratch)

    real_connect = db_base.sqlite3.connect

    class Boom(Exception):
        pass

    class FailsToCopy(sqlite3.Connection):
        """A connection whose backup dies half-way (disk full, a kill)."""

        def backup(self, *_args, **_kwargs):
            raise Boom("disk full")

    def failing_connect(target, *args, **kwargs):
        return real_connect(target, *args, factory=FailsToCopy, **kwargs)

    monkeypatch.setattr(db_base.sqlite3, "connect", failing_connect)
    with pytest.raises(Boom):
        db_base.backup_sqlite(now=datetime(2026, 10, 25, 1, 0, 0))
    folder = tmp_path / db_base.BACKUP_DIRNAME
    assert [name for name in os.listdir(folder) if name.endswith(".db")] == []
    monkeypatch.setattr(db_base.sqlite3, "connect", real_connect)
    assert db_base.backup_sqlite(now=datetime(2026, 10, 25, 1, 1, 0)) == str(folder / "ordering-20261025-010100.db")
    assert not any(name.endswith(".part") for name in os.listdir(folder))


def test_start_up_takes_the_copy_first(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(db_base, "backup_sqlite", lambda: calls.append("backup"))
    db_base.init_db()
    assert calls == ["backup"]


def test_a_failed_copy_does_not_stop_the_app(monkeypatch, caplog):
    def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(db_base, "backup_sqlite", boom)
    db_base.init_db()  # still comes up
    assert any("could not back up" in record.getMessage() for record in caplog.records)


def test_the_copy_is_closed_before_it_is_renamed(monkeypatch, tmp_path):
    """Windows will not rename an open file. sqlite3's own "with" leaves the
    connection open, so on the owner's laptop every copy stayed a ".part" and
    was swept at the next start: 51 checksums and no backup (18 Nov 2026).
    Here the rename refuses, as Windows does, while a connection to it is open."""
    import sqlite3 as real_sqlite3

    path = tmp_path / "ordering.db"
    scratch = create_engine(f"sqlite:///{path}")
    db_base.Base.metadata.create_all(scratch)
    monkeypatch.setattr(db_base, "engine", scratch)
    _pretend_real(monkeypatch, scratch)
    _add_address(scratch, "one")

    open_files: set[str] = set()

    class Tracked(real_sqlite3.Connection):
        def __init__(self, database, *args, **kwargs):
            super().__init__(database, *args, **kwargs)
            self._name = str(database)
            open_files.add(self._name)

        def close(self):
            open_files.discard(self._name)
            super().close()

    connect = real_sqlite3.connect
    monkeypatch.setattr(db_base.sqlite3, "connect", lambda *a, **k: connect(*a, factory=Tracked, **k))
    replace = os.replace

    def windows_replace(src, dst):
        if str(src) in open_files:
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return replace(src, dst)

    monkeypatch.setattr(db_base.os, "replace", windows_replace)

    target = db_base.backup_sqlite(now=datetime(2026, 11, 19, 9, 0, 0))
    assert target is not None and os.path.exists(target)
    folder = tmp_path / db_base.BACKUP_DIRNAME
    assert not any(name.endswith(".part") for name in os.listdir(folder))


def test_checksums_left_by_copies_that_never_finished_are_swept(monkeypatch, tmp_path):
    path = tmp_path / "ordering.db"
    scratch = create_engine(f"sqlite:///{path}")
    db_base.Base.metadata.create_all(scratch)
    monkeypatch.setattr(db_base, "engine", scratch)
    _pretend_real(monkeypatch, scratch)
    _add_address(scratch, "one")
    folder = tmp_path / db_base.BACKUP_DIRNAME
    folder.mkdir()
    (folder / "ordering-20261112-135809.db.sha256").write_text("x\n")

    db_base.backup_sqlite(now=datetime(2026, 11, 19, 9, 0, 0))
    names = os.listdir(folder)
    assert "ordering-20261112-135809.db.sha256" not in names
    assert "ordering-20261119-090000.db" in names
