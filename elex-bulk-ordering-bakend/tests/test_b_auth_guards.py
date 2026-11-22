"""Three guards on the way in (audit of 25 Oct 2026, F-42 and F-31).

A token a sign-in issued is refused once the user row it was minted for is
gone, instead of being trusted on its role claim; tokens the server mints
itself, with no row behind them, are judged as before. The saved IMAP
password goes only to the saved host and user. And outside dev mode the app
does not start on the shipped signing key, before it has changed anything,
while the owner's laptop, in dev mode, keeps starting."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import addresses as addresses_api
from app.api import settings as settings_api
from app.api.settings import OtpInboxTestResult
from app.config import Settings, get_settings
from app.core import app_settings, security, users
from app.core.security import Principal, create_access_token, decode_access_token, refuse_default_secret
from app.db.models import Job, JobStatus, JobType, User, UserRole

ADDRESS = {"label": "hq", "name": "HQ", "line1": "1 Main Road", "city": "Lucknow", "state": "Uttar Pradesh", "pincode": "226001"}


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _row(db, email: str, role: UserRole, *, id: int | None = None) -> User:
    user = User(email=email, name="", password_hash="", role=role, active=True)
    if id is not None:
        user.id = id
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def api() -> TestClient:
    """The address book: anyone signed in reads it, only an admin adds to it."""
    application = FastAPI()
    application.include_router(addresses_api.router)
    return TestClient(application)


# --- 1. a token minted for a row is that row's or nothing ------------------------------
def test_a_token_whose_row_is_gone_is_refused_not_trusted_on_its_role(db, api):
    """Rows are never deleted, so a missing one means the database was wiped
    or restored from an older copy; the token's "admin" claim must not
    outlive the row it was minted for."""
    boss = _row(db, "boss@test.com", UserRole.admin)
    token = users.issue_token(boss)
    claims = decode_access_token(token)
    assert (claims["uid"], claims["role"]) == (boss.id, "admin")
    assert users.lookup_principal(claims).role == "admin"  # while the row stands
    assert api.post("/addresses", json=ADDRESS, headers=_bearer(token)).status_code == 201

    db.delete(boss)
    db.commit()

    assert users.lookup_principal(claims) is None
    assert api.get("/addresses", headers=_bearer(token)).status_code == 401
    assert api.post("/addresses", json=ADDRESS, headers=_bearer(token)).status_code == 401


def test_a_token_whose_id_was_handed_to_someone_else_is_refused(db):
    """A wiped database hands ids out again. The new owner of the id — an
    admin, say, who has not changed a password either — must not inherit an
    old operator's token: the token's subject has to be the row's email."""
    ops = _row(db, "ops@test.com", UserRole.operator)
    claims = decode_access_token(users.issue_token(ops))
    reused_id = ops.id
    db.delete(ops)
    db.commit()
    _row(db, "boss@test.com", UserRole.admin, id=reused_id)

    assert users.lookup_principal(claims) is None


def test_a_reset_link_whose_id_was_handed_to_someone_else_is_refused(db):
    """The same for a password-reset link: mailed for one address, it must not
    open the account that holds its id after a restore."""
    ops = _row(db, "ops@test.com", UserRole.operator)
    link_token = users.reset_token(ops)
    assert users.user_for_reset_token(db, link_token) is not None  # good while the row is the same
    reused_id = ops.id
    db.delete(ops)
    db.commit()
    _row(db, "boss@test.com", UserRole.admin, id=reused_id)

    assert users.user_for_reset_token(db, link_token) is None


def test_a_uid_claim_that_is_not_a_row_id_is_refused(db):
    """Only a whole number names a row. A uid claim that is there but names
    none is refused, never read as "no uid" — the subject here has a row."""
    ops = _row(db, "ops@test.com", UserRole.operator)
    for uid in (str(ops.id), float(ops.id), True, None):
        claims = decode_access_token(create_access_token(ops.email, role="admin", extra={"uid": uid}))
        assert users.lookup_principal(claims) is None, uid


def test_a_token_without_a_uid_is_judged_as_before(db, api, monkeypatch):
    """What the server mints itself carries no uid: a worker, a test (about
    40 test files sign their requests this way), the configured admin before
    its row existed. Trusted on its claims when its subject has no row; the
    row's when it has one."""
    monkeypatch.setattr(get_settings(), "admin_username", "owner@example.com")  # no row for this name yet

    def principal(subject: str, **options) -> Principal | None:
        return users.lookup_principal(decode_access_token(create_access_token(subject, **options)))

    assert principal("tester", role="admin") == Principal(id=None, email="tester", name="", role="admin")
    assert principal("tester") == Principal(id=None, email="tester", name="", role="operator")
    assert principal("owner@example.com") == Principal(
        id=None, email="owner@example.com", name="", role="admin", is_env_admin=True
    )
    ops = _row(db, "ops@test.com", UserRole.operator)
    from_row = principal("ops@test.com", role="admin")
    assert (from_row.id, from_row.role) == (ops.id, "operator")  # the row's role, not the claim's

    as_tester = _bearer(create_access_token("tester", role="admin"))
    assert api.post("/addresses", json=ADDRESS, headers=as_tester).status_code == 201


# --- 2. the saved IMAP password stays with the saved host and user ------------------------
SAVED_PASSWORD = "saved-app-password-never-sent-elsewhere"
SAVED_INBOX = {"otp_imap_host": "imap.gmail.com", "otp_imap_user": "main@gmail.com", "otp_imap_password": SAVED_PASSWORD}


@pytest.fixture
def probes(monkeypatch) -> list[tuple]:
    """Every connection the inbox test would open, recorded instead of opened."""
    calls: list[tuple] = []

    def fake_probe(host, port, user, password, mailbox, use_ssl):
        calls.append((host, port, user, password, mailbox, use_ssl))
        return OtpInboxTestResult(ok=True, message="probed")

    monkeypatch.setattr(settings_api, "_probe_inbox", fake_probe)
    return calls


@pytest.fixture
def inbox_api(db, monkeypatch) -> TestClient:
    """An admin's client, with an inbox saved from the Settings page."""
    monkeypatch.setattr(get_settings(), "otp_imap_password", "")  # nothing from the environment
    app_settings.set_overrides(db, SAVED_INBOX)
    application = FastAPI()
    application.include_router(settings_api.router)
    return TestClient(application, headers=_bearer(create_access_token("tester", role="admin")))


@pytest.mark.parametrize(
    "body",
    [
        {"host": "imap.attacker.example"},
        {"user": "someone-else@gmail.com"},
        {"host": "imap.attacker.example", "user": "main@gmail.com", "use_ssl": False, "port": 143},
        {"use_ssl": False, "port": 143},
    ],
    ids=["foreign-host", "foreign-user", "foreign-host-plaintext", "saved-host-ssl-off"],
)
def test_a_foreign_host_or_user_without_a_password_never_reaches_the_probe(inbox_api, probes, body):
    response = inbox_api.post("/settings/otp-inbox/test", json=body)

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is False
    assert "saved password" in result["message"] and "type" in result["message"]
    assert probes == []  # nothing was connected to
    assert SAVED_PASSWORD not in response.text


def test_a_foreign_host_with_its_own_password_is_tested_with_that_password(inbox_api, probes):
    response = inbox_api.post("/settings/otp-inbox/test", json={"host": "imap.other.example", "password": "typed-here"})

    assert response.json()["ok"] is True
    assert probes == [("imap.other.example", 993, "main@gmail.com", "typed-here", "INBOX", True)]


@pytest.mark.parametrize(
    "body",
    [None, {}, {"host": "IMAP.Gmail.com"}, {"host": "imap.gmail.com", "user": "main@gmail.com"}, {"mailbox": "Spam"}],
    ids=["no-body", "empty", "host-in-capitals", "same-host-and-user", "other-mailbox"],
)
def test_the_saved_inbox_is_still_tested_with_the_saved_password(inbox_api, probes, body):
    """The Settings page sends an empty body; the saved host and user, however
    spelt, still get the saved password."""
    response = inbox_api.post("/settings/otp-inbox/test", **({} if body is None else {"json": body}))

    assert response.json()["ok"] is True
    assert len(probes) == 1
    host, port, user, password, mailbox, use_ssl = probes[0]
    assert (host.lower(), port, user, password, use_ssl) == ("imap.gmail.com", 993, "main@gmail.com", SAVED_PASSWORD, True)
    assert mailbox == ((body or {}).get("mailbox") or "INBOX")


@pytest.mark.parametrize(
    "body",
    [
        {"otp_imap_host": "imap.attacker.example"},
        {"otp_imap_user": "someone-else@gmail.com"},
        {"otp_imap_use_ssl": False},
        {"otp_imap_host": "imap.attacker.example", "otp_imap_port": 143, "otp_imap_use_ssl": False},
    ],
    ids=["new-host", "new-user", "ssl-off", "new-host-plaintext"],
)
def test_saving_a_new_place_for_the_inbox_needs_its_password(inbox_api, db, body):
    """Saving a new host, user or a clear-text connection without a new
    password would hand the saved one to the new place at the next login run."""
    response = inbox_api.put("/settings", json=body)

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "otp_imap_password"]
    saved = app_settings.get_effective(db)
    assert (saved["otp_imap_host"], saved["otp_imap_user"]) == ("imap.gmail.com", "main@gmail.com")
    assert SAVED_PASSWORD not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"otp_imap_host": "imap.other.example", "otp_imap_password": "typed-here"},
        {"otp_imap_host": "IMAP.GMAIL.COM"},
        {"otp_imap_mailbox": "Spam"},
        {"otp_imap_port": 993},
        {"otp_imap_host": ""},
    ],
    ids=["new-host-with-password", "same-host-in-capitals", "other-mailbox", "same-port", "host-cleared"],
)
def test_other_inbox_saves_go_through_as_before(inbox_api, body):
    assert inbox_api.put("/settings", json=body).status_code == 200


def test_with_no_saved_password_the_answer_is_the_one_it_was(inbox_api, probes, db):
    app_settings.set_overrides(db, {"otp_imap_password": ""})

    result = inbox_api.post("/settings/otp-inbox/test", json={"host": "imap.other.example"}).json()

    assert result["ok"] is False and "not configured yet" in result["message"]
    assert probes == []


# --- 3. no start on the shipped signing key outside dev mode -----------------------------
SHIPPED_KEY = Settings.model_fields["jwt_secret"].default


def test_the_suite_runs_on_a_pinned_key_of_its_own():
    """tests/conftest.py pins ORDERING_JWT_SECRET, so no test signs with a
    shipped default or needs dev mode to start the app."""
    secret = get_settings().jwt_secret
    assert secret not in security._DEFAULT_SECRETS
    assert len(secret.encode("utf-8")) >= 32
    assert SHIPPED_KEY in security._DEFAULT_SECRETS  # what config.py ships is one of the refused


@pytest.mark.parametrize("secret", security._DEFAULT_SECRETS)
def test_outside_dev_mode_a_shipped_key_is_refused(monkeypatch, secret):
    monkeypatch.setattr(get_settings(), "jwt_secret", secret)
    monkeypatch.setattr(get_settings(), "dev_mode", False)

    with pytest.raises(RuntimeError, match="ORDERING_JWT_SECRET"):
        refuse_default_secret()


def test_a_key_of_its_own_starts_without_dev_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "jwt_secret", "x" * 48)
    monkeypatch.setattr(get_settings(), "dev_mode", False)

    assert refuse_default_secret() is None


@pytest.mark.parametrize("secret", security._DEFAULT_SECRETS)
def test_the_owners_laptop_in_dev_mode_keeps_starting_on_the_shipped_key(db, monkeypatch, secret):
    """The laptop runs with ORDERING_DEV_MODE=true and no key of its own: the
    whole start-up runs, as before, and the app answers."""
    from app import main as main_module

    monkeypatch.setattr(get_settings(), "jwt_secret", secret)
    monkeypatch.setattr(get_settings(), "dev_mode", True)

    assert refuse_default_secret() is None
    with TestClient(main_module.app) as client:
        assert client.get("/health").status_code == 200


def test_a_refused_start_changes_nothing(db, monkeypatch):
    """The check comes first in the start-up: no run flag cleared, no schema
    touched, no orphaned job settled — the app just does not start."""
    from app import main as main_module

    monkeypatch.setattr(get_settings(), "jwt_secret", SHIPPED_KEY)
    monkeypatch.setattr(get_settings(), "dev_mode", False)
    orphan = Job(type=JobType.sync, status=JobStatus.running, total_items=1)
    db.add(orphan)
    db.commit()
    ran: list[str] = []
    monkeypatch.setattr(main_module, "allow_runs", lambda: ran.append("allow_runs"))
    monkeypatch.setattr(main_module, "init_db", lambda: ran.append("init_db"))

    with pytest.raises(RuntimeError, match="ORDERING_JWT_SECRET"):
        with TestClient(main_module.app):
            pass

    assert ran == []
    db.expire_all()
    assert db.get(Job, orphan.id).status == JobStatus.running  # the start-up's recovery never ran
