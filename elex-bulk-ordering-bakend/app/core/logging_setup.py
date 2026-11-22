"""Where the app's own log lines go — the audit trail above all (spec FR-058).

Python installs a handler of its own only for WARNING and above, and uvicorn
configures its own loggers and nothing else, so under the shipped
``uvicorn app.main:app`` every INFO line the app wrote — sign-ins, password
changes, the ``ordering.audit`` line for each request that changed something —
was dropped on the floor: no audit trail existed in production. This gives
the ``ordering`` logger tree one timestamped handler on stderr (where a
container's logs are read from) at ``ORDERING_LOG_LEVEL``, and keeps the
audit logger at INFO whatever that says: an audit trail nobody can turn down
is the point of one. Called once from the app's start-up; calling it again is
harmless (the level follows the setting; the handler and the filter are added
once).

It also takes the query string off uvicorn's access line (audit F-29, F-20).
uvicorn writes one line per request with the path exactly as asked, query
string and all, and a query string can hold what no log should keep: the
archive download's token, the ledger's card filter, the reset token of a link
mailed before the token moved behind the ``#``.
"""

from __future__ import annotations

import logging
import sys
from typing import IO

from app.config import get_settings

#: The root of the app's loggers; every ``logging.getLogger("ordering.…")``
#: propagates here.
APP_LOGGER = "ordering"
#: One line per request that changed something, with who made it.
AUDIT_LOGGER = "ordering.audit"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
#: ISO 8601 with the offset, so lines from boxes in different zones still sort.
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
#: uvicorn's own logger for its line per request; the server writes it, not the app.
UVICORN_ACCESS_LOGGER = "uvicorn.access"


class StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is at the moment of writing.

    A plain StreamHandler keeps the stderr it saw at set-up; a supervisor
    (or a test) that swaps the stream afterwards would then lose the lines,
    or get them written to a closed file.
    """

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self) -> IO[str]:  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value: IO[str]) -> None:
        pass  # StreamHandler.__init__ assigns the stream once; ignored on purpose


class StripQueryString(logging.Filter):
    """Cuts the query string off the path in uvicorn's access line.

    uvicorn logs a request as ``'%s - "%s %s HTTP/%s" %d'`` with five
    arguments: the client's address and port, the method, the path with its
    query string, the HTTP version and the status. That call is in uvicorn's
    ``protocols/http/h11_impl.py`` and ``httptools_impl.py``, and its access
    formatter unpacks the same five. The path comes percent-encoded, so a
    ``?`` that belongs to it reads ``%3F`` and its first real ``?`` starts the
    query. None of the other four can hold a ``?``, so each text argument is
    cut at its first one: that is exactly the path's query string, and stays
    so should a later uvicorn reorder the arguments. The record is changed
    before any handler formats it, so every handler on the logger writes the
    short path.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.partition("?")[0] if isinstance(arg, str) else arg for arg in record.args
            )
        return True


def level_named(name: str) -> int:
    """The level a setting names, case blind; INFO for a name that is none."""
    return logging.getLevelNamesMapping().get((name or "").strip().upper(), logging.INFO)


def configure_logging() -> logging.Handler:
    """Give the app's loggers a handler and their levels, and uvicorn's
    access line its filter; returns the handler."""
    app_logger = logging.getLogger(APP_LOGGER)
    handler = next((h for h in app_logger.handlers if isinstance(h, StderrHandler)), None)
    if handler is None:
        handler = StderrHandler()
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        app_logger.addHandler(handler)
    app_logger.setLevel(level_named(get_settings().log_level))
    # Its own level, so the audit line passes even when the app is turned down
    # to WARNING: a parent's level is not consulted for a child's record, only
    # its handlers are.
    logging.getLogger(AUDIT_LOGGER).setLevel(logging.INFO)
    # On the logger rather than on a handler, so it applies to whatever
    # handlers uvicorn (or a --log-config) gives that logger. uvicorn sets up
    # its loggers first and imports the app after, in the process that serves
    # the requests: the one process of the Docker CMD, and each fresh child
    # under --reload (the parent that watches for changes never imports the
    # app). So this runs after uvicorn's set-up; were it the other way round,
    # the filter would still stand: dictConfig replaces a logger's handlers
    # but keeps its filters.
    access = logging.getLogger(UVICORN_ACCESS_LOGGER)
    if not any(isinstance(f, StripQueryString) for f in access.filters):
        access.addFilter(StripQueryString())
    return handler
