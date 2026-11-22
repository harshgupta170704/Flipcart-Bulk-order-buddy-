"""``python -m app.cli test-mail <to>``: proves the ORDERING_SMTP_* settings
before anyone needs a reset link — prints them without the password, sends one
mail with the real mailer, and explains a refusal in the operator's words."""

from __future__ import annotations

import asyncio
import smtplib
from types import SimpleNamespace

import pytest

from app import cli
from app.config import get_settings
from app.core import mailer

SECRET = "app-password-secret-value"


def _configured(monkeypatch, **over):
    settings = get_settings()
    values = dict(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_user="orders@example.com",
        smtp_password=SECRET,
        smtp_from="orders@example.com",
        smtp_starttls=True,
        app_url="https://orders.example.com",
    )
    values.update(over)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    return settings


def _run(to: str = "me@example.com") -> None:
    asyncio.run(cli.cmd_test_mail(SimpleNamespace(to=to)))


def test_a_test_mail_is_sent_with_the_settings_and_the_password_is_never_printed(monkeypatch, capsys):
    _configured(monkeypatch)
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(mailer, "send_mail", lambda to, subject, body: sent.append((to, subject, body)))

    _run()

    out = capsys.readouterr().out
    assert sent and sent[0][0] == "me@example.com" and "test mail" in sent[0][1].lower()
    assert "smtp.gmail.com" in out and "587" in out and "orders@example.com" in out and "STARTTLS on" in out
    assert SECRET not in out and SECRET not in sent[0][2]
    assert "sent a test mail to me@example.com" in out


def test_a_missing_setting_is_named_and_nothing_is_sent(monkeypatch, capsys):
    _configured(monkeypatch, smtp_host="", app_url="")
    monkeypatch.setattr(mailer, "send_mail", lambda *args: pytest.fail("must not send"))

    with pytest.raises(SystemExit) as stop:
        _run()

    message = str(stop.value)
    assert "ORDERING_SMTP_HOST" in message and "ORDERING_APP_URL" in message
    assert "ORDERING_SMTP_FROM" not in message  # that one is set
    assert SECRET not in message and SECRET not in capsys.readouterr().out


def test_a_refused_sign_in_is_explained_as_an_app_password_problem(monkeypatch):
    _configured(monkeypatch)

    def refuse(to, subject, body):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    monkeypatch.setattr(mailer, "send_mail", refuse)

    with pytest.raises(SystemExit) as stop:
        _run()

    message = str(stop.value)
    assert message.startswith("not sent:") and "App Password" in message and "535" in message
    assert SECRET not in message


def test_nothing_answering_at_the_host_points_at_host_and_port(monkeypatch):
    _configured(monkeypatch, smtp_host="smtp.example.invalid")

    def refuse(to, subject, body):
        raise ConnectionRefusedError(61, "Connection refused")

    monkeypatch.setattr(mailer, "send_mail", refuse)

    with pytest.raises(SystemExit) as stop:
        _run()

    assert "ORDERING_SMTP_HOST" in str(stop.value) and "587" in str(stop.value)


def test_the_command_is_wired_into_the_parser():
    args = cli.build_parser().parse_args(["test-mail", "someone@example.com"])
    assert args.func is cli.cmd_test_mail and args.to == "someone@example.com"
