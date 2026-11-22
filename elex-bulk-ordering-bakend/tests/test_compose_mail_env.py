"""Compose hands the mail settings to the API (spec C-13, FR-059).

The "Forgot password?" link needs ORDERING_SMTP_* and ORDERING_APP_URL inside
the api container, and a container gets only the variables compose names: the
image copies no .env (backend/Dockerfile). So the environment the api, worker
and migrate services share (``&backend_env``) names each one, taken from the
project's .env as ``${NAME:-default}``. Left out, the host, sender and app
address are blank, which keeps the link off; the port and STARTTLS keep the
app's own defaults, because a blank one would not parse and the API would not
start at all.
"""

import re
from pathlib import Path

from app.config import Settings
from app.core import mailer

from tests import layout

COMPOSE = layout.COMPOSE

MAIL_SETTINGS = (
    "ORDERING_SMTP_HOST",
    "ORDERING_SMTP_PORT",
    "ORDERING_SMTP_USER",
    "ORDERING_SMTP_PASSWORD",
    "ORDERING_SMTP_FROM",
    "ORDERING_SMTP_STARTTLS",
    "ORDERING_APP_URL",
)

#: ``${NAME:-default}``: the value compose takes from the .env or the shell.
_FROM_ENV = re.compile(r"^\$\{(?P<name>[A-Z0-9_]+):-(?P<default>[^}]*)\}$")


def _uncommented() -> list[str]:
    """The compose file's lines, comments and blank lines stripped."""
    lines = [raw.split("#", 1)[0].rstrip() for raw in COMPOSE.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line.strip()]


def _backend_env() -> dict[str, str]:
    """NAME → value as written, in the environment block the backend services share."""
    lines = _uncommented()
    start = next(i for i, line in enumerate(lines) if line.strip() == "environment: &backend_env")
    indent = len(lines[start]) - len(lines[start].lstrip())
    env: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if len(line) - len(line.lstrip()) <= indent:
            break  # the block ends where its indent does
        name, _, value = line.strip().partition(":")
        env[name] = value.strip()
    return env


def test_the_shared_backend_environment_takes_every_mail_setting_from_the_env_file():
    env = _backend_env()

    for name in MAIL_SETTINGS:
        assert name in env, f"{name} never reaches the api container"
        written = _FROM_ENV.match(env[name])
        # Its own variable, never a value written into the compose file.
        assert written is not None and written["name"] == name, (name, env[name])
    shared = [line.strip() for line in _uncommented()]
    assert shared.count("environment: *backend_env") == 2  # the migrate and worker services


def test_left_out_they_keep_the_reset_link_off_and_the_api_starting(monkeypatch):
    """What the api container gets when the .env names none of them."""
    env = _backend_env()
    for name in MAIL_SETTINGS:
        monkeypatch.setenv(name, _FROM_ENV.match(env[name])["default"])

    settings = Settings(_env_file=None)  # a blank port or STARTTLS fails right here

    for field in ("smtp_port", "smtp_starttls"):
        assert getattr(settings, field) == Settings.model_fields[field].default
    assert (settings.smtp_host, settings.smtp_from, settings.app_url) == ("", "", "")
    monkeypatch.setattr(mailer, "get_settings", lambda: settings)
    assert mailer.mail_configured() is False


def test_set_in_the_env_file_they_turn_the_reset_link_on(monkeypatch):
    """What compose passes once the .env names them (made-up values)."""
    values = {
        "ORDERING_SMTP_HOST": "smtp.example.test",
        "ORDERING_SMTP_PORT": "465",
        "ORDERING_SMTP_FROM": "orders@example.test",
        "ORDERING_SMTP_STARTTLS": "false",
        "ORDERING_APP_URL": "https://orders.example.test",
    }
    env = _backend_env()
    for name in MAIL_SETTINGS:
        assert _FROM_ENV.match(env[name])["name"] == name
        monkeypatch.setenv(name, values.get(name, ""))

    settings = Settings(_env_file=None)

    assert (settings.smtp_host, settings.smtp_port, settings.smtp_starttls) == ("smtp.example.test", 465, False)
    monkeypatch.setattr(mailer, "get_settings", lambda: settings)
    assert mailer.mail_configured() is True
