"""SQLAlchemy engine, session factory and declarative base.

Phase 1 uses a synchronous engine (SQLite by default) which FastAPI runs in a
threadpool. The ``database_url`` is the only thing that changes to move to
PostgreSQL in production.
"""

import contextlib
import hashlib
import logging
import os
import sqlite3
import tempfile
from collections.abc import Generator
from datetime import datetime

from sqlalchemy import Table, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

log = logging.getLogger("ordering.db")

_settings = get_settings()

# SQLite needs check_same_thread=False so the connection can be shared across
# FastAPI's threadpool workers; other backends ignore this.
#
# SQLITE_LOCK_WAIT: how long a statement waits for SQLite's lock before giving
# up with "database is locked". Python's default is 5 s, and one streamed
# export of a year's ledger reads for longer than that, so a read-back
# recording what it found behind it, or the invoice backfill, failed instead of
# waiting its turn (measured 27 Oct 2026: 40,000 units). PostgreSQL never
# blocks a write behind a read.
SQLITE_LOCK_WAIT = 30.0
#: Seconds to wait for a PostgreSQL server to answer a new connection. Without
#: one, a network that drops a reply leaves the app waiting for ever; a hosted
#: database that sleeps when idle (Neon) wakes well within it.
PG_CONNECT_TIMEOUT = 15


def engine_options(url: str) -> dict:
    """What an engine for this database is built with, beside the address: one
    rule for the app's engine and any other this program opens (the database
    copy, app.db.copy_database)."""
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False, "timeout": SQLITE_LOCK_WAIT}}
    return {
        "connect_args": {"connect_timeout": PG_CONNECT_TIMEOUT},
        # Every pooled connection is tested before use. A hosted database that
        # sleeps when idle (Neon, after five quiet minutes) closes the
        # connections the pool still holds, and without the test the first
        # request after a quiet spell failed with "the connection is closed".
        "pool_pre_ping": True,
    }


engine = create_engine(
    _settings.database_url,
    future=True,
    # A failed statement must not print its parameters into the log: an order
    # row's parameters carry the card number (audit F-7, 25 Oct 2026).
    hide_parameters=True,
    **engine_options(_settings.database_url),
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
)


def pin_to_utc(dbapi_connection, _record=None) -> None:
    """Pin one new PostgreSQL connection to UTC, and commit it at once (see
    :func:`_pin_session_to_utc`) — for any engine this program opens."""
    cursor = dbapi_connection.cursor()
    cursor.execute("SET TIME ZONE 'UTC'")
    cursor.close()
    dbapi_connection.commit()


#: A hosted PostgreSQL's pooled address (Neon names it "<endpoint>-pooler.…").
#: Its pooler hands each transaction whatever server connection is free, so a
#: setting made per connection — the UTC pin below — does not follow the app
#: from one transaction to the next. The copy tool sets UTC on the database
#: itself, which the pooler keeps; the direct address needs neither.
POOLED_HOST_MARK = "-pooler."


if engine.dialect.name == "postgresql":
    if POOLED_HOST_MARK in (engine.url.host or ""):
        log.warning(
            "ORDERING_DATABASE_URL is a pooled address (%s): use the direct one (the same without "
            "'-pooler'), or make sure the database itself is set to UTC — the copy tool does that",
            engine.url.host,
        )

    @event.listens_for(engine, "connect")
    def _pin_session_to_utc(dbapi_connection, _record) -> None:
        """Every stamp in this schema is UTC held in a plain timestamp column.
        PostgreSQL turns an aware value into the session's wall clock on the
        way in, so a host whose server or client zone is Asia/Kolkata would
        store 15:30 for 10:00Z, and every age, idle time and duration compared
        against utcnow() would be off by the offset (review of 26 Oct 2026).
        Pinning the session zone makes the stored value the UTC instant on
        every host, whatever TZ or PGTZ says.

        Committed at once: the driver runs the SET inside a transaction of its
        own, and the pool rolls a connection back when it is handed back, which
        undid the SET — a connection whose first use ended in a rollback went
        on at the server's zone, so stamps written through it were off by the
        offset (seen 27 Oct 2026 on a server set to Asia/Kolkata)."""
        cursor = dbapi_connection.cursor()
        cursor.execute("SET TIME ZONE 'UTC'")
        cursor.close()
        dbapi_connection.commit()


if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record) -> None:
        """SQLite ignores foreign keys unless each connection asks; PostgreSQL
        never does. Development must fail the way production will (a job
        item for a deleted account crashed a read-back only on PostgreSQL,
        25 Oct 2026), so every SQLite connection enforces them."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# --- keeping the real database out of harm's way -----------------------------
#: Set to "1" to drop tables in a database that is not a throwaway one.
ALLOW_DROP_ENV = "ORDERING_ALLOW_DROP"
#: Where copies of a SQLite file go: a folder beside it.
BACKUP_DIRNAME = "backups"
#: How many start-up copies are kept.
BACKUPS_KEPT = 10
#: Beside the newest copies, the newest copy of each day is kept this long: a
#: burst of restarts must not push every older recovery point out (audit F-16).
BACKUP_DAYS_KEPT = 30
#: Tables whose rows make a database worth copying; one holding none of them
#: is a fresh or emptied file, and a copy of that would only push a good copy
#: out of the ring.
_WORTH_COPYING = ("accounts", "orders", "addresses", "gsts", "invoices")


def sqlite_path(url: URL) -> str | None:
    """The file behind a SQLite URL, or None (another backend, or in memory)."""
    if url.get_backend_name() != "sqlite":
        return None
    database = url.database or ""
    if database in ("", ":memory:") or database.startswith("file:"):
        return None
    return os.path.realpath(database)


def is_throwaway(url: URL) -> bool:
    """Whether a database may be dropped without a second thought: SQLite in
    memory, or a SQLite file under the temp directory (what the test fixtures
    and scratch scripts use). A PostgreSQL database never counts."""
    if url.get_backend_name() != "sqlite":
        return False
    path = sqlite_path(url)
    if path is None:
        return True
    roots = {os.path.realpath(tempfile.gettempdir()), os.path.realpath("/tmp")}
    return any(path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)


#: What Alembic calls the copy it fills when it rebuilds a table in batch mode
#: (alembic/operations/batch.py), cut, as Alembic cuts it, to 50 characters.
ALEMBIC_COPY_PREFIX = "_alembic_tmp_"
_ALEMBIC_COPY_NAME_LIMIT = 50


def _alembic_swaps_a_copy(target, connection, runner) -> bool:
    """Whether a drop is Alembic's own copy-and-swap of one SQLite table.

    SQLite can alter almost nothing in place, so a batch revision
    (alembic/env.py) rebuilds the table: it creates ``_alembic_tmp_<name>``,
    copies every row into it, drops the table and renames the copy — and
    drops the copy itself if anything fails on the way. Neither drop loses a
    row, and refusing them stopped the documented way of putting the laptop's
    file under Alembic (docs/DEPLOY.md §5: revision 0005 rebuilds
    ``accounts``). So they pass, and nothing else does:

    * the drop comes from Alembic's own DDL runner, not SQLAlchemy's — every
      ``Table.drop()``, ``drop_all``, fixture and script goes through
      SQLAlchemy's;
    * it is one table (the metadata-level hook gets a MetaData, from ``drop_all``);
    * that table is Alembic's copy, or the copy sits beside it holding exactly
      as many rows. A revision's own ``op.drop_table`` has no copy beside it,
      and a copy an earlier failed attempt left behind is empty, so it cannot
      let through the drop of a table that has rows.
    """
    from sqlalchemy import func, inspect, select, table

    if not isinstance(target, Table):
        return False
    if not type(runner).__module__.startswith("alembic."):
        return False
    if target.name.startswith(ALEMBIC_COPY_PREFIX):
        return True
    copy = (ALEMBIC_COPY_PREFIX + target.name)[:_ALEMBIC_COPY_NAME_LIMIT]
    if not inspect(connection).has_table(copy, schema=target.schema):
        return False
    rows = connection.scalar(select(func.count()).select_from(table(target.name, schema=target.schema)))
    copied = connection.scalar(select(func.count()).select_from(table(copy, schema=target.schema)))
    return rows == copied


def refuse_drop_unless_throwaway(target, connection, **kw) -> None:
    """Refuse ``drop_all`` against anything but a throwaway database.

    On 25 Oct 2026 the development database — the accounts, a signed-in
    Flipkart session, the Address Book, the order history — was emptied by a
    table drop that reached it from a script run beside the app, and no copy
    existed. Dropping tables is what the test fixtures do to their own temp
    file; against any other database it is refused here, at the engine, where
    every SQLAlchemy path (``drop_all``, one table's ``drop()``, a fixture, a
    script, a shell one-liner) has to pass — a raw ``DROP TABLE`` sent as SQL
    text cannot be caught at this layer, which is what the start-up copy is
    for. Set ``ORDERING_ALLOW_DROP=1`` to do it on purpose.

    The one drop let through is Alembic swapping a rebuilt SQLite table for
    its full copy (:func:`_alembic_swaps_a_copy`), which loses nothing.
    """
    if os.environ.get(ALLOW_DROP_ENV) == "1":
        return
    url = getattr(connection.engine, "url", None)
    if url is None:
        # A stand-in connection with no database behind it, such as the one
        # ``alembic ... --sql`` writes statements out through for review:
        # nothing can be dropped through it. A real connection always has one.
        return
    if is_throwaway(url):
        return
    if _alembic_swaps_a_copy(target, connection, kw.get("_ddl_runner")):
        return
    raise RuntimeError(
        f"refusing to drop tables in {url.render_as_string(hide_password=True)}: only a "
        "throwaway database (SQLite in memory, or a file under the temp directory) may be "
        f"dropped. Set {ALLOW_DROP_ENV}=1 if you really mean it."
    )


#: Both levels: the metadata hook meets ``drop_all``, the Table-class hook
#: meets any single table's ``drop()`` (of any metadata), so neither path
#: slips by. A ``drop_all`` meets the refusal twice; that is harmless.
event.listen(Base.metadata, "before_drop", refuse_drop_unless_throwaway)
event.listen(Table, "before_drop", refuse_drop_unless_throwaway, propagate=True)


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _holds_rows(path: str) -> bool:
    """Whether the file has a row in any table worth copying."""
    try:
        # closing(): sqlite3's own "with" commits but leaves the file open.
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as con:
            names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in _WORTH_COPYING:
                if table in names and con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    return True
    except sqlite3.Error as exc:
        log.warning("could not read %s to decide on a backup: %s", path, exc)
    return False


def backup_sqlite(keep: int = BACKUPS_KEPT, now: datetime | None = None) -> str | None:
    """Copy the SQLite file beside itself before the schema is touched.

    ``backups/ordering-YYYYmmdd-HHMMSS.db`` next to the database, made with
    SQLite's own backup API so the copy is whole even while the app writes,
    the newest ``keep`` kept. Nothing is copied for a throwaway database, for
    another backend (PostgreSQL has pg_dump — DEPLOY.md), for a file that
    holds no rows worth keeping (a copy of an emptied file must not push a
    good copy out), or when the file has not changed since the last copy (a
    development server restarts on every save). Returns the copy's path.
    """
    url = engine.url
    path = sqlite_path(url)
    if path is None or is_throwaway(url) or not os.path.exists(path):
        return None
    if not _holds_rows(path):
        log.info("no backup of %s: it holds no rows worth copying", path)
        return None
    folder = os.path.join(os.path.dirname(path), BACKUP_DIRNAME)
    os.makedirs(folder, exist_ok=True)
    stem = os.path.splitext(os.path.basename(path))[0]
    # A copy that was cut short (disk full, the process killed) is left as a
    # ".part" and must never pass for the newest good copy: swept here, never
    # counted in the ring.
    for name in os.listdir(folder):
        if name.endswith(".part"):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(folder, name))
    # A checksum whose copy never got its name is a leftover of the same.
    for name in os.listdir(folder):
        if name.endswith(".db.sha256") and not os.path.exists(os.path.join(folder, name[: -len(".sha256")])):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(folder, name))
    existing = sorted(
        name for name in os.listdir(folder) if name.startswith(f"{stem}-") and name.endswith(".db")
    )
    digest = _file_digest(path)
    if existing:
        latest = os.path.join(folder, existing[-1])
        try:
            with open(latest + ".sha256") as handle:
                if handle.read().strip() == digest:
                    return None  # unchanged since the last copy
        except OSError:
            pass
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = os.path.join(folder, f"{stem}-{stamp}.db")
    if os.path.exists(target):
        target = os.path.join(folder, f"{stem}-{stamp}-{len(existing)}.db")
    partial = target + ".part"
    # Both connections CLOSED before the rename. sqlite3's own "with" only
    # commits and leaves the files open, and Windows will not rename an open
    # file: every copy on the owner's laptop stayed a ".part" and was swept at
    # the next start, so there was no backup at all (found 18 Nov 2026).
    with contextlib.closing(sqlite3.connect(path)) as source, contextlib.closing(
        sqlite3.connect(partial)
    ) as copy:
        source.backup(copy)
    # The checksum first, the final name last: a copy is a copy only once both
    # are there, so a crash between the two leaves a .part, never a half file
    # under a trusted name.
    with open(target + ".sha256", "w") as handle:
        handle.write(digest + "\n")
    os.replace(partial, target)
    existing.append(os.path.basename(target))
    _prune_backups(folder, stem, sorted(existing), keep, today=(now or datetime.now()).date())
    log.info("backed up %s to %s", path, target)
    return target


def _prune_backups(folder: str, stem: str, names: list[str], keep: int, *, today, days_kept: int = BACKUP_DAYS_KEPT) -> list[str]:
    """Remove the copies that neither the ring nor the daily tier keeps.

    Kept: the newest ``keep`` copies, and the newest copy of every day within
    ``days_kept`` days of ``today`` — so ten restarts in an hour cost at most
    the hour's intermediate copies, never yesterday's or last week's state.
    Returns the names removed.
    """
    newest = set(names[-keep:]) if keep > 0 else set()
    by_day: dict[str, list[str]] = {}
    prefix = len(stem) + 1
    for name in names:
        by_day.setdefault(name[prefix : prefix + 8], []).append(name)
    daily: set[str] = set()
    for day, same_day in by_day.items():
        try:
            when = datetime.strptime(day, "%Y%m%d").date()
        except ValueError:
            continue
        if 0 <= (today - when).days <= days_kept:
            daily.add(sorted(same_day)[-1])
    removed: list[str] = []
    for name in names:
        if name in newest or name in daily:
            continue
        for suffix in ("", ".sha256"):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(folder, name + suffix))
        removed.append(name)
    return removed


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables, then add any columns the models gained since.

    A database built from nothing here is stamped with the newest Alembic
    revision, so ``alembic upgrade head`` later knows where it stands; one
    that already had tables is never stamped (app.db.migrations).

    Safe to call repeatedly.
    """
    from sqlalchemy import inspect

    from app.db import models  # noqa: F401  (register models on Base.metadata)

    # A copy first, before anything below touches the schema: the one thing
    # that would have saved the development data on 25 Oct 2026.
    try:
        backup_sqlite()
    except Exception:  # noqa: BLE001 — a failed copy must not stop the app
        log.exception("could not back up the database before start-up")
    tables_before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(bind=engine)
    try:
        from app.db.migrations import stamp_new_database

        stamped = stamp_new_database(engine, tables_before)
        if stamped:
            log.info("stamped the new database with Alembic revision %s", stamped)
    except Exception:  # noqa: BLE001 — a missing stamp must not stop the app
        log.exception("could not stamp the new database with its Alembic revision")
    added = sync_missing_columns()
    if added:
        log.info("added missing columns: %s", ", ".join(added))
    indexes = sync_missing_indexes()
    if indexes:
        log.info("created missing indexes: %s", ", ".join(indexes))
    values = sync_enum_values()
    if values:
        log.info("added missing enum values: %s", ", ".join(values))
    derived = sync_derived_columns()
    if derived:
        log.info("backfilled derived columns on %d row(s)", derived)
    from app.core.order_units import sync_unit_readings

    readings = sync_unit_readings()
    if any(readings.values()):
        log.info(
            "brought %d delivery reading(s) and %d timeline(s) in line with the current code; "
            "%d order(s) that no longer stand marked failed",
            readings["states"],
            readings["timelines"],
            readings.get("orders", 0),
        )
    owned = sync_alert_closes()
    if owned:
        log.info("marked %d alert(s) closed before resolved_by existed as closed by hand", owned)
    from app.core.order_units import install_flush_guard

    install_flush_guard()
    units = sync_order_units()
    if units:
        log.info("created unit rows for %d order(s) written before order_units existed", units)
    carried = sync_business_block()
    if carried:
        log.info(
            "carried the retired business settings into the Address Book: %s",
            ", ".join(carried),
        )
    # The configured admin's user row (app.core.users): the bootstrap account.
    from app.core.users import seed_env_admin

    seed_env_admin()


#: The Settings keys the "Business details" group wrote before the Address Book
#: replaced it — and the ``ORDERING_*`` environment variables of the same names.
RETIRED_BUSINESS_KEYS: tuple[str, ...] = (
    "default_gstin",
    "default_business_name",
    "default_address",
    "default_pin",
)


#: The ``.env`` file the retired block is read from — beside the app, as
#: :class:`app.config.Settings` reads its own. None reads the process
#: environment only; the test suite sets that, so a developer's own file can
#: never seed a test database.
RETIRED_ENV_FILE: str | None = ".env"

#: The Settings row that records the environment's block was carried. A
#: ``.env`` cannot be scrubbed the way the saved row is, so this is what makes
#: the carry happen exactly once rather than at every start.
BUSINESS_BLOCK_MARK = "business_block_carried"


def sync_alert_closes() -> int:
    """Give alerts closed before ``resolved_by`` existed an owner — once.

    A row closed by hand before the column existed must not be reopened by the
    first rules pass after the upgrade as if a rule had closed it. Every
    resolved row with no owner is marked as a person's close: the first pass
    then flips the ones whose problem has already cleared to a rule's close
    (see ``app.core.alerts._apply``) and leaves the ones whose problem persists
    closed — which is what the hand close meant. Returns how many rows changed;
    nothing is left to change after the first run.
    """
    from sqlalchemy import update

    from app.db.models import Alert

    with SessionLocal() as db:
        result = db.execute(
            update(Alert)
            .where(Alert.resolved.is_(True), Alert.resolved_by.is_(None))
            .values(resolved_by="user")
            .execution_options(synchronize_session=False)
        )
        db.commit()
        return int(result.rowcount or 0)


def _retired_business_block() -> dict[str, str]:
    """The four retired keys as the environment supplies them.

    Read the way :class:`app.config.Settings` reads its own values — the
    ``ORDERING_`` environment and the ``.env`` file beside the app — rather
    than from ``os.environ`` alone, which never sees a ``.env`` file: the
    block that most installations had lives in exactly that file.
    """
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class _RetiredBlock(BaseSettings):
        model_config = SettingsConfigDict(
            env_file=RETIRED_ENV_FILE, env_prefix="ORDERING_", extra="ignore"
        )

        default_gstin: str = ""
        default_business_name: str = ""
        default_address: str = ""
        default_pin: str = ""

    block = _RetiredBlock()
    return {key: str(getattr(block, key) or "").strip() for key in RETIRED_BUSINESS_KEYS}


def _free_label(db, model, base: str) -> str:
    """``base``, or ``base-2``, ``base-3`` … when the label is already taken."""
    from sqlalchemy import select

    taken = set(db.scalars(select(model.label).where(model.label.like(f"{base}%"))).all())
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def sync_business_block() -> list[str]:
    """Carry the retired Settings "Business details" into the Address Book, once.

    Until Phase 4 the GSTIN, business name, delivery address and pin were four
    flat settings every run fell back to. They are not settings any more, and a
    run with no Address Book address is refused — so an installation that still
    relied on them must not wake up unable to order, or ordering under no GST
    where it used to have one. At start-up this reads the retired keys from the
    saved settings row (what the Settings UI wrote) and, failing that, from the
    ``ORDERING_DEFAULT_*`` environment and ``.env`` file, and:

    * when a GSTIN of the right shape is set and no GST carries it, adds that
      GST — as the default when the table was empty, beside the others
      otherwise; one of the wrong shape is skipped with a warning, never
      saved (the API refuses it too, audit F-24);
    * when no address is on file and an address line and a 6-digit pin are set,
      creates it as the default address, linked to the GST the saved GSTIN
      names — to none when the block named a GSTIN that was refused, and to
      the default GST only when the block named no GSTIN at all;
    * removes the retired keys from the saved row, and records in
      :data:`BUSINESS_BLOCK_MARK` that an environment block was carried, so
      this runs once — a deleted address does not come back at the next start.

    Addresses already on file are left exactly as they are: the saved address
    is the one that shipped to the wrong place, and the operator has since
    entered the right ones. Returns what was created, for the log; a failure
    is logged and leaves the app to start without it.
    """
    import re

    from sqlalchemy import func, select

    from app.core.app_settings import SETTINGS_KEY
    from app.db.models import Address, Gst, Setting, utcnow
    from app.schemas.address import is_gstin, normalise_gstin

    created: list[str] = []
    with SessionLocal() as db:
        row = db.get(Setting, SETTINGS_KEY)
        saved = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
        carried_before = db.get(Setting, BUSINESS_BLOCK_MARK) is not None
        env = {} if carried_before else _retired_business_block()
        values: dict[str, str] = {}
        for key in RETIRED_BUSINESS_KEYS:
            raw = saved.get(key)
            values[key] = str(raw).strip() if raw not in (None, "") else env.get(key, "")
        from_env = any(env.values())
        stale = [key for key in RETIRED_BUSINESS_KEYS if key in saved]
        if not any(values.values()) and not stale:
            return created
        try:
            explained = False
            gstin_named = bool(values["default_gstin"])
            # Stripped and upper-cased exactly as the API does it, and held to
            # the same shape: the API would refuse a malformed GSTIN, so the
            # carry-over must not save one behind its back (audit F-24).
            gstin = str(normalise_gstin(values["default_gstin"]))
            name = values["default_business_name"]
            if gstin and not is_gstin(gstin):
                log.warning(
                    "the retired business GSTIN %r is not a valid GSTIN (15 characters: 2 digits, "
                    "5 letters, 4 digits, a letter, a digit 1-9 or a letter, Z, a digit or a letter) "
                    "and was not carried: add the GST by hand in the Address Book, and link the "
                    "carried address to it",
                    gstin,
                )
                gstin = ""
                explained = True
            gst: Gst | None = None
            if gstin:
                gst = db.scalars(select(Gst).where(func.upper(Gst.gstin) == gstin).limit(1)).first()
                if gst is None:
                    first = db.scalar(select(Gst.id).limit(1)) is None
                    gst = Gst(
                        label=_free_label(db, Gst, "Default"),
                        gstin=gstin,
                        business_name=name or "Default",
                        is_default=first,
                    )
                    db.add(gst)
                    db.flush()
                    created.append(f"GST {gstin}")
            line = values["default_address"]
            pin = values["default_pin"]
            # ASCII digits only: Python's \d is any Unicode digit, and a
            # full-width pincode would be typed into Flipkart as it is.
            if line and re.fullmatch(r"[0-9]{6}", pin) and db.scalar(select(Address.id).limit(1)) is None:
                # Linked to the GST the block named. When it named one that was
                # refused, to none — never to whichever entity happens to be
                # the default; that is only right when the block named no GST.
                if gstin_named:
                    linked = gst
                else:
                    linked = db.scalars(select(Gst).where(Gst.is_default.is_(True)).limit(1)).first()
                db.add(
                    Address(
                        label="Default",
                        name=name or "Default",
                        line1=line,
                        pincode=pin,
                        gst_id=None if linked is None else linked.id,
                        is_default=True,
                    )
                )
                created.append(f"address pin {pin}")
            if stale and row is not None:
                # Reassign rather than mutate: a plain JSON column is not change-tracked.
                row.value = {k: v for k, v in saved.items() if k not in RETIRED_BUSINESS_KEYS}
            if from_env:
                # The environment cannot be scrubbed, so remember it was read.
                db.add(
                    Setting(
                        key=BUSINESS_BLOCK_MARK,
                        value={"at": utcnow().isoformat(), "keys": [k for k in RETIRED_BUSINESS_KEYS if env.get(k)]},
                    )
                )
                log.info(
                    "the ORDERING_DEFAULT_* block in the environment was read once and will "
                    "not be read again; the keys can be deleted from .env"
                )
            if not created and any(values.values()) and not explained:
                log.warning(
                    "a retired business block was found but nothing was carried: the Address "
                    "Book already holds what it names"
                )
            db.commit()
        except Exception:  # noqa: BLE001 — logged; the app still starts
            log.exception("could not carry the business settings into the Address Book")
            db.rollback()
            return []
    return created


def sync_order_units(batch: int = 500) -> int:
    """Give every order written before ``order_units`` existed its unit rows.

    One placeholder per requested unit, the first carrying what the order row
    already knew from a read-back (app.core.order_units.ensure_units). A unit
    id another order's unit already claims — a duplicate order row — is left
    off the new unit rather than failing the start-up. Returns how many orders
    were backfilled; a failure is logged and leaves the rest for next time.
    """
    from sqlalchemy import exists, select

    from app.core.order_units import PLATFORM, ensure_units
    from app.db.models import Order, OrderUnit

    done = 0
    with SessionLocal() as db:
        while True:
            missing = (
                select(Order)
                .where(~exists().where(OrderUnit.order_pk == Order.id))
                .order_by(Order.id)
                .limit(batch)
            )
            orders = db.scalars(missing).all()
            if not orders:
                break
            try:
                for order in orders:
                    first = ensure_units(order)[0]
                    if first.unit_id is not None:
                        claimed = db.scalar(
                            select(OrderUnit.id)
                            .where(OrderUnit.platform == PLATFORM, OrderUnit.unit_id == first.unit_id)
                            .limit(1)
                        )
                        if claimed is not None:
                            first.unit_id = None
                    db.flush()
                    done += 1
                db.commit()
            except Exception:  # noqa: BLE001 — logged; the app still starts
                log.exception("could not backfill unit rows; %d order(s) done so far", done)
                db.rollback()
                break
    return done


def sync_derived_columns(batch: int = 500) -> int:
    """Fill ``orders.delivery_state`` and ``orders.processing_stage`` /
    ``orders.error_code`` on rows written before those columns existed.

    The columns are derived on every assignment / attempt write, but rows that
    predate them hold NULL. Done in batches through the ORM. Returns how many
    rows were updated.
    """
    from app.core import order_outcomes, run_notes
    from app.db.models import Order, OrderStatus

    updated = 0
    with SessionLocal() as db:
        while True:
            rows = db.query(Order).filter(Order.delivery_state.is_(None)).limit(batch).all()
            if not rows:
                break
            for row in rows:
                row.platform_status = row.platform_status  # the validator derives it
            db.commit()
            updated += len(rows)
        while True:
            rows = db.query(Order).filter(Order.processing_stage.is_(None)).limit(batch).all()
            if not rows:
                break
            notes = run_notes.messages_for(
                db, [r for r in rows if r.status != OrderStatus.placed]
            )
            for row in rows:
                status_val = getattr(row.status, "value", str(row.status))
                reason = run_notes.reason_text(notes.get(row.id))
                stage, code = order_outcomes.classify(status_val, reason)
                row.processing_stage = stage.value
                row.error_code = code.value if code is not None else None
            db.commit()
            updated += len(rows)
    return updated


def sync_missing_indexes() -> list[str]:
    """Create the indexes the models declare but an existing table lacks.

    ``create_all()`` builds indexes only for tables it creates, and
    :func:`sync_missing_columns` adds columns without their indexes, so an
    index declared after the table first existed is otherwise never built —
    and a ledger filter that should take milliseconds scans the table instead.
    Like the other sync steps, this only ADDS.
    """
    from sqlalchemy import inspect

    created: list[str] = []
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        present = {ix["name"] for ix in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if not index.name or index.name in present:
                continue
            try:
                index.create(bind=engine, checkfirst=True)
                created.append(f"{table.name}.{index.name}")
            except Exception as exc:  # noqa: BLE001
                log.warning("could not create index %s on %s: %s", index.name, table.name, exc)
    return created


def sync_enum_values() -> list[str]:
    """Teach PostgreSQL's native enum types the members the models gained.

    On PostgreSQL an ``Enum`` column is a native type whose allowed values are
    fixed when the type is created, so a member added to the Python enum later
    (``OrderStatus.unconfirmed``) is refused on insert until the type learns it.
    SQLite stores these as plain strings and needs nothing.

    Like :func:`sync_missing_columns`, it only ADDS.
    """
    if engine.dialect.name != "postgresql":
        return []
    from sqlalchemy import Enum, text

    added: list[str] = []
    seen: set[str] = set()
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            kind = column.type
            if not isinstance(kind, Enum) or not kind.native_enum or not kind.name:
                continue
            if kind.name in seen:
                continue
            seen.add(kind.name)
            with engine.connect() as conn:
                present = set(
                    conn.execute(
                        text(
                            "SELECT e.enumlabel FROM pg_enum e "
                            "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :name"
                        ),
                        {"name": kind.name},
                    ).scalars()
                )
            if not present:
                continue  # create_all() just made the type, so it is already whole
            for value in kind.enums:
                if value in present:
                    continue
                try:
                    # ADD VALUE may not run inside a transaction before PostgreSQL 12.
                    with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
                        conn.execute(text(f"ALTER TYPE {kind.name} ADD VALUE IF NOT EXISTS '{value}'"))
                    added.append(f"{kind.name}.{value}")
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not add %s to enum %s: %s", value, kind.name, exc)
    return added


def sync_missing_columns() -> list[str]:
    """Add columns present on the models but missing from an existing table.

    ``create_all()`` creates missing TABLES but never alters existing ones, so a
    column added to a model after the database was first created shows up as a
    confusing 500 on the next insert rather than as a schema error. Every schema
    change in this project so far has been additive, so closing that specific
    gap removes a whole class of "it works on a fresh database" bugs.

    Deliberately limited: it only ADDS columns. Dropping, renaming or re-typing
    needs a real migration (see docs/DEPLOY.md §5) and is not attempted here —
    quietly guessing at those is how data gets lost.
    """
    from sqlalchemy import inspect, text

    added: list[str] = []
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue  # create_all() just made it, so it is already current
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue

            default = getattr(column.default, "arg", None)
            if not column.nullable and default is None and column.server_default is None:
                # A NOT NULL column with no default cannot be added to a table
                # that already has rows — say so instead of failing obscurely.
                log.warning(
                    "cannot auto-add %s.%s (NOT NULL with no default) — needs a migration",
                    table.name,
                    column.name,
                )
                continue

            ddl = f"{column.name} {column.type.compile(engine.dialect)}"
            if default is not None and not callable(default):
                literal = f"'{default}'" if isinstance(default, str) else (
                    str(int(default)) if isinstance(default, bool) else str(default)
                )
                ddl += f" DEFAULT {literal}"
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
                added.append(f"{table.name}.{column.name}")
            except Exception as exc:  # noqa: BLE001
                log.warning("could not add %s.%s: %s", table.name, column.name, exc)

    return added
