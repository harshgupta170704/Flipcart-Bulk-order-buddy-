"""Compose hands the API every setting docs/DEPLOY.md §2 tells the owner to set
in the project's .env (review of 27 Oct 2026).

A container gets only the variables compose names: the image copies no .env.
Seven rows of DEPLOY §2 — the order-tracking timers, the sign-in throttle, the
log level, the password work factor and the proxy — were named nowhere, so on
a server a value set for them in the .env never reached the API. Each is now
its own ``${NAME:-default}`` with the app's own default, so leaving one out
changes nothing. Inbox 2's own host reaches it too: the Settings page asks for
it before it lets the first inbox move to another provider (app.api.settings).
Whatever else §2 lists needs a docker-compose.override.yml, and §2 says so.
"""

import re
from pathlib import Path

import pytest

from app.config import Settings
from tests import layout
from tests.test_compose_mail_env import COMPOSE, _FROM_ENV, _backend_env, _uncommented

DEPLOY = layout.DOCS / "DEPLOY.md"

#: The rows compose used to leave out, and the setting each one is.
FROM_THE_ENV_FILE = {
    "ORDERING_TRACK_EVERY_MINUTES": "track_every_minutes",
    "ORDERING_OUT_FOR_DELIVERY_MINUTES": "out_for_delivery_minutes",
    "ORDERING_LOGIN_MAX_FAILURES_PER_EMAIL": "login_max_failures_per_email",
    "ORDERING_LOGIN_MAX_FAILURES_PER_ADDRESS": "login_max_failures_per_address",
    "ORDERING_LOG_LEVEL": "log_level",
    "ORDERING_PBKDF2_ITERATIONS": "pbkdf2_iterations",
    "ORDERING_PROXY_SERVER": "proxy_server",
}
#: A full setting name; one followed by ``*`` (``ORDERING_OTP_IMAP_*``) is a
#: family, which tests/test_compose_otp_env.py covers name by name.
_NAME = re.compile(r"ORDERING_[A-Z0-9_]*[A-Z0-9](?![A-Z0-9_]*\*)")


@pytest.mark.parametrize("name", sorted(FROM_THE_ENV_FILE))
def test_each_reaches_the_api_from_the_env_file(name):
    written = _FROM_ENV.match(_backend_env().get(name, ""))
    assert written is not None and written["name"] == name, f"{name} does not reach the api container from the .env"


@pytest.mark.parametrize("name", sorted(FROM_THE_ENV_FILE))
def test_left_out_each_is_the_apps_own_default(name):
    written = _FROM_ENV.match(_backend_env().get(name, ""))
    assert written is not None, name
    assert written["default"] == str(Settings.model_fields[FROM_THE_ENV_FILE[name]].default), name


def test_left_out_they_change_nothing_and_the_api_starts(monkeypatch):
    env = _backend_env()
    for name in FROM_THE_ENV_FILE:
        monkeypatch.setenv(name, _FROM_ENV.match(env[name])["default"])
    settings = Settings(_env_file=None)  # a default that did not parse would fail right here
    for name, field in FROM_THE_ENV_FILE.items():
        assert getattr(settings, field) == Settings.model_fields[field].default, name


def test_inbox_2_can_name_its_own_host_and_left_blank_takes_the_first_inboxs(caplog):
    """The host the Settings page tells the admin to set before moving the
    first inbox (app.api.settings) must reach the container, or that page
    refuses the move for ever. Left blank, inbox 2 signs in where it always did."""
    from app.automation import otp

    written = _FROM_ENV.match(_backend_env().get("ORDERING_OTP_IMAP_HOST_2", ""))
    assert written is not None and written["name"] == "ORDERING_OTP_IMAP_HOST_2" and written["default"] == ""

    settings = Settings(_env_file=None, otp_imap_host="imap.first-provider.test")
    second = {"ORDERING_OTP_IMAP_USER_2": "second@gmail.example", "ORDERING_OTP_IMAP_PASSWORD_2": "made-up-app-password"}
    with caplog.at_level("WARNING"):
        blank = otp.extra_otp_inboxes(settings, environ={**second, "ORDERING_OTP_IMAP_HOST_2": ""}, use_env_file=False)
    own = otp.extra_otp_inboxes(
        settings, environ={**second, "ORDERING_OTP_IMAP_HOST_2": "imap.gmail.example"}, use_env_file=False
    )

    assert [spec.host for spec in blank] == ["imap.first-provider.test"]
    assert not [record for record in caplog.records if "inbox" in record.getMessage().lower()]
    assert [spec.host for spec in own] == ["imap.gmail.example"]


def _section_2() -> str:
    text = DEPLOY.read_text(encoding="utf-8")
    start = text.index("## 2. Configuration")
    return text[start : text.index("\n## 3.", start)]


def _web_env() -> set[str]:
    """The names in the web service's environment block."""
    lines = _uncommented()
    start = next(i for i, line in enumerate(lines) if line.strip() == "web:")
    names: set[str] = set()
    for line in lines[start + 1 :]:
        if not line.startswith("    "):
            break  # the next service, or the volumes
        name, sep, _value = line.strip().partition(":")
        if sep and name.startswith("ORDERING_"):
            names.add(name)
    return names


def test_every_row_of_deploy_section_2_reaches_a_container_or_says_how():
    """A row the containers never get, and nothing saying so, is how seven
    settings went missing on a server unnoticed."""
    section = _section_2()
    rows = [line for line in section.splitlines() if line.startswith("| `ORDERING_")]
    named = {name for row in rows for name in _NAME.findall(row.split("|")[1])}
    paragraphs = [paragraph for paragraph in section.split("\n\n") if not paragraph.lstrip().startswith("|")]
    override = " ".join(paragraph for paragraph in paragraphs if "docker-compose.override.yml" in paragraph)
    compose = COMPOSE.read_text(encoding="utf-8")
    assert len(named) > 15  # the table was read

    missing = [
        name
        for name in sorted(named)
        if name not in _backend_env()  # the api, worker and migrate services
        and name not in _web_env()
        and f"${{{name}:?" not in compose  # read by compose itself (the database password)
        and f"`{name}`" not in override
    ]

    assert missing == [], f"DEPLOY §2 lists {missing}, which no container gets, and does not say how to pass them"
    assert "`ORDERING_DEV_MODE`" in override and "`ORDERING_EXPOSE_DOCS`" in override
