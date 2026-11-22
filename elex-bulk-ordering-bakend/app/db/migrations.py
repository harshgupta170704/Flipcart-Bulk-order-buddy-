"""Alembic, as the app sees it (docs/DEPLOY.md §5, audit F-33).

Two things keep the schema in step with the models, each where it fits:

* **Alembic** owns a PostgreSQL deployment's schema. ``alembic upgrade head``
  runs before the API starts (compose's one-shot ``migrate`` service), and
  every model change comes with a revision in ``backend/alembic/versions/``.
  That is what can retype a column, add a NOT NULL one, rename or drop.
* **init_db** still builds a missing schema with ``create_all`` and fills the
  additive gaps (``sync_missing_columns`` and friends). The laptop's SQLite
  database and the test suite run on exactly that, as before.

Where the two meet: a database ``init_db`` has just built from nothing is,
by construction, the schema the newest revision describes (the test suite
holds the revisions to the models), so it is stamped with that revision here.
Alembic then knows where it stands, and a later ``alembic upgrade head``
applies only what came after. A database that already had tables is never
stamped: what an older build left there is for a person to vouch for, with
``alembic stamp`` (DEPLOY.md §5). The stamp is the revision a database was
born at; init_db never moves it afterwards (only Alembic does), which is why
a laptop file init_db has kept up to date is re-stamped before Alembic takes
it over.

Alembic is imported only when it is needed, so an installation whose
environment predates it still starts; it only goes without the stamp.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from alembic.config import Config
    from sqlalchemy.engine import Connection, Engine

log = logging.getLogger("ordering.db")

#: The backend folder: alembic.ini and the alembic/ scripts sit in it, beside
#: the app package (also in the image, which copies all three to /app).
BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
#: Where Alembic records a database's revision (its default table name).
VERSION_TABLE = "alembic_version"

#: How a database is compared with the models: by env.py, for
#: ``alembic revision --autogenerate`` and ``alembic check``, and by the test
#: that holds the revisions to the models, so the two always judge alike.
#: Column types are compared (a retype is exactly what create_all cannot do).
#: Server defaults are not: the models declare none, and the DEFAULT clauses
#: sync_missing_columns gave columns it added to older tables are harmless.
COMPARE_OPTIONS: dict[str, Any] = {"compare_type": True, "compare_server_default": False}


def alembic_config(connection: Connection | None = None) -> Config:
    """alembic.ini as an Alembic ``Config``, for calling Alembic from Python.

    With ``connection`` the commands run on that connection (env.py reads it
    from ``config.attributes``), not on the database the app's settings name:
    how the tests aim a migration at a temporary database, never the owner's.
    """
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str | None:
    """The newest revision in ``alembic/versions`` (None when there is none)."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def stamp_new_database(engine: Engine, tables_before: Iterable[str]) -> str | None:
    """Stamp, at the newest revision, a database ``init_db`` has just built.

    ``tables_before`` is what the database held before ``create_all`` ran.
    Only a database that held none of the app's tables, and no Alembic
    version table, is stamped: its whole schema came from ``create_all`` a
    moment ago, which is what the newest revision describes. One with tables
    is left alone, whatever they are — the laptop's own database among them.

    Returns the revision stamped, or None when nothing was stamped (an
    existing database, or Alembic not installed). A failure is the caller's
    to log; the app starts either way.
    """
    from app.db.base import Base

    before = set(tables_before)
    if VERSION_TABLE in before or before & set(Base.metadata.tables):
        return None
    if not ALEMBIC_INI.exists():
        log.info("no %s beside the app: the new database was not stamped for Alembic", ALEMBIC_INI.name)
        return None
    try:
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
    except ImportError:
        log.info(
            "alembic is not installed, so the new database was not stamped with a revision "
            "(pip install -r requirements.txt); 'alembic stamp head' does it by hand"
        )
        return None
    script = ScriptDirectory.from_config(alembic_config())
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        context.stamp(script, "head")
        return context.get_current_revision()
