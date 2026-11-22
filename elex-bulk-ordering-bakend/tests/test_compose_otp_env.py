"""Compose hands the OTP inbox and the archive retention to the API from the
project's .env (27 Oct 2026). The inbox used to be written into
docker-compose.yml as blanks, so on a server it could only be set from the
Settings page, and the retention days were not passed at all. Each is now its
own ``${NAME:-default}``; the typed ones (port, SSL, days) keep the app's own
defaults when left out, because a blank one would not parse and the API would
not start."""

import pytest

from app.config import Settings
from tests.test_compose_mail_env import _FROM_ENV, _backend_env

OTP_AND_RETENTION = {
    "ORDERING_OTP_IMAP_HOST": "otp_imap_host",
    "ORDERING_OTP_IMAP_PORT": "otp_imap_port",
    "ORDERING_OTP_IMAP_USER": "otp_imap_user",
    "ORDERING_OTP_IMAP_PASSWORD": "otp_imap_password",
    "ORDERING_OTP_IMAP_MAILBOX": "otp_imap_mailbox",
    "ORDERING_OTP_IMAP_USE_SSL": "otp_imap_use_ssl",
    "ORDERING_ARCHIVE_RETENTION_DAYS": "archive_retention_days",
}
#: The second inbox, read from the environment by name (app.automation.otp):
#: blank by default, which reads as "no second inbox", without a warning.
SECOND_INBOX = ("ORDERING_OTP_IMAP_USER_2", "ORDERING_OTP_IMAP_PASSWORD_2", "ORDERING_OTP_IMAP_FOR_2")


@pytest.mark.parametrize("name", SECOND_INBOX)
def test_the_second_inbox_is_taken_from_the_env_file_blank_by_default(name):
    written = _FROM_ENV.match(_backend_env().get(name, ""))
    assert written is not None and written["name"] == name and written["default"] == "", name


def test_left_blank_the_second_inbox_is_none_and_says_nothing(monkeypatch, caplog):
    from app.automation import otp

    env = _backend_env()
    blank = {name: _FROM_ENV.match(env[name])["default"] for name in SECOND_INBOX}
    settings = Settings(_env_file=None)
    with caplog.at_level("WARNING"):
        assert otp.extra_otp_inboxes(settings, environ=blank, use_env_file=False) == []
    assert not [r for r in caplog.records if "inbox" in r.getMessage().lower()]


@pytest.mark.parametrize("name", sorted(OTP_AND_RETENTION))
def test_each_is_taken_from_the_env_file_never_written_in(name):
    written = _FROM_ENV.match(_backend_env().get(name, ""))
    assert written is not None and written["name"] == name, f"{name} does not reach the api container from the .env"


def test_left_out_they_keep_the_apps_own_defaults_and_the_api_starting(monkeypatch):
    env = _backend_env()
    for name in OTP_AND_RETENTION:
        monkeypatch.setenv(name, _FROM_ENV.match(env[name])["default"])
    settings = Settings(_env_file=None)  # a blank port, switch or day count fails right here
    for name, field in OTP_AND_RETENTION.items():
        assert getattr(settings, field) == Settings.model_fields[field].default, name
