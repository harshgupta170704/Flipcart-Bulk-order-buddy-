"""Where the files outside ``app/`` are, for the tests that read them.

The API and the web app live in two repositories. This one (the API) also
holds what deploys them together: ``docker-compose.yml`` and ``docs/``. A
handful of tests pin promises that cross the two — nginx's config against the
API's routes, the ledger's column list against the page's, compose against
the web image — and those need the web app's files: the web repository
checked out beside this one, as the deploy guide lays them out.

Without it those tests are skipped, saying why, and everything else runs: a
fresh clone of this repository alone must pass.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

#: The names the web app's folder goes by beside this one: the repository's
#: own name, and ``frontend`` (one folder holding both, as on a developer's
#: machine). ``ORDERING_WEB_DIR`` names any other place.
WEB_FOLDER_NAMES = ("elex-bulk-ordering-web", "frontend")


def _web_dir() -> Path | None:
    named = os.environ.get("ORDERING_WEB_DIR", "").strip()
    candidates = [Path(named).expanduser()] if named else []
    candidates += [BACKEND.parent / name for name in WEB_FOLDER_NAMES]
    for candidate in candidates:
        if (candidate / "nginx.conf").is_file() and (candidate / "src").is_dir():
            return candidate.resolve()
    return None


#: The web app's folder, or None when it is not checked out beside this one.
WEB = _web_dir()
NO_WEB = (
    "needs the web app's files: check out the web repository beside this one "
    f"(as {' or '.join(WEB_FOLDER_NAMES)}), or set ORDERING_WEB_DIR"
)
#: ``pytestmark = layout.needs_web`` for a module that is all about the web app's files.
needs_web = pytest.mark.skipif(WEB is None, reason=NO_WEB)
#: What a path under the web app is built on when it is absent — never read,
#: since every test that would is skipped.
_WEB_ROOT = WEB if WEB is not None else BACKEND.parent / WEB_FOLDER_NAMES[0]

COMPOSE = BACKEND / "docker-compose.yml"
DOCS = BACKEND / "docs"


def web(*parts: str) -> Path:
    """A file of the web app. Inside a test, the test is skipped when the web
    app is not there; at a module's top, pair it with ``needs_web``."""
    return _WEB_ROOT.joinpath(*parts)


def web_or_skip(*parts: str) -> Path:
    """A file of the web app, for one test in a module that otherwise needs
    none of it: that test alone is skipped when the web app is not there."""
    if WEB is None:
        pytest.skip(NO_WEB)
    return WEB.joinpath(*parts)
