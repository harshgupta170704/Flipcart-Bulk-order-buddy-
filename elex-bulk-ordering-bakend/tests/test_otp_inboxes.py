"""More than one OTP inbox (the owner's two accounts, 26 Oct 2026): numbered
settings beside the first inbox, each account's login read from the inbox
that serves its address, and the Settings page told about them — never their
passwords."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.otp import (
    ImapOtpReader,
    InboxSpec,
    NullOtpReader,
    OtpReader,
    RoutedOtpReader,
    extra_otp_inboxes,
    get_otp_reader,
)
from app.core.security import create_access_token


def _settings(**over):
    base = dict(
        otp_imap_host="imap.gmail.com",
        otp_imap_port=993,
        otp_imap_user="main@gmail.com",
        otp_imap_password="main-app-password",
        otp_imap_mailbox="INBOX",
        otp_imap_use_ssl=True,
        otp_poll_interval=3,
        otp_sender_filter="flipkart",
        otp_since_days=1,
        otp_max_age_minutes=15,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _spec(number: int, user: str, serves: tuple[str, ...]) -> InboxSpec:
    return InboxSpec(number, user, "app-password", "imap.gmail.com", 993, "INBOX", True, serves)


def test_a_second_inbox_needs_only_its_address_and_app_password():
    specs = extra_otp_inboxes(
        _settings(),
        environ={"ORDERING_OTP_IMAP_USER_2": "second@gmail.com", "ORDERING_OTP_IMAP_PASSWORD_2": "abcd efgh ijkl mnop"},
        use_env_file=False,
    )

    assert len(specs) == 1
    spec = specs[0]
    assert (spec.number, spec.user, spec.host, spec.port, spec.mailbox, spec.use_ssl) == (
        2,
        "second@gmail.com",
        "imap.gmail.com",
        993,
        "INBOX",
        True,
    )
    assert spec.serves == ("second@gmail.com",)
    assert "password" not in spec.summary()


def test_an_inbox_can_serve_named_accounts_and_whole_domains():
    specs = extra_otp_inboxes(
        _settings(),
        environ={
            "ORDERING_OTP_IMAP_USER_3": "catch@all.in",
            "ORDERING_OTP_IMAP_PASSWORD_3": "pw",
            "ORDERING_OTP_IMAP_FOR_3": "A@x.com, @shop.in",
            "ORDERING_OTP_IMAP_HOST_3": "imap.other.in",
            "ORDERING_OTP_IMAP_USE_SSL_3": "false",
        },
        use_env_file=False,
    )

    assert specs[0].serves == ("a@x.com", "@shop.in")
    assert specs[0].host == "imap.other.in"
    assert specs[0].use_ssl is False


def test_an_incomplete_inbox_is_skipped(caplog):
    with caplog.at_level("WARNING"):
        assert extra_otp_inboxes(_settings(), environ={"ORDERING_OTP_IMAP_USER_2": "second@gmail.com"}, use_env_file=False) == []
    assert "OTP inbox 2 is not used: ORDERING_OTP_IMAP_USER_2 and ORDERING_OTP_IMAP_PASSWORD_2 are both needed" in caplog.text


def test_an_inbox_named_but_left_blank_is_skipped_quietly(caplog):
    blank = {"ORDERING_OTP_IMAP_USER_2": "", "ORDERING_OTP_IMAP_PASSWORD_2": " ", "ORDERING_OTP_IMAP_PORT_2": "993"}
    with caplog.at_level("WARNING"):
        assert extra_otp_inboxes(_settings(), environ=blank, use_env_file=False) == []
    assert "OTP inbox 2" not in caplog.text


def test_the_settings_file_is_read_too(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "ORDERING_OTP_IMAP_USER_2=second@gmail.com\nORDERING_OTP_IMAP_PASSWORD_2='abcd efgh ijkl mnop'\n",
        encoding="utf-8",
    )

    specs = extra_otp_inboxes(_settings(), environ={}, env_file=str(env))

    assert specs[0].user == "second@gmail.com"
    assert specs[0].password == "abcd efgh ijkl mnop"


def test_each_account_is_read_from_the_inbox_that_serves_it():
    reader = get_otp_reader(
        _settings(),
        extras=[_spec(2, "second@gmail.com", ("second@gmail.com",)), _spec(3, "catch@all.in", ("@shop.in",))],
    )

    assert isinstance(reader, RoutedOtpReader)
    own, label = reader.reader_for("Second@Gmail.com")
    assert isinstance(own, ImapOtpReader) and own.user == "second@gmail.com" and "inbox 2" in label
    by_domain, label = reader.reader_for("buyer@shop.in")
    assert by_domain.user == "catch@all.in" and "every @shop.in account" in label
    main, label = reader.reader_for("main@gmail.com")
    assert main.user == "main@gmail.com" and label == "the main inbox"


def test_with_no_numbered_inbox_the_reader_is_what_it_was():
    assert isinstance(get_otp_reader(_settings(), extras=[]), ImapOtpReader)
    assert isinstance(get_otp_reader(_settings(otp_imap_host="", otp_imap_user=""), extras=[]), NullOtpReader)


def test_a_second_inbox_works_even_when_the_first_is_not_set():
    reader = get_otp_reader(_settings(otp_imap_user=""), extras=[_spec(2, "second@gmail.com", ("second@gmail.com",))])
    assert isinstance(reader, RoutedOtpReader)
    assert isinstance(reader.default, NullOtpReader)


def test_the_routed_reader_asks_the_inbox_that_serves_the_account():
    asked: list[tuple[str, str]] = []

    class Inbox(OtpReader):
        def __init__(self, name: str) -> None:
            self.name = name

        async def get_otp(self, address, *, timeout=60, not_before=None):
            asked.append((self.name, address))
            return "123456"

    reader = RoutedOtpReader(Inbox("main"), [(_spec(2, "second@gmail.com", ("second@gmail.com",)), Inbox("second"))])

    assert asyncio.run(reader.get_otp("second@gmail.com")) == "123456"
    asyncio.run(reader.get_otp("other@gmail.com"))
    assert asked == [("second", "second@gmail.com"), ("main", "other@gmail.com")]


def test_the_settings_page_lists_the_numbered_inboxes_without_passwords(monkeypatch):
    from app.api import settings as settings_api

    monkeypatch.setenv("ORDERING_OTP_IMAP_USER_2", "second@gmail.com")
    monkeypatch.setenv("ORDERING_OTP_IMAP_PASSWORD_2", "secret-app-password")
    monkeypatch.setenv("ORDERING_OTP_IMAP_HOST_2", "imap.gmail.com")
    application = FastAPI()
    application.include_router(settings_api.router)
    token = create_access_token("tester", role="admin")
    client = TestClient(application, headers={"Authorization": f"Bearer {token}"})

    response = client.get("/settings")

    assert response.status_code == 200
    assert response.json()["otp_extra_inboxes"] == [
        {"number": 2, "user": "second@gmail.com", "host": "imap.gmail.com", "serves": ["second@gmail.com"]}
    ]
    assert "secret-app-password" not in response.text
