"""Phase 4a: app login — password hashing, JWT, /auth routes."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import auth as auth_api
from app.config import Settings, get_settings
from app.core import users
from app.core.logging_setup import configure_logging
from app.core.login_guard import guard
from app.core.security import (
    check_security_config,
    create_access_token,
    decode_access_token,
    hash_password,
    is_password_hash,
    verify_password,
)
from app.db.models import User, UserRole

# Low iteration count: these tests exercise the format, not the KDF's cost.
FAST = 1_000
# >= 32 bytes, as RFC 7518 wants for HMAC-SHA256 (PyJWT warns otherwise).
TEST_SECRET = "test-secret-long-enough-for-hmac-sha256"


@pytest.fixture
def admin(monkeypatch):
    """Pin the admin credentials so the suite never depends on a local .env.

    The shipped default password is only accepted in dev mode, which is what
    a laptop running the defaults is.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_username", "admin")
    monkeypatch.setattr(settings, "admin_password", "admin")
    monkeypatch.setattr(settings, "admin_password_hash", "")
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(settings, "jwt_secret", TEST_SECRET)
    monkeypatch.setattr(settings, "access_token_expire_minutes", 60)
    guard.reset()
    return {"username": "admin", "password": "admin"}


@pytest.fixture
def auth_client():
    """Own app with just the auth router — independent of app.main wiring."""
    api = FastAPI()
    api.include_router(auth_api.router)
    return TestClient(api)


# --- password hashing --------------------------------------------------------


def test_hash_verify_roundtrip():
    stored = hash_password("correct horse battery staple", iterations=FAST)
    assert stored.startswith("pbkdf2_sha256$1000$")
    assert verify_password("correct horse battery staple", stored) is True


def test_hash_verify_roundtrip_default_iterations():
    stored = hash_password("s3cret")
    assert verify_password("s3cret", stored) is True


def test_verify_wrong_password_is_false():
    stored = hash_password("s3cret", iterations=FAST)
    assert verify_password("s3cre", stored) is False
    assert verify_password("S3cret", stored) is False
    assert verify_password("", stored) is False


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "garbage",
        "pbkdf2_sha256$1000$deadbeef",  # too few parts
        "pbkdf2_sha256$1000$deadbeef$ff$extra",  # too many parts
        "pbkdf2_sha256$notanint$deadbeef$ffff",
        "pbkdf2_sha256$1000$nothex!!$ffff",
        "pbkdf2_sha256$1000$deadbeef$nothex!!",
        "pbkdf2_sha256$0$deadbeef$ffff",  # zero iterations
        "pbkdf2_sha256$99999999999$deadbeef$ffff",  # absurd work factor
        "bcrypt$1000$deadbeef$ffff",  # foreign algorithm
        "pbkdf2_sha256$1000$$ffff",  # empty salt
        None,
        12345,
    ],
)
def test_verify_malformed_hash_is_false_and_never_raises(bad):
    assert verify_password("anything", bad) is False
    assert is_password_hash(bad) is False


def test_a_hash_of_our_own_is_recognised_as_one():
    assert is_password_hash(hash_password("anything", iterations=FAST)) is True


def test_same_password_hashes_differ_random_salt():
    first = hash_password("same", iterations=FAST)
    second = hash_password("same", iterations=FAST)
    assert first != second
    assert verify_password("same", first) and verify_password("same", second)


# --- the environment credential --------------------------------------------------


def test_env_password_plaintext_fallback(admin, db):
    assert users.env_password_ok("admin") is True
    assert users.env_password_ok("nope") is False
    # The name is checked by authenticate(), not by the password check.
    assert users.authenticate(db, "root", "admin") is None
    assert users.authenticate(db, "admin", "admin") is not None


def test_env_password_prefers_hash_over_plaintext(admin, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "admin_password_hash", hash_password("hashed-pw", iterations=FAST)
    )
    assert users.env_password_ok("hashed-pw") is True
    # The plaintext value must be ignored once a hash is configured.
    assert users.env_password_ok("admin") is False


def test_env_password_false_when_nothing_configured(admin, monkeypatch, db):
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_password", "")
    monkeypatch.setattr(settings, "admin_password_hash", "")
    assert users.env_password_ok("") is False
    assert users.env_password_ok("anything") is False
    assert users.authenticate(db, "admin", "") is None


def test_the_shipped_default_password_is_refused_outside_dev_mode(admin, monkeypatch, auth_client):
    monkeypatch.setattr(get_settings(), "dev_mode", False)
    assert users.env_password_ok("admin") is False
    assert auth_client.post("/auth/login", json=admin).status_code == 401
    # A password of one's own is fine without dev mode.
    monkeypatch.setattr(get_settings(), "admin_password", "something-else-entirely")
    assert users.env_password_ok("something-else-entirely") is True


# --- tokens ------------------------------------------------------------------


def test_token_roundtrip(admin):
    claims = decode_access_token(create_access_token("admin"))
    assert claims["sub"] == "admin"
    assert "exp" in claims and "iat" in claims


def test_tampered_token_raises(admin):
    token = create_access_token("admin")
    head, payload, signature = token.split(".")
    tampered = f"{head}.{payload}.{signature[:-3]}abc"
    with pytest.raises(ValueError):
        decode_access_token(tampered)
    with pytest.raises(ValueError):
        decode_access_token("not.a.token")
    with pytest.raises(ValueError):
        decode_access_token("")
    with pytest.raises(ValueError):
        decode_access_token(None)  # a missing header must not crash a caller


def test_token_signed_with_another_secret_raises(admin, monkeypatch):
    token = create_access_token("admin")
    monkeypatch.setattr(get_settings(), "jwt_secret", TEST_SECRET + "-rotated")
    with pytest.raises(ValueError):
        decode_access_token(token)


def test_expired_token_raises(admin):
    token = create_access_token("admin", expires_minutes=-1)
    with pytest.raises(ValueError):
        decode_access_token(token)


def test_create_token_requires_subject(admin):
    with pytest.raises(ValueError):
        create_access_token("")


# --- routes ------------------------------------------------------------------


def test_login_returns_token(admin, auth_client):
    resp = auth_client.post("/auth/login", json=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 60 * 60
    assert decode_access_token(body["access_token"])["sub"] == "admin"


def test_login_bad_credentials_401(admin, auth_client):
    bad_pw = auth_client.post(
        "/auth/login", json={"username": "admin", "password": "wrong"}
    )
    bad_user = auth_client.post(
        "/auth/login", json={"username": "nobody", "password": "admin"}
    )
    assert bad_pw.status_code == 401
    assert bad_user.status_code == 401
    # Same message either way: the response must not say which half was wrong.
    assert bad_pw.json()["detail"] == bad_user.json()["detail"]
    assert bad_pw.headers["www-authenticate"] == "Bearer"


def test_me_without_token_is_rejected(admin, auth_client):
    resp = auth_client.get("/auth/me")
    assert resp.status_code != 200
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_me_with_bad_token_is_rejected(admin, auth_client):
    resp = auth_client.get("/auth/me", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401
    expired = create_access_token("admin", expires_minutes=-1)
    resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    # Wrong scheme must not pass either.
    resp = auth_client.get("/auth/me", headers={"Authorization": "Basic YWRtaW46YWRtaW4="})
    assert resp.status_code == 401


def test_me_with_token(admin, auth_client):
    token = auth_client.post("/auth/login", json=admin).json()["access_token"]
    resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["username"], body["email"], body["role"]) == ("admin", "admin", "admin")
    assert body["must_change_password"] is False
    assert body["is_env_admin"] is True  # the UI says the environment password stays a key
    assert body["expires_at"] is not None  # the session countdown reads this


def test_sign_in_is_throttled_after_five_wrong_passwords(admin, auth_client):
    """Five wrong tries lock the email for a while — and the right password
    is refused during the lock too, so the lock cannot be probed past."""
    for _ in range(5):
        assert auth_client.post("/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    locked = auth_client.post("/auth/login", json=admin)
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0
    assert "minute" in locked.json()["detail"]
    guard.reset()
    assert auth_client.post("/auth/login", json=admin).status_code == 200


def test_sign_in_options_are_public(admin, auth_client, monkeypatch):
    assert auth_client.get("/auth/options").json() == {"password_reset_by_email": False}
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(settings, "smtp_from", "ordering@example.com")
    monkeypatch.setattr(settings, "app_url", "https://orders.example.com")
    assert auth_client.get("/auth/options").json() == {"password_reset_by_email": True}


def test_a_reset_link_is_never_sent_for_a_name_that_is_not_an_address(admin, auth_client, monkeypatch):
    """The configured admin signs in by a bare name; a reset link queued to the
    string "admin" would sign its reader in as the admin (audit F-31). Same
    answer as for everyone, nothing sent."""
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "mail.example.com")
    monkeypatch.setattr(settings, "smtp_from", "ordering@example.com")
    monkeypatch.setattr(settings, "app_url", "https://orders.example.com")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("app.core.mailer.send_password_reset", lambda to, token: sent.append((to, token)))

    response = auth_client.post("/auth/forgot-password", json={"email": "admin"})

    assert response.status_code == 202
    assert "on its way" in response.json()["detail"]
    assert sent == []


def test_guessing_the_current_password_through_change_password_is_throttled(admin, auth_client):
    """A token holder had unlimited tries at the current password (audit F-31);
    the change door now locks like the sign-in door."""
    token = auth_client.post("/auth/login", json=admin).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    wrong = {"current_password": "not-the-password", "new_password": "Quite-Another-Passw0rd!"}

    answers = [auth_client.post("/auth/change-password", json=wrong, headers=headers).status_code for _ in range(6)]

    assert answers[:5] == [403] * 5
    assert answers[5] == 429
    # The right password is refused too while the lock holds — the lock is on the door, not the guess.
    right = {"current_password": admin["password"], "new_password": "Quite-Another-Passw0rd!"}
    assert auth_client.post("/auth/change-password", json=right, headers=headers).status_code == 429


def test_check_security_config_flags_the_shipped_defaults(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_secret", "change-me-in-production")
    monkeypatch.setattr(settings, "admin_password_hash", "")
    monkeypatch.setattr(settings, "admin_password", "admin")
    monkeypatch.setattr(settings, "dev_mode", False)
    problems = check_security_config()
    assert any("ORDERING_JWT_SECRET" in p for p in problems)
    assert any("ORDERING_ADMIN_PASSWORD_HASH" in p for p in problems)
    assert any("shipped default" in p and "refused" in p for p in problems)


def test_check_security_config_says_when_no_admin_credential_exists(monkeypatch):
    """Both blank is not "plaintext in use": it is "nobody can sign in", and
    the message must say how to fix that."""
    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "admin_password_hash", "")
    monkeypatch.setattr(settings, "admin_password", "")
    monkeypatch.setattr(settings, "dev_mode", False)
    problems = check_security_config()
    assert len(problems) == 1
    assert "set-password" in problems[0]
    assert "plaintext" not in problems[0]


def test_check_security_config_flags_the_default_config_py_ships(monkeypatch):
    """The literal in config.py, not only the one older .env files copied —
    the download tokens are signed with this secret too."""
    from app.config import Settings

    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_secret", Settings.model_fields["jwt_secret"].default)
    assert any("shipped default" in p for p in check_security_config())


def test_check_security_config_clean_when_hardened(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "dev_mode", False)
    monkeypatch.setattr(
        settings, "admin_password_hash", hash_password("pw", iterations=FAST)
    )
    assert check_security_config() == []


def test_login_never_echoes_the_submitted_password(admin, auth_client):
    bad = auth_client.post(
        "/auth/login", json={"username": "admin", "password": "sup3r-secret-pw"}
    )
    assert bad.status_code == 401
    assert "sup3r-secret-pw" not in bad.text


def test_a_failed_sign_in_never_logs_what_was_typed_as_the_name(admin, auth_client, caplog):
    """The email box often gets the password. The log keeps the first two
    characters and the domain of an email, only the length of anything else,
    and names the configured admin as such — never the text itself."""
    with caplog.at_level(logging.INFO, logger="ordering.api.auth"):
        auth_client.post("/auth/login", json={"username": "Tr0ub4dor&3-typed-wrong", "password": "whatever"})
        auth_client.post("/auth/login", json={"username": "johanna@example.com", "password": "whatever"})
        auth_client.post("/auth/login", json={"username": "admin", "password": "whatever"})
        # No mail server here, so this is logged as a warning — masked the same way.
        auth_client.post("/auth/forgot-password", json={"email": "Tr0ub4dor&3-typed-wrong"})
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Tr0ub4dor&3-typed-wrong" not in text
    assert "johanna@example.com" not in text
    assert "jo…@example.com" in text
    assert "<23 chars>" in text
    assert "the configured admin" in text


# --- the throttle: one limit for an email, a much higher one for an address --------


def test_the_address_limit_is_much_higher_than_the_email_limit_by_default():
    """One person's five typos lock their own account's door; an office behind
    one NAT address must not be locked out with them."""
    per_email = Settings.model_fields["login_max_failures_per_email"].default
    per_address = Settings.model_fields["login_max_failures_per_address"].default
    assert per_email == 5
    assert per_address >= 10 * per_email


def test_one_persons_typos_lock_their_email_but_not_the_shared_address(admin, auth_client):
    for _ in range(5):
        assert auth_client.post("/auth/login", json={"username": "alice@example.com", "password": "wrong"}).status_code == 401
    assert auth_client.post("/auth/login", json={"username": "alice@example.com", "password": "wrong"}).status_code == 429
    # A colleague behind the same address still signs in.
    assert auth_client.post("/auth/login", json=admin).status_code == 200


def test_spraying_many_emails_from_one_address_locks_the_address_at_its_own_limit(admin, auth_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_max_failures_per_address", 8)
    for i in range(8):
        assert auth_client.post("/auth/login", json={"username": f"user{i}@example.com", "password": "wrong"}).status_code == 401
    locked = auth_client.post("/auth/login", json=admin)  # the right password, from the locked address
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0


def test_reset_asks_count_per_address_at_the_higher_limit_too(admin, auth_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "login_max_failures_per_address", 6)
    for i in range(6):
        assert auth_client.post("/auth/forgot-password", json={"email": f"user{i}@example.com"}).status_code == 202
    assert auth_client.post("/auth/forgot-password", json={"email": "another@example.com"}).status_code == 429


def _dockerfile() -> str:
    return (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")


def _shipped_forwarded_allow_ips() -> str:
    """The trust list the container ships with, so this pins the deployment
    and not a value of the test's own."""
    match = re.search(r'^ENV FORWARDED_ALLOW_IPS="([^"]+)"', _dockerfile(), re.MULTILINE)
    assert match, "backend/Dockerfile must set FORWARDED_ALLOW_IPS"
    return match.group(1)


def test_behind_the_shipped_proxy_setup_the_throttle_sees_the_browsers_address(admin, caplog):
    """In the compose stack request.client is the nginx container unless
    uvicorn is told to trust its X-Forwarded-For; with the shipped trust list
    it is the browser's address — and a browser cannot choose one, because the
    real address, appended last by the proxy, is the first untrusted entry
    reading from the right."""
    assert "--proxy-headers" in _dockerfile()
    api = FastAPI()
    api.include_router(auth_api.router)
    proxied = TestClient(
        ProxyHeadersMiddleware(api, trusted_hosts=_shipped_forwarded_allow_ips()),
        client=("172.18.0.5", 40000),  # the proxy container, on the compose network
    )
    with caplog.at_level(logging.WARNING, logger="ordering.api.auth"):
        proxied.post(
            "/auth/login",
            json={"username": "x@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
        proxied.post(
            "/auth/login",
            json={"username": "x@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.9"},  # a forged entry, then the real one
        )
        # A browser on a private address (an office LAN): nginx sends exactly
        # that one address — it replaces the header, never appends to it (see
        # frontend/nginx.conf) — and it must come through as the browser's,
        # although it falls inside the trusted ranges.
        proxied.post(
            "/auth/login",
            json={"username": "x@example.com", "password": "wrong"},
            headers={"X-Forwarded-For": "192.168.1.50"},
        )
    addresses = [r.getMessage().rsplit(" from ", 1)[1] for r in caplog.records if "failed login" in r.getMessage()]
    assert addresses == ["203.0.113.9", "203.0.113.9", "192.168.1.50"]


def test_nginx_sends_one_forwarded_address_and_never_appends_the_browsers():
    """The whole trust model rests on nginx replacing X-Forwarded-For: with
    $proxy_add_x_forwarded_for a browser on a private address could put any
    address in front of its own and uvicorn would take it."""
    from tests import layout

    conf = layout.web_or_skip("nginx.conf").read_text(encoding="utf-8")
    directives = [line.strip() for line in conf.splitlines() if line.strip() and not line.strip().startswith("#")]
    forwarded = [line for line in directives if line.startswith("proxy_set_header X-Forwarded-For")]
    # One per location that passes plain HTTP to the API: the invoices, the
    # two card-sheet routes, the account import, the sheet of order ids, the
    # order report, and the rest of /api/.
    assert forwarded == ["proxy_set_header X-Forwarded-For $remote_addr;"] * 7
    assert not any("proxy_add_x_forwarded_for" in line for line in directives)


# --- a reset-link token is not a session -----------------------------------------


def test_a_reset_link_token_is_not_a_session(admin, auth_client, db):
    """A reset token carries a purpose, and that purpose is all it opens: as a
    bearer token it is refused — it travelled by mail, and must not become an
    8-hour session that outlives the reset."""
    user = User(email="ops@test.com", name="Ops", password_hash=hash_password("x", iterations=FAST), role=UserRole.operator, active=True)
    db.add(user)
    db.commit()
    token = users.reset_token(user)
    assert auth_client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert users.lookup_principal(decode_access_token(token)) is None
    assert users.user_for_reset_token(db, token) is not None  # still good for the reset itself


# --- a mangled admin hash is named at start-up ----------------------------------------


def test_check_security_config_shouts_about_a_mangled_admin_hash(monkeypatch, caplog):
    """A hash a shell ate the '$' parts of is "set", and verify_password just
    says no — the admin would be locked out with only "Incorrect email or
    password" to go on. Start-up must name the problem, as an error."""
    settings = get_settings()
    monkeypatch.setattr(settings, "jwt_secret", "x" * 48)
    monkeypatch.setattr(settings, "dev_mode", False)
    # What `echo "pbkdf2_sha256$390000$…"` leaves behind in bash: $3 and $… expanded away.
    monkeypatch.setattr(settings, "admin_password_hash", "pbkdf2_sha25690000")
    with caplog.at_level(logging.ERROR, logger="ordering.security"):
        problems = check_security_config()
    assert len(problems) == 1
    assert "ORDERING_ADMIN_PASSWORD_HASH" in problems[0] and "not a hash" in problems[0]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "ORDERING_ADMIN_PASSWORD_HASH" in errors[0].getMessage()
    # A proper hash passes, as before.
    monkeypatch.setattr(settings, "admin_password_hash", hash_password("pw", iterations=FAST))
    assert check_security_config() == []


# --- the audit trail reaches a handler ------------------------------------------------


@pytest.fixture
def logging_state():
    """Leave the "ordering" loggers as they were: levels and handlers."""
    tree = [logging.getLogger("ordering"), logging.getLogger("ordering.audit")]
    saved = [(lg, lg.level, list(lg.handlers)) for lg in tree]
    yield
    for lg, level, handlers in saved:
        lg.setLevel(level)
        lg.handlers[:] = handlers


def test_configure_logging_gives_the_audit_line_a_way_out(logging_state, capsys):
    """Under the shipped uvicorn command no handler exists for INFO lines, so
    the audit trail went nowhere; after configure_logging() it reaches stderr,
    with a timestamp."""
    handler = configure_logging()
    assert handler in logging.getLogger("ordering").handlers
    logging.getLogger("ordering.audit").info("POST /addresses by %s", "owner@example.com")
    err = capsys.readouterr().err
    assert "POST /addresses by owner@example.com" in err
    assert "ordering.audit" in err and "INFO" in err
    assert re.search(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", err, re.MULTILINE)


def test_configure_logging_is_idempotent_and_follows_the_level_setting(logging_state, capsys, monkeypatch):
    first = configure_logging()
    monkeypatch.setattr(get_settings(), "log_level", "warning")
    second = configure_logging()
    assert first is second
    assert logging.getLogger("ordering").handlers.count(first) == 1
    assert logging.getLogger("ordering").level == logging.WARNING
    logging.getLogger("ordering.api.auth").info("chatter")
    logging.getLogger("ordering.audit").info("the audit trail is never turned down")
    err = capsys.readouterr().err
    assert "chatter" not in err
    assert "the audit trail is never turned down" in err


def test_configure_logging_treats_an_unknown_level_as_info(logging_state, monkeypatch):
    monkeypatch.setattr(get_settings(), "log_level", "loud")
    configure_logging()
    assert logging.getLogger("ordering").level == logging.INFO
