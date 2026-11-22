"""Users and roles (spec FR-055, FR-056, FR-058, FR-059; C-7 as recommended:
Admin and Operator; C-13: a reset link by mail). Sign in by email; the
configured admin is the bootstrap account whose environment credential always
works; an admin adds users with a temporary password, resets one, and takes a
user out; an operator reads everything and runs things but changes no setup;
a password change or reset ends that user's other sessions."""

from __future__ import annotations

import asyncio
import ssl

import pytest
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import accounts as accounts_api
from app.api import addresses as addresses_api
from app.api import alerts as alerts_api
from app.api import auth as auth_api
from app.api import gsts as gsts_api
from app.api import orders as orders_api
from app.api import settings as settings_api
from app.api import unit_limits as unit_limits_api
from app.api import users as users_api
from app.config import get_settings
from app.core import alerts as alert_engine
from app.core import mailer, users
from app.core.login_guard import guard
from app.core.security import PASSWORD_CHANGE_REQUIRED, create_access_token, decode_access_token
from app.db.models import Account, AccountStatus, User, UserRole
from app.schemas.auth import ForgotPasswordIn

TEST_SECRET = "test-secret-long-enough-for-hmac-sha256"
ADDRESS = {"label": "hq", "name": "HQ", "line1": "1 Main Road", "city": "Lucknow", "state": "Uttar Pradesh", "pincode": "226001"}


@pytest.fixture
def env_admin(monkeypatch) -> dict:
    """Pin the configured admin so the suite never depends on a local .env,
    and seed its row the way start-up does."""
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_username", "Owner@Example.com")
    monkeypatch.setattr(settings, "admin_password", "owner-pass-123")
    monkeypatch.setattr(settings, "admin_password_hash", "")
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(settings, "jwt_secret", TEST_SECRET)
    guard.reset()
    users.seed_env_admin()
    return {"username": "owner@example.com", "password": "owner-pass-123"}


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    for module in (auth_api, users_api, addresses_api, orders_api, gsts_api, unit_limits_api, settings_api, alerts_api, accounts_api):
        application.include_router(module.router)
    return TestClient(application)


def _login(api: TestClient, email: str, password: str) -> dict:
    response = api.post("/auth/login", json={"username": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _admin(api: TestClient, env_admin: dict) -> dict:
    return _bearer(_login(api, env_admin["username"], env_admin["password"])["access_token"])


def _operator(api: TestClient, admin: dict, email: str = "ops@test.com", password: str = "temporary-pass-1") -> dict:
    created = api.post("/users", json={"email": email, "name": "Ops", "role": "operator", "temporary_password": password}, headers=admin)
    assert created.status_code == 201, created.text
    return created.json()


def _settled_operator(api: TestClient, admin: dict, email: str = "ops@test.com") -> tuple[dict, dict]:
    """An operator past the temporary password: (their row, their headers)."""
    row = _operator(api, admin, email=email)
    temp = _bearer(_login(api, email, "temporary-pass-1")["access_token"])
    changed = api.post("/auth/change-password", json={"current_password": "temporary-pass-1", "new_password": "a-proper-password-1"}, headers=temp)
    assert changed.status_code == 200, changed.text
    return row, _bearer(changed.json()["access_token"])


# --- the configured admin --------------------------------------------------------
def test_start_up_seeds_the_configured_admin_once(api, db, env_admin):
    """The row exists before any sign-in, and seeding again adds nothing."""
    rows = db.query(User).filter(User.email == "owner@example.com").all()
    assert len(rows) == 1 and rows[0].role == UserRole.admin and rows[0].password_hash == ""
    users.seed_env_admin()
    assert db.query(User).filter(User.email == "owner@example.com").count() == 1


def test_the_configured_admin_signs_in_with_the_environment_password(api, db, env_admin):
    body = _login(api, "OWNER@example.com ", env_admin["password"])  # case and spaces do not matter
    claims = decode_access_token(body["access_token"])
    assert claims["role"] == "admin"
    assert body["must_change_password"] is False

    me = api.get("/auth/me", headers=_bearer(body["access_token"])).json()
    assert (me["email"], me["role"], me["name"]) == ("owner@example.com", "admin", users.ENV_ADMIN_NAME)
    assert me["is_env_admin"] is True
    assert me["expires_at"] is not None

    row = db.query(User).filter(User.email == "owner@example.com").one()
    assert (row.role, row.password_hash, row.active) == (UserRole.admin, "", True)
    assert claims["uid"] == row.id


def test_the_environment_password_keeps_working_after_the_admin_sets_their_own(api, db, env_admin):
    admin = _admin(api, env_admin)
    changed = api.post(
        "/auth/change-password",
        json={"current_password": env_admin["password"], "new_password": "my-own-password-9"},
        headers=admin,
    )
    assert changed.status_code == 200, changed.text

    _login(api, env_admin["username"], "my-own-password-9")
    _login(api, env_admin["username"], env_admin["password"])  # the way back in


def test_the_configured_admins_row_is_made_an_admin_again_at_start_up(db, env_admin):
    """Pointing the setting at an existing operator row, or at a deactivated
    one, means "this is the admin": the row's password and name are kept."""
    row = db.query(User).filter(User.email == "owner@example.com").one()
    row.role = UserRole.operator
    row.active = False
    row.name = "Kept"
    db.commit()
    users.seed_env_admin()
    db.refresh(row)
    assert (row.role, row.active, row.name) == (UserRole.admin, True, "Kept")


# --- sign-in -----------------------------------------------------------------------
def test_sign_in_says_nothing_about_which_half_was_wrong(api, env_admin):
    wrong_password = api.post("/auth/login", json={"username": env_admin["username"], "password": "nope"})
    unknown_email = api.post("/auth/login", json={"username": "nobody@test.com", "password": env_admin["password"]})
    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["detail"] == unknown_email.json()["detail"]


def test_sign_in_verifies_exactly_one_hash_whatever_the_email(api, env_admin, monkeypatch):
    """An unknown email must cost the same as a wrong password: one PBKDF2
    verification either way, never zero (spec FR-056)."""
    admin = _admin(api, env_admin)
    _operator(api, admin)
    calls: list[str] = []
    real = users.verify_password

    def counting(password: str, stored: str) -> bool:
        calls.append(stored)
        return real(password, stored)

    monkeypatch.setattr(users, "verify_password", counting)
    api.post("/auth/login", json={"username": "nobody@test.com", "password": "whatever-it-is-1"})
    assert len(calls) == 1
    calls.clear()
    api.post("/auth/login", json={"username": "ops@test.com", "password": "wrong-password-1"})
    assert len(calls) == 1
    calls.clear()
    _login(api, "ops@test.com", "temporary-pass-1")
    assert len(calls) == 1


# --- an admin adds an operator ----------------------------------------------------
def test_an_admin_adds_an_operator_who_must_change_the_temporary_password(api, env_admin):
    admin = _admin(api, env_admin)
    created = _operator(api, admin, email="Ops@Test.com")
    assert created["email"] == "ops@test.com"
    assert created["must_change_password"] is True
    assert created["is_env_admin"] is False

    first = _login(api, "ops@test.com", "temporary-pass-1")
    assert first.get("must_change_password") is True
    ops = _bearer(first["access_token"])
    assert api.get("/auth/me", headers=ops).json()["must_change_password"] is True

    wrong = api.post("/auth/change-password", json={"current_password": "nope", "new_password": "a-proper-password-1"}, headers=ops)
    assert wrong.status_code == 403  # the session stays; the client must not sign out
    weak = api.post("/auth/change-password", json={"current_password": "temporary-pass-1", "new_password": "short"}, headers=ops)
    assert weak.status_code == 422
    same = api.post("/auth/change-password", json={"current_password": "temporary-pass-1", "new_password": "OPS@test.com"}, headers=ops)
    assert same.status_code == 422

    good = api.post("/auth/change-password", json={"current_password": "temporary-pass-1", "new_password": "a-proper-password-1"}, headers=ops)
    assert good.status_code == 200, good.text
    assert good.json()["must_change_password"] is False
    assert good.json()["access_token"]  # a fresh token: the change ended the old session

    _login(api, "ops@test.com", "a-proper-password-1")
    assert api.post("/auth/login", json={"username": "ops@test.com", "password": "temporary-pass-1"}).status_code == 401


def test_a_temporary_password_opens_only_me_and_change_password(api, env_admin):
    """The API enforces it too, not only the UI's redirect: everything else
    answers 403 with a code the client can tell apart from "not allowed"."""
    admin = _admin(api, env_admin)
    _operator(api, admin)
    ops = _bearer(_login(api, "ops@test.com", "temporary-pass-1")["access_token"])
    assert api.get("/auth/me", headers=ops).status_code == 200
    refused = api.get("/addresses", headers=ops)
    assert refused.status_code == 403
    assert refused.json()["detail"] == PASSWORD_CHANGE_REQUIRED
    changed = api.post("/auth/change-password", json={"current_password": "temporary-pass-1", "new_password": "a-proper-password-1"}, headers=ops)
    assert api.get("/addresses", headers=_bearer(changed.json()["access_token"])).status_code == 200


@pytest.mark.parametrize("bad", ["password12", "Password123", "aaaaaaaaaaaa", "1234567890", "ababababab"])
def test_passwords_on_every_guessing_list_are_refused(api, env_admin, bad):
    admin = _admin(api, env_admin)
    refused = api.post("/users", json={"email": "x@test.com", "role": "operator", "temporary_password": bad}, headers=admin)
    assert refused.status_code == 422, bad


def test_a_duplicate_email_is_a_409_and_a_weak_temporary_password_a_422(api, env_admin):
    admin = _admin(api, env_admin)
    _operator(api, admin)
    again = api.post("/users", json={"email": "OPS@test.com", "role": "operator", "temporary_password": "temporary-pass-1"}, headers=admin)
    assert again.status_code == 409
    weak = api.post("/users", json={"email": "x@test.com", "role": "operator", "temporary_password": "short"}, headers=admin)
    assert weak.status_code == 422
    assert api.post("/users", json={"email": "not-an-email", "role": "operator", "temporary_password": "temporary-pass-1"}, headers=admin).status_code == 422


def test_two_admins_adding_the_same_email_at_once(api, env_admin, monkeypatch):
    """The pre-check can miss a row inserted a moment earlier; the unique
    index then decides, and the loser gets the same 409, not a 500."""
    admin = _admin(api, env_admin)
    _operator(api, admin)
    monkeypatch.setattr(users_api, "_email_taken", lambda db, email: False)
    again = api.post("/users", json={"email": "ops@test.com", "role": "operator", "temporary_password": "temporary-pass-1"}, headers=admin)
    assert again.status_code == 409
    assert api.get("/users", headers=admin).status_code == 200  # the session survived the rollback


# --- what each role may do ---------------------------------------------------------
def test_an_operator_reads_the_address_book_but_cannot_change_it(api, env_admin):
    admin = _admin(api, env_admin)
    _, ops = _settled_operator(api, admin)

    assert api.get("/addresses", headers=ops).status_code == 200
    refused = api.post("/addresses", json=ADDRESS, headers=ops)
    assert refused.status_code == 403
    assert "admin" in refused.json()["detail"].lower()
    assert api.get("/users", headers=ops).status_code == 403

    allowed = api.post("/addresses", json=ADDRESS, headers=admin)
    assert allowed.status_code == 201, allowed.text
    assert api.get("/users").status_code == 401  # nobody at all


WRITE_ROUTES = [
    ("post", "/gsts", {"label": "g", "gstin": "27AAAAA0000A1Z5", "business_name": "G"}),
    ("patch", "/gsts/1", {"label": "h"}),
    ("delete", "/gsts/1", None),
    ("post", "/unit-limits", {"scope": "account", "value": "x@test.com", "max_units": 3}),
    ("patch", "/unit-limits/1", {"max_units": 4}),
    ("delete", "/unit-limits/1", None),
    ("put", "/settings", {}),
    ("post", "/settings/otp-inbox/test", None),
    # An account's own GST and billing phone (spec C-7): setup, though the
    # rest of an account is the operator's.
    ("post", "/accounts/gst", {"gst_id": None, "account_ids": [1]}),
    ("patch", "/accounts/1", {"gst_id": None}),
    ("patch", "/accounts/1", {"billing_phone": "9811111111"}),
]


@pytest.mark.parametrize("method,path,body", WRITE_ROUTES)
def test_setup_writes_are_for_admins_only(api, env_admin, method, path, body):
    """Every setup write answers an operator 403 and an admin anything but
    (spec FR-058): a missing row is a 404, a bad body a 422, never a 403."""
    admin = _admin(api, env_admin)
    _, ops = _settled_operator(api, admin)
    kwargs = {"json": body} if body is not None else {}
    refused = getattr(api, method)(path, headers=ops, **kwargs)
    assert refused.status_code == 403, (method, path, refused.text)
    allowed = getattr(api, method)(path, headers=admin, **kwargs)
    assert allowed.status_code != 403, (method, path, allowed.text)


def test_an_operator_arranges_accounts_but_sets_no_gst_or_billing_phone(api, db, env_admin):
    """C-7: an account's own GST and billing phone are setup, the admin's;
    its phone, status and tag stay the operator's, on the same route."""
    admin = _admin(api, env_admin)
    _, ops = _settled_operator(api, admin)
    account = Account(email="buyer@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()

    arranged = api.patch(f"/accounts/{account.id}", json={"phone": "9000000001", "status": "logged_out"}, headers=ops)
    assert arranged.status_code == 200, arranged.text
    refused = api.patch(f"/accounts/{account.id}", json={"billing_phone": "9811111111"}, headers=ops)
    assert refused.status_code == 403
    assert "admin" in refused.json()["detail"].lower()
    allowed = api.patch(f"/accounts/{account.id}", json={"billing_phone": "9811111111"}, headers=admin)
    assert allowed.status_code == 200, allowed.text
    assert (allowed.json()["phone"], allowed.json()["billing_phone"]) == ("9000000001", "9811111111")


def test_a_deactivated_user_is_refused_on_the_very_next_request(api, env_admin):
    admin = _admin(api, env_admin)
    created, ops = _settled_operator(api, admin)
    assert api.get("/addresses", headers=ops).status_code == 200

    off = api.patch(f"/users/{created['id']}", json={"active": False}, headers=admin)
    assert off.status_code == 200 and off.json()["active"] is False

    assert api.get("/addresses", headers=ops).status_code == 401  # the token they still hold
    assert api.post("/auth/login", json={"username": "ops@test.com", "password": "a-proper-password-1"}).status_code == 401

    on = api.patch(f"/users/{created['id']}", json={"active": True}, headers=admin)
    assert on.json()["active"] is True
    assert api.get("/addresses", headers=ops).status_code == 200


def test_a_role_change_takes_effect_on_the_next_request(api, env_admin):
    admin = _admin(api, env_admin)
    created, ops = _settled_operator(api, admin)
    assert api.post("/addresses", json=ADDRESS, headers=ops).status_code == 403

    promoted = api.patch(f"/users/{created['id']}", json={"role": "admin"}, headers=admin)
    assert promoted.json()["role"] == "admin"
    assert api.post("/addresses", json=ADDRESS, headers=ops).status_code == 201  # same token, new role


def test_an_admin_cannot_lock_everybody_out(api, db, env_admin):
    admin = _admin(api, env_admin)
    me_id = db.query(User).filter(User.email == "owner@example.com").one().id
    assert api.patch(f"/users/{me_id}", json={"active": False}, headers=admin).status_code == 409
    assert api.patch(f"/users/{me_id}", json={"role": "operator"}, headers=admin).status_code == 409

    # A second admin cannot demote or deactivate the configured admin either...
    other = api.post("/users", json={"email": "second@test.com", "role": "admin", "temporary_password": "temporary-pass-1"}, headers=admin)
    temp = _bearer(_login(api, "second@test.com", "temporary-pass-1")["access_token"])
    second = _bearer(api.post("/auth/change-password", json={"current_password": "temporary-pass-1", "new_password": "second-own-pass-1"}, headers=temp).json()["access_token"])
    assert api.patch(f"/users/{me_id}", json={"role": "operator"}, headers=second).status_code == 409
    assert api.patch(f"/users/{me_id}", json={"active": False}, headers=second).status_code == 409
    # ...nor reset their password: the environment's password is their reset.
    assert api.post(f"/users/{me_id}/reset-password", json={"temporary_password": "temporary-pass-2"}, headers=second).status_code == 409
    # ...but can rename them, confirm them as an admin (a no-op, not a refusal),
    # and the configured admin can take the second one out.
    assert api.patch(f"/users/{me_id}", json={"name": "The owner"}, headers=second).json()["name"] == "The owner"
    assert api.patch(f"/users/{me_id}", json={"role": "admin", "active": True}, headers=second).status_code == 200
    assert api.patch(f"/users/{other.json()['id']}", json={"active": False}, headers=admin).json()["active"] is False


def test_an_admin_resets_a_forgotten_password(api, env_admin):
    admin = _admin(api, env_admin)
    created, ops = _settled_operator(api, admin)

    reset = api.post(f"/users/{created['id']}/reset-password", json={"temporary_password": "temporary-pass-2"}, headers=admin)
    assert reset.status_code == 200 and reset.json()["must_change_password"] is True

    assert api.post("/auth/login", json={"username": "ops@test.com", "password": "a-proper-password-1"}).status_code == 401
    assert _login(api, "ops@test.com", "temporary-pass-2")["must_change_password"] is True
    assert api.post(f"/users/{created['id']}/reset-password", json={"temporary_password": "short"}, headers=admin).status_code == 422
    # The session they had before the reset is over everywhere.
    assert api.get("/addresses", headers=ops).status_code == 401
    assert api.get("/auth/me", headers=ops).status_code == 401


def test_changing_your_own_password_ends_your_other_sessions(api, env_admin):
    admin = _admin(api, env_admin)
    _, ops = _settled_operator(api, admin)
    elsewhere = _bearer(_login(api, "ops@test.com", "a-proper-password-1")["access_token"])
    assert api.get("/addresses", headers=elsewhere).status_code == 200

    changed = api.post("/auth/change-password", json={"current_password": "a-proper-password-1", "new_password": "newer-password-22"}, headers=ops)
    assert changed.status_code == 200, changed.text
    fresh = _bearer(changed.json()["access_token"])
    assert api.get("/addresses", headers=fresh).status_code == 200  # the token handed back works
    assert api.get("/addresses", headers=ops).status_code == 401  # the one used for the change is spent
    assert api.get("/addresses", headers=elsewhere).status_code == 401  # and so is the other tab's


def test_users_are_listed_newest_last_with_the_configured_admin_marked(api, env_admin):
    admin = _admin(api, env_admin)
    _operator(api, admin)
    listed = api.get("/users", headers=admin).json()
    by_email = {u["email"]: u for u in listed}
    assert by_email["owner@example.com"]["is_env_admin"] is True
    assert by_email["ops@test.com"]["is_env_admin"] is False
    assert "password_hash" not in listed[0]


# --- who did what -------------------------------------------------------------------
def test_an_alert_closed_by_hand_records_who_closed_it(api, db, env_admin):
    admin = _admin(api, env_admin)
    alert_engine.raise_alert(db, key="t:1", kind="delivery_overdue", message="late")
    db.commit()
    alert_id = db.scalar(select(alert_engine.Alert.id).where(alert_engine.Alert.key == "t:1"))
    closed = api.post(f"/alerts/{alert_id}/resolve", json={"note": "seen"}, headers=admin)
    assert closed.status_code == 200, closed.text
    assert closed.json()["resolved_by"] == "user"
    assert closed.json()["resolved_by_user"] == "owner@example.com"
    reopened = api.post(f"/alerts/{alert_id}/reopen", headers=admin)
    assert reopened.json()["resolved_by_user"] is None


# --- tokens the server mints itself --------------------------------------------------
def test_a_token_without_a_user_row_is_trusted_on_its_claims(api, env_admin):
    as_operator = _bearer(create_access_token("tester"))
    assert api.get("/addresses", headers=as_operator).status_code == 200
    assert api.post("/addresses", json=ADDRESS, headers=as_operator).status_code == 403

    as_admin = _bearer(create_access_token("tester", role="admin"))
    assert api.post("/addresses", json=ADDRESS, headers=as_admin).status_code == 201

    as_env_admin = _bearer(create_access_token("owner@example.com"))  # no role claim: the configured admin
    assert api.get("/users", headers=as_env_admin).status_code == 200


# --- the saved ledger view is per user ---------------------------------------------
def test_the_saved_ledger_view_is_per_user(api, env_admin):
    admin = _admin(api, env_admin)
    _, ops = _settled_operator(api, admin)

    saved = api.put("/orders/view", json={"columns": ["order_id"], "filters": {}}, headers=admin)
    assert saved.status_code == 200, saved.text
    assert api.get("/orders/view", headers=admin).json()["columns"] == ["order_id"]
    assert api.get("/orders/view", headers=ops).json() is None

    api.delete("/orders/view", headers=ops)  # nothing of theirs to clear; nothing of the admin's goes
    assert api.get("/orders/view", headers=admin).json()["columns"] == ["order_id"]


# --- the reset link by mail (spec FR-059 / C-13) -------------------------------------
@pytest.fixture
def mail(monkeypatch) -> list[tuple[str, str, str]]:
    """A configured mail server whose mails land in a list instead."""
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(settings, "smtp_from", "ordering@example.com")
    monkeypatch.setattr(settings, "app_url", "https://orders.example.com/")
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(mailer, "send_mail", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def _token_in(body: str) -> str:
    link = next(line for line in body.splitlines() if line.startswith("https://orders.example.com/reset-password#token="))
    return link.split("token=", 1)[1]


def test_a_reset_link_sets_a_new_password_and_works_once(api, env_admin, mail):
    admin = _admin(api, env_admin)
    _, ops = _settled_operator(api, admin)

    asked = api.post("/auth/forgot-password", json={"email": "OPS@test.com"})
    assert asked.status_code == 202
    assert len(mail) == 1 and mail[0][0] == "ops@test.com"
    token = _token_in(mail[0][2])

    weak = api.post("/auth/reset-password", json={"token": token, "new_password": "short"})
    assert weak.status_code == 422
    done = api.post("/auth/reset-password", json={"token": token, "new_password": "from-the-mail-link-1"})
    assert done.status_code == 200, done.text
    assert done.json()["must_change_password"] is False
    _login(api, "ops@test.com", "from-the-mail-link-1")
    assert api.post("/auth/login", json={"username": "ops@test.com", "password": "a-proper-password-1"}).status_code == 401
    assert api.get("/addresses", headers=ops).status_code == 401  # the old session ended

    again = api.post("/auth/reset-password", json={"token": token, "new_password": "once-more-with-feeling-1"})
    assert again.status_code == 400  # the link was spent by the reset
    forged = api.post("/auth/reset-password", json={"token": create_access_token("ops@test.com"), "new_password": "an-access-token-is-not-1"})
    assert forged.status_code == 400


def test_the_reset_answer_never_says_who_has_an_account(api, env_admin, mail, monkeypatch):
    admin = _admin(api, env_admin)
    _settled_operator(api, admin)
    nobody = api.post("/auth/forgot-password", json={"email": "nobody@test.com"})
    somebody = api.post("/auth/forgot-password", json={"email": "ops@test.com"})
    assert nobody.status_code == somebody.status_code == 202
    assert nobody.json() == somebody.json()
    assert [m[0] for m in mail] == ["ops@test.com"]

    # Without a mail server nothing is sent, and the answer is still the same.
    monkeypatch.setattr(get_settings(), "smtp_host", "")
    assert api.post("/auth/forgot-password", json={"email": "ops@test.com"}).status_code == 202
    assert len(mail) == 1


def test_reset_links_are_throttled_per_email(api, env_admin, mail):
    for _ in range(5):
        assert api.post("/auth/forgot-password", json={"email": "ops@test.com"}).status_code == 202
    locked = api.post("/auth/forgot-password", json={"email": "ops@test.com"})
    assert locked.status_code == 429


def test_a_reset_link_token_opens_nothing_but_the_reset(api, env_admin, mail):
    """The link travels by mail. Taken as a bearer token it would be an
    8-hour session for whoever reads the mailbox, and one the reset itself
    would not end — so every bearer door refuses it, and refusing does not
    spend it."""
    admin = _admin(api, env_admin)
    _settled_operator(api, admin)
    api.post("/auth/forgot-password", json={"email": "ops@test.com"})
    token = _token_in(mail[0][2])
    assert api.get("/auth/me", headers=_bearer(token)).status_code == 401
    assert api.get("/addresses", headers=_bearer(token)).status_code == 401
    assert api.post("/auth/reset-password", json={"token": token, "new_password": "from-the-mail-link-1"}).status_code == 200


def test_the_reset_mail_goes_out_after_the_answer_not_before(api, db, env_admin, mail):
    """Sent inline, the mail server's round trip would make the answer for a
    known email measurably slower than for an unknown one — telling who has
    an account, which the answer's wording is careful not to. So the route
    only queues the mail, and the server sends it once the answer has gone."""
    admin = _admin(api, env_admin)
    _settled_operator(api, admin)
    scope = {
        "type": "http", "method": "POST", "path": "/auth/forgot-password", "headers": [],
        "query_string": b"", "scheme": "http", "server": ("testserver", 80), "client": ("203.0.113.9", 4321),
    }
    tasks = BackgroundTasks()
    answer = auth_api.forgot_password(ForgotPasswordIn(email="ops@test.com"), Request(scope), tasks, db)
    assert "reset link" in answer["detail"]
    assert mail == []  # nothing sent while answering
    assert len(tasks.tasks) == 1
    asyncio.run(tasks())
    assert [m[0] for m in mail] == ["ops@test.com"]

    # An unknown email: the same answer, nothing queued, nothing sent.
    tasks = BackgroundTasks()
    assert auth_api.forgot_password(ForgotPasswordIn(email="nobody@test.com"), Request(scope), tasks, db) == answer
    assert tasks.tasks == [] and len(mail) == 1


def test_the_reset_mail_is_sent_over_a_verified_tls_connection(monkeypatch):
    """starttls() without a context takes any certificate the mail server —
    or whoever sits between — shows. The link in this mail signs its reader
    in, so the server's certificate and name are verified."""
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_from", "ordering@example.com")
    monkeypatch.setattr(settings, "smtp_user", "mailer")
    monkeypatch.setattr(settings, "smtp_password", "made-up-mail-password")
    monkeypatch.setattr(settings, "smtp_starttls", True)
    calls: list[tuple] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("quit",))
            return False

        def starttls(self, context=None):
            calls.append(("starttls", context))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, message):
            calls.append(("send", message["To"]))

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    mailer.send_mail("ops@test.com", "subject", "body")

    assert [c[0] for c in calls] == ["connect", "starttls", "login", "send", "quit"]
    assert calls[0][1:] == ("mail.example.com", 587, mailer.SEND_TIMEOUT_SECONDS)
    context = calls[1][1]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
