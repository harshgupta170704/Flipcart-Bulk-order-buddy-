"""The inbox connections check the mail server's certificate.

``imaplib.IMAP4_SSL`` takes any certificate at all unless it is handed a
context, so whoever sat between the app and the mail server could collect the
inbox password. The OTP reader and the Settings page's inbox test now hand it
one that checks the certificate and the host name — and that has certificate
authorities to check against even on a Python with no list of its own (the
python.org build on the owner's laptop). The plain, no-SSL path is as it was.
Nothing here connects anywhere: imaplib is replaced by recorders."""

from __future__ import annotations

import imaplib
import logging
import ssl
import sys

import pytest

from app.api import settings as settings_api
from app.automation.otp import ImapOtpReader, imap_ssl_context


def _checks_the_server(context) -> bool:
    return (
        isinstance(context, ssl.SSLContext)
        and context.verify_mode == ssl.CERT_REQUIRED
        and context.check_hostname is True
    )


class _Connection:
    """A logged-in mailbox holding three messages; connects nowhere."""

    def __init__(self, record: list) -> None:
        self.record = record

    def login(self, user, password):
        self.record.append(("login", user))
        return "OK", [b"Logged in"]

    def select(self, mailbox, readonly=False):
        return "OK", [b"3"]

    def logout(self):
        return "BYE", [b""]


@pytest.fixture
def opened(monkeypatch) -> list[tuple]:
    """How imaplib was asked to connect, recorded in place of connecting."""
    record: list[tuple] = []
    login_error = imaplib.IMAP4.error

    def over_ssl(host, port=993, *, ssl_context=None, timeout=None):
        record.append(("IMAP4_SSL", host, port, ssl_context, timeout))
        return _Connection(record)

    def plain(host, port=143, timeout=None):
        record.append(("IMAP4", host, port, timeout))
        return _Connection(record)

    plain.error = login_error  # the inbox test catches imaplib.IMAP4.error
    monkeypatch.setattr(imaplib, "IMAP4_SSL", over_ssl)
    monkeypatch.setattr(imaplib, "IMAP4", plain)
    return record


def _refuse_the_certificate(monkeypatch) -> None:
    def over_ssl(host, port=993, *, ssl_context=None, timeout=None):
        raise ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", over_ssl)


# --- the context ------------------------------------------------------------------------
def test_the_inbox_context_checks_the_certificate_and_the_host_name():
    context = imap_ssl_context()

    assert _checks_the_server(context)
    assert context.cert_store_stats()["x509_ca"] > 0


def test_it_has_authorities_even_where_python_has_no_list_of_its_own(monkeypatch):
    """The python.org Python on macOS finds no certificate authorities until
    its "Install Certificates" step is run, and SSL_CERT_FILE is not set in
    every shell: certifi's list is there either way."""
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, purpose=ssl.Purpose.SERVER_AUTH: None)

    context = imap_ssl_context()

    assert _checks_the_server(context)
    assert context.cert_store_stats()["x509_ca"] > 0


def test_without_certifi_the_system_list_is_used_and_still_checked(monkeypatch):
    monkeypatch.setitem(sys.modules, "certifi", None)  # "import certifi" now fails

    assert _checks_the_server(imap_ssl_context())


def test_a_broken_certifi_falls_back_to_the_system_list_without_raising(monkeypatch, tmp_path):
    import certifi

    monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "missing-cacert.pem"))

    assert _checks_the_server(imap_ssl_context())


# --- the OTP reader ---------------------------------------------------------------------
def test_the_otp_reader_hands_imaplib_a_context_that_checks_the_server(opened):
    ImapOtpReader("imap.example.com", 993, "catchall@example.com", "app-password")._connect()

    kind, host, port, context, _timeout = opened[0]
    assert (kind, host, port) == ("IMAP4_SSL", "imap.example.com", 993)
    assert _checks_the_server(context)
    assert opened[1] == ("login", "catchall@example.com")


def test_the_otp_reader_without_ssl_connects_as_it_did(opened):
    ImapOtpReader("imap.example.com", 143, "catchall@example.com", "app-password", use_ssl=False)._connect()

    assert opened == [("IMAP4", "imap.example.com", 143, None), ("login", "catchall@example.com")]


def test_a_failed_certificate_check_is_logged_once_per_login_not_per_poll(monkeypatch, caplog):
    """The login would only say it found no code; the log says why, once."""
    _refuse_the_certificate(monkeypatch)
    reader = ImapOtpReader("imap.example.com", 993, "catchall@example.com", "app-password")

    with caplog.at_level(logging.ERROR, logger="ordering.otp"):
        assert reader._fetch_code("buyer@example.com") is None
        assert reader._fetch_code("buyer@example.com") is None

    said = [r.getMessage() for r in caplog.records if "certificate could not be verified" in r.getMessage()]
    assert len(said) == 1 and "imap.example.com:993" in said[0]
    assert "app-password" not in caplog.text


# --- the Settings page's inbox test -----------------------------------------------------
def test_the_inbox_test_hands_imaplib_a_context_that_checks_the_server(opened):
    result = settings_api._probe_inbox("imap.example.com", 993, "catchall@example.com", "app-password", "INBOX", True)

    assert (result.ok, result.message_count) == (True, 3)
    kind, host, port, context, timeout = opened[0]
    assert (kind, host, port, timeout) == ("IMAP4_SSL", "imap.example.com", 993, 15)
    assert _checks_the_server(context)


def test_the_inbox_test_without_ssl_connects_as_it_did(opened):
    result = settings_api._probe_inbox("imap.example.com", 143, "catchall@example.com", "app-password", "INBOX", False)

    assert result.ok is True
    assert opened == [("IMAP4", "imap.example.com", 143, 15), ("login", "catchall@example.com")]


def test_a_certificate_that_fails_the_check_is_named_and_no_password_is_sent(monkeypatch):
    _refuse_the_certificate(monkeypatch)

    result = settings_api._probe_inbox("imap.example.com", 993, "catchall@example.com", "app-password", "INBOX", True)

    assert result.ok is False
    assert "certificate could not be verified" in result.message
    assert "password was not sent" in result.message
    assert "app-password" not in result.message
