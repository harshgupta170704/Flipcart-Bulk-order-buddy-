"""Query strings stay out of uvicorn's access line (audit F-29, F-20).

uvicorn writes one line per request with the path as asked, and a query
string can carry the archive download's token or the ledger's card filter.
These build the record the way uvicorn's protocol code does, and run it
through uvicorn's own logging set-up and formatter.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter

from app.core.logging_setup import UVICORN_ACCESS_LOGGER, StripQueryString, configure_logging
from app.main import create_app

#: The message uvicorn logs every request with (uvicorn/protocols/http/
#: h11_impl.py and httptools_impl.py), followed by the client's address and
#: port, the method, the path with its query string, the version and the status.
UVICORN_ACCESS_MESSAGE = '%s - "%s %s HTTP/%s" %d'
CLIENT = "203.0.113.9:51234"
DOWNLOAD = "/invoices/archives/7/download?token=1761480000.0f3a9c"
LEDGER = "/orders?card=4242&page=2"


def _access_record(path: str, status: int = 200) -> logging.LogRecord:
    """A record exactly as uvicorn makes one for a request to ``path``."""
    return logging.LogRecord(
        UVICORN_ACCESS_LOGGER, logging.INFO, "h11_impl.py", 477,
        UVICORN_ACCESS_MESSAGE, (CLIENT, "GET", path, "1.1", status), None,
    )


@pytest.fixture
def logger_state():
    """Leave the loggers as they were.

    dictConfig resets every ``uvicorn`` logger and re-enables any other, and
    configure_logging sets the ``ordering`` ones; the rest of the suite must
    inherit neither. The root logger is left alone: pytest swaps its capture
    handlers on it between the phases of a test.
    """
    logging.getLogger("uvicorn")  # made real now, so it is saved and put back like the rest
    loggers = [lg for lg in list(logging.root.manager.loggerDict.values()) if isinstance(lg, logging.Logger)]
    ours = {lg for lg in loggers if lg.name.split(".")[0] in ("uvicorn", "ordering")}
    saved = [(lg, lg.level, list(lg.handlers), list(lg.filters), lg.propagate) for lg in ours]
    disabled = [(lg, lg.disabled) for lg in loggers]
    yield
    for lg, level, handlers, filters, propagate in saved:
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.filters[:] = filters
        lg.propagate = propagate
    for lg, was_disabled in disabled:
        lg.disabled = was_disabled


def test_the_query_string_is_cut_off_the_path():
    record = _access_record(DOWNLOAD)
    assert StripQueryString().filter(record) is True  # the line itself is kept
    assert record.args == (CLIENT, "GET", "/invoices/archives/7/download", "1.1", 200)
    assert record.getMessage() == f'{CLIENT} - "GET /invoices/archives/7/download HTTP/1.1" 200'

    record = _access_record(LEDGER)
    StripQueryString().filter(record)
    assert record.getMessage() == f'{CLIENT} - "GET /orders HTTP/1.1" 200'


def test_a_line_with_nothing_to_cut_is_left_as_it_was():
    record = _access_record("/health")
    StripQueryString().filter(record)
    assert record.args == (CLIENT, "GET", "/health", "1.1", 200)

    # A '?' that is part of the path reaches the log percent-encoded, and stays.
    record = _access_record("/files/what%3F.pdf")
    StripQueryString().filter(record)
    assert record.args[2] == "/files/what%3F.pdf"

    # Any other line on the logger passes untouched.
    plain = logging.LogRecord(UVICORN_ACCESS_LOGGER, logging.INFO, __file__, 1, "no arguments", None, None)
    assert StripQueryString().filter(plain) is True
    assert plain.args is None and plain.getMessage() == "no arguments"


def test_uvicorns_own_formatter_writes_the_short_line():
    """uvicorn's formatter unpacks the five arguments by position; the filtered
    record must still fit it. Should a later uvicorn log a different shape,
    its formatter changes with it and this fails: the cue to look at
    StripQueryString again."""
    formatter = AccessFormatter(fmt=LOGGING_CONFIG["formatters"]["access"]["fmt"], use_colors=False)
    record = _access_record(LEDGER)
    StripQueryString().filter(record)
    line = formatter.format(record)
    assert line.endswith(f'{CLIENT} - "GET /orders HTTP/1.1" 200 OK')
    assert "card" not in line and "4242" not in line


#: The backend folder, for running a child interpreter that imports the app.
BACKEND = Path(__file__).resolve().parents[1]


def _access_lines_from_a_fresh_interpreter(order: str) -> str:
    """What uvicorn's access logger writes when uvicorn's logging set-up and
    the app's run in ``order``, with the two requests logged after.

    In a child interpreter: dictConfig closes every handler in the process,
    pytest's own capture handlers among them, and leaves the ``uvicorn``
    loggers set up for the rest of the suite."""
    code = textwrap.dedent(
        f"""
        import copy, logging, logging.config
        from uvicorn.config import LOGGING_CONFIG
        from app.core.logging_setup import UVICORN_ACCESS_LOGGER, configure_logging

        config = copy.deepcopy(LOGGING_CONFIG)
        config["formatters"]["default"]["use_colors"] = False
        config["formatters"]["access"]["use_colors"] = False
        uvicorns = lambda: logging.config.dictConfig(config)
        for step in {{"uvicorn-first": (uvicorns, configure_logging), "app-first": (configure_logging, uvicorns)}}[{order!r}]:
            step()
        access = logging.getLogger(UVICORN_ACCESS_LOGGER)
        access.info({UVICORN_ACCESS_MESSAGE!r}, {CLIENT!r}, "GET", {DOWNLOAD!r}, "1.1", 200)
        access.info({UVICORN_ACCESS_MESSAGE!r}, {CLIENT!r}, "GET", {LEDGER!r}, "1.1", 200)
        """
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout  # uvicorn sends its access lines to stdout


def test_the_app_filters_the_access_line_after_uvicorn_has_set_up_its_loggers():
    """The order of a real start-up, the Docker CMD's and each --reload
    child's alike: uvicorn applies its logging set-up, then imports the app,
    whose create_app() calls configure_logging()."""
    out = _access_lines_from_a_fresh_interpreter("uvicorn-first")
    assert f'{CLIENT} - "GET /invoices/archives/7/download HTTP/1.1" 200 OK' in out
    assert f'{CLIENT} - "GET /orders HTTP/1.1" 200 OK' in out
    assert "token" not in out and "card" not in out and "4242" not in out


def test_the_filter_outlasts_uvicorn_setting_up_its_loggers_afterwards():
    """The other way round, as when something imports the app before uvicorn
    configures logging: dictConfig replaces the logger's handlers, never its
    filters."""
    out = _access_lines_from_a_fresh_interpreter("app-first")
    assert '"GET /invoices/archives/7/download HTTP/1.1" 200 OK' in out
    assert "token" not in out and "4242" not in out


def test_building_the_app_installs_the_filter_once(logger_state):
    """What uvicorn's import of ``app.main:app`` runs; the suite builds apps by
    the dozen, so a second build must not stack a second filter."""
    access = logging.getLogger(UVICORN_ACCESS_LOGGER)
    access.filters[:] = []
    create_app()
    create_app()
    configure_logging()
    assert sum(isinstance(f, StripQueryString) for f in access.filters) == 1
