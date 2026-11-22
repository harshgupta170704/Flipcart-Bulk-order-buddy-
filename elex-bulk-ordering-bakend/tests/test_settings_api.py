"""Phase 4a: Settings API + the DB-overlaid effective settings."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth as auth_api
from app.api import settings as settings_api
from app.config import get_settings
from app.core import app_settings
from app.db.models import Setting

IMAP_PASSWORD = "imap-s3cr3t-never-echo-me"
PROXY = "http://proxy.example.net:3128"

#: Moved to the Address Book; they are not settings any more.
RETIRED_BUSINESS_KEYS = ("default_gstin", "default_business_name", "default_address", "default_pin")


@pytest.fixture
def admin(monkeypatch):
    """Pin credentials and the env-side OTP password so results are stable."""
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_username", "admin")
    monkeypatch.setattr(settings, "admin_password", "admin")
    monkeypatch.setattr(settings, "admin_password_hash", "")
    # The shipped default password is accepted in dev mode only: set here, so
    # the suite never leans on a laptop's own settings (a fresh clone has none).
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(
        settings, "jwt_secret", "test-secret-long-enough-for-hmac-sha256"
    )
    monkeypatch.setattr(settings, "otp_imap_password", "")
    return {"username": "admin", "password": "admin"}


@pytest.fixture
def api_client():
    """Own app with the auth + settings routers — independent of app.main."""
    api = FastAPI()
    api.include_router(auth_api.router)
    api.include_router(settings_api.router)
    return TestClient(api)


@pytest.fixture
def token(admin, api_client):
    resp = api_client.post("/auth/login", json=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture
def auth(token):
    return {"Authorization": f"Bearer {token}"}


# --- protection --------------------------------------------------------------


def test_get_settings_without_token_is_rejected(admin, api_client):
    resp = api_client.get("/settings")
    assert resp.status_code != 200
    assert resp.status_code == 401


def test_put_settings_without_token_is_rejected(admin, api_client):
    resp = api_client.put("/settings", json={"proxy_server": PROXY})
    assert resp.status_code != 200
    assert resp.status_code == 401


def test_get_settings_with_token(admin, api_client, auth):
    resp = api_client.get("/settings", headers=auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for field in app_settings.EDITABLE_FIELDS:
        if field in app_settings.SECRET_FIELDS:
            continue
        assert field in body
    assert body["max_concurrency"] == get_settings().max_concurrency


# --- round-tripping ----------------------------------------------------------


def test_put_then_get_roundtrips(admin, api_client, auth):
    put = api_client.put(
        "/settings",
        json={"proxy_server": PROXY, "max_concurrency": 9},
        headers=auth,
    )
    assert put.status_code == 200, put.text
    assert put.json()["proxy_server"] == PROXY
    assert put.json()["max_concurrency"] == 9

    get = api_client.get("/settings", headers=auth)
    assert get.json()["proxy_server"] == PROXY
    assert get.json()["max_concurrency"] == 9


def test_the_read_back_timer_is_a_setting_with_bounds(admin, api_client, auth):
    """Off by default (0); any number of hours up to a week; the timer in
    app.jobs.schedule reads the effective value."""
    assert api_client.get("/settings", headers=auth).json()["sync_every_hours"] == 0

    put = api_client.put("/settings", json={"sync_every_hours": 6}, headers=auth)
    assert put.status_code == 200, put.text
    assert put.json()["sync_every_hours"] == 6
    assert api_client.get("/settings", headers=auth).json()["sync_every_hours"] == 6

    assert api_client.put("/settings", json={"sync_every_hours": -1}, headers=auth).status_code == 422
    assert api_client.put("/settings", json={"sync_every_hours": 500}, headers=auth).status_code == 422


def test_partial_put_keeps_other_overrides(admin, api_client, auth):
    api_client.put("/settings", json={"proxy_server": PROXY}, headers=auth)
    api_client.put("/settings", json={"otp_imap_host": "imap.example.net"}, headers=auth)
    body = api_client.get("/settings", headers=auth).json()
    assert body["proxy_server"] == PROXY
    assert body["otp_imap_host"] == "imap.example.net"


def test_the_retired_business_keys_are_not_settings_any_more(admin, api_client, auth):
    """GSTIN, business name, address and pin live in the Address Book now:
    writing one is a 422 naming it, and GET no longer returns them."""
    for key in RETIRED_BUSINESS_KEYS:
        resp = api_client.put("/settings", json={key: "x"}, headers=auth)
        assert resp.status_code == 422, f"{key} -> {resp.status_code}"
        assert key in resp.text
    body = api_client.get("/settings", headers=auth).json()
    assert not any(key in body for key in RETIRED_BUSINESS_KEYS)


def test_an_older_saved_row_with_the_retired_keys_does_not_leak_them(db):
    db.add(
        Setting(
            key=app_settings.SETTINGS_KEY,
            value={"default_pin": "226001", "default_gstin": "09ABCDE1234F1Z5", "max_concurrency": 3},
        )
    )
    db.commit()
    assert app_settings.get_overrides(db) == {"max_concurrency": 3}
    assert not any(key in app_settings.get_effective(db) for key in RETIRED_BUSINESS_KEYS)


def test_booleans_and_ints_are_coerced(admin, api_client, auth):
    resp = api_client.put(
        "/settings",
        json={"browser_headless": False, "otp_imap_port": "143", "otp_imap_use_ssl": "no"},
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["browser_headless"] is False
    assert body["otp_imap_port"] == 143
    assert body["otp_imap_use_ssl"] is False


def test_empty_put_is_a_no_op(admin, api_client, auth):
    before = api_client.get("/settings", headers=auth).json()
    resp = api_client.put("/settings", json={}, headers=auth)
    assert resp.status_code == 200, resp.text
    assert resp.json() == before


# --- the OTP password is write-only -----------------------------------------


def test_otp_password_is_never_returned(admin, api_client, auth):
    before = api_client.get("/settings", headers=auth)
    assert before.json()["otp_imap_password_set"] is False
    assert "otp_imap_password" not in before.json()

    put = api_client.put(
        "/settings", json={"otp_imap_password": IMAP_PASSWORD}, headers=auth
    )
    assert put.status_code == 200, put.text
    get = api_client.get("/settings", headers=auth)

    # Assert on the raw body text: no masking, no partial echo, nothing.
    assert IMAP_PASSWORD not in put.text
    assert IMAP_PASSWORD not in get.text
    assert "otp_imap_password" not in put.json()
    assert "otp_imap_password" not in get.json()
    assert put.json()["otp_imap_password_set"] is True
    assert get.json()["otp_imap_password_set"] is True


def test_otp_password_is_still_usable_internally(admin, api_client, auth, db):
    api_client.put("/settings", json={"otp_imap_password": IMAP_PASSWORD}, headers=auth)
    # The worker/OTP reader needs the real value; only the API hides it.
    assert app_settings.get_effective(db)["otp_imap_password"] == IMAP_PASSWORD


# --- validation --------------------------------------------------------------


def test_max_concurrency_zero_is_422(admin, api_client, auth):
    resp = api_client.put("/settings", json={"max_concurrency": 0}, headers=auth)
    assert resp.status_code == 422
    assert "max_concurrency" in resp.text


def test_max_concurrency_negative_is_422(admin, api_client, auth):
    resp = api_client.put("/settings", json={"max_concurrency": -3}, headers=auth)
    assert resp.status_code == 422


def test_forbidden_keys_are_422_and_named(admin, api_client, auth):
    for key, value in (
        ("database_url", "postgresql://evil/db"),
        ("jwt_secret", "pwned"),
        ("admin_password_hash", "x"),
        ("not_a_setting", 1),
    ):
        resp = api_client.put("/settings", json={key: value}, headers=auth)
        assert resp.status_code == 422, f"{key} -> {resp.status_code}"
        assert key in resp.text


def test_forbidden_key_is_not_persisted(admin, api_client, auth, db):
    before = get_settings().database_url
    api_client.put("/settings", json={"database_url": "postgresql://evil/db"}, headers=auth)
    assert "database_url" not in app_settings.get_overrides(db)
    assert get_settings().database_url == before


def test_bad_port_is_422(admin, api_client, auth):
    for port in (0, 70000, "not-a-port"):
        resp = api_client.put("/settings", json={"otp_imap_port": port}, headers=auth)
        assert resp.status_code == 422, f"{port} -> {resp.status_code}"


# --- core helpers ------------------------------------------------------------


def test_overrides_start_empty_and_effective_falls_back_to_env(db):
    assert app_settings.get_overrides(db) == {}
    effective = app_settings.get_effective(db)
    assert set(effective) == set(app_settings.EDITABLE_FIELDS)
    assert effective["max_concurrency"] == get_settings().max_concurrency


def test_set_overrides_merges_and_persists(db):
    app_settings.set_overrides(db, {"proxy_server": PROXY})
    merged = app_settings.set_overrides(db, {"max_concurrency": 2})
    assert merged == {"proxy_server": PROXY, "max_concurrency": 2}
    assert app_settings.get_overrides(db) == merged
    assert app_settings.get_effective(db)["proxy_server"] == PROXY


def test_set_overrides_rejects_unknown_keys(db):
    with pytest.raises(app_settings.SettingsValidationError) as excinfo:
        app_settings.set_overrides(db, {"jwt_secret": "nope"})
    assert excinfo.value.key == "jwt_secret"
    assert app_settings.get_overrides(db) == {}


def test_set_overrides_rejects_bad_values(db):
    for payload in (
        {"max_concurrency": 0},
        {"max_concurrency": "many"},
        {"otp_imap_port": 99999},
        {"browser_headless": "maybe"},
        {"proxy_server": {"a": 1}},
    ):
        with pytest.raises(app_settings.SettingsValidationError):
            app_settings.set_overrides(db, payload)


def test_public_view_hides_every_secret():
    effective = {f: "x" for f in app_settings.EDITABLE_FIELDS}
    public = app_settings.public_view(effective)
    for secret in app_settings.SECRET_FIELDS:
        assert secret not in public
        assert public[f"{secret}_set"] is True
    assert app_settings.public_view({})["otp_imap_password_set"] is False
