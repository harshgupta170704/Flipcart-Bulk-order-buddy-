"""Test fixtures.

A throwaway SQLite database is configured *before* any app module imports, so
the engine binds to it. Each test run starts from a clean schema. The admin
the fixtures sign in as is pinned here too, so the suite needs no ``.env``: a
clean checkout must pass, with dev mode off.
"""

import os
import tempfile

# Must be set before importing app modules (engine is built at import time).
# ORDERING_TEST_DATABASE_URL points the whole suite at another database on
# purpose — a scratch PostgreSQL, the engine production runs on — and nothing
# else: the app's own ORDERING_DATABASE_URL is never used by a test, so a
# shell that has it set cannot aim the fixtures at real data.
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="ordering-test-"), "test.db")
_TEST_DB_URL = os.environ.get("ORDERING_TEST_DATABASE_URL") or f"sqlite:///{_TMP_DB}"
# As the app reads the address (app.config.normalise_database_url): a host's
# own "postgresql://" names the psycopg driver.
for _prefix in ("postgresql://", "postgres://"):
    if _TEST_DB_URL.startswith(_prefix):
        _TEST_DB_URL = "postgresql+psycopg://" + _TEST_DB_URL[len(_prefix) :]
os.environ["ORDERING_DATABASE_URL"] = _TEST_DB_URL
if "ORDERING_TEST_DATABASE_URL" in os.environ:
    # A database named for the tests is theirs to drop (app.db.base refuses
    # to drop anything that is not a throwaway file otherwise).
    os.environ["ORDERING_ALLOW_DROP"] = "1"
# Password hashing at the production work factor costs a third of a second a
# time; the suite creates users by the dozen and tests the format, not the cost.
os.environ["ORDERING_PBKDF2_ITERATIONS"] = "1000"
# The suite must not inherit the laptop's mail server, OTP inbox, app address
# or Redis: a configured mail server alone flips what the sign-in page offers
# (audit F-34, 25 Oct 2026). Blank here, set by the tests that need them.
for _name in ("ORDERING_SMTP_HOST", "ORDERING_SMTP_FROM", "ORDERING_APP_URL", "ORDERING_OTP_IMAP_HOST", "ORDERING_REDIS_URL"):
    os.environ[_name] = ""
# The signing key too: outside dev mode the shipped default stops the app's
# start-up (audit F-31), and the suite must neither run on that default nor
# lean on dev mode being on. Made up, and longer than HMAC-SHA256's 32 bytes.
os.environ["ORDERING_JWT_SECRET"] = "test-suite-signing-key-made-up-for-the-tests-only-0123456789"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.automation import otp as otp_module  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.login_guard import guard  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db import base as db_base  # noqa: E402
from app.db.base import Base, SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402

#: The configured admin the fixtures sign in as — made up, and hashed once. A
#: fixture pins the settings to it for the test, whatever the local ``.env``
#: or shell say, so no test depends on dev mode being on.
TEST_ADMIN = {"username": "admin", "password": "test-admin-password-1"}
_TEST_ADMIN_HASH = hash_password(TEST_ADMIN["password"], iterations=1000)


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    """Reset the schema before every test.

    ``init_db`` carries a retired business block from the environment into the
    Address Book; the developer's own ``.env`` and shell must never seed a test
    database, so that source is blanked here (tests that need it set their own).
    """
    monkeypatch.setattr(db_base, "RETIRED_ENV_FILE", None)
    # Nor may it add OTP inboxes to a test (ORDERING_OTP_IMAP_USER_2 …).
    monkeypatch.setattr(otp_module, "ENV_FILE", None)
    for key in db_base.RETIRED_BUSINESS_KEYS:
        monkeypatch.delenv(f"ORDERING_{key.upper()}", raising=False)
    # The fixtures only ever drop the temp file they made. If an app module
    # was imported before this file set the address (a script importing a
    # test module, say), the engine would be bound to the real database —
    # stop here, loudly, rather than drop it. The engine refuses such a drop
    # as well (app.db.base.refuse_drop_unless_throwaway); this names the cause.
    # Parsed on both sides: a rendered URL re-quotes the password and sorts the
    # query, so comparing strings would fail honest URLs — and pytest's assert
    # rewriting would print the raw one, password included. A URL's repr masks it.
    assert engine.url == make_url(_TEST_DB_URL), (
        f"the test engine is bound to {engine.url}, not the test database — "
        "an app module was imported before tests/conftest.py ran"
    )
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def pinned_admin(monkeypatch) -> dict:
    """The configured admin as a known account for this test, restored after:
    a hashed password, dev mode off, and the throttle's memory cleared."""
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_username", TEST_ADMIN["username"])
    monkeypatch.setattr(settings, "admin_password_hash", _TEST_ADMIN_HASH)
    monkeypatch.setattr(settings, "admin_password", "")
    monkeypatch.setattr(settings, "dev_mode", False)
    guard.reset()
    return dict(TEST_ADMIN)


@pytest.fixture
def admin_token(client, pinned_admin) -> str:
    """A fresh sign-in as the configured admin."""
    resp = client.post("/auth/login", json=pinned_admin)
    assert resp.status_code == 200, f"login failed: {resp.text}"
    return resp.json()["access_token"]


@pytest.fixture
def auth_headers(admin_token) -> dict:
    """Bearer header for the configured admin user.

    Every route except /, /health and /auth/login requires a token, so most API
    tests need this.
    """
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(autouse=True)
def _kept_page_text_in_the_tests_own_folder(monkeypatch, tmp_path):
    """The read-back keeps an out-for-delivery page's text for mapping
    (app.automation.sync._keep_page_text); in the suite it goes to the test's
    own folder, never into the app's real backend/diagnostics (found 26 Oct
    2026: eight test files had landed there)."""
    from app.automation import sync as sync_module

    monkeypatch.setattr(sync_module, "DIAGNOSTICS_DIR", str(tmp_path / "diagnostics"))
