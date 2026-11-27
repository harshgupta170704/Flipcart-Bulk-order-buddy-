"""11 Nov 2026: two ways the Settings page could break every run after it.

* The proxy address is shown with its sign-in masked ("http://***@host") and
  the Performance card sends all its fields on Save, so changing the
  concurrency wrote the mask over the real address: every run after that
  failed at the proxy, and nothing said why.
* No setting had an upper bound, so a slip (50 typed as 5000) was saved.
"""

from __future__ import annotations

import pytest

from app.core import app_settings

REAL = "http://buyer:s3cret@proxy.example:8000"


def test_a_proxy_sent_back_as_it_was_shown_keeps_the_saved_sign_in(db):
    app_settings.set_overrides(db, {"proxy_server": REAL})
    shown = app_settings.public_view(app_settings.get_effective(db))["proxy_server"]
    assert shown == "http://***@proxy.example:8000"

    # The Performance card's Save, as the page sent it: every field, the proxy as shown.
    app_settings.set_overrides(db, {"max_concurrency": 6, "proxy_server": shown, "sync_every_hours": 0})

    effective = app_settings.get_effective(db)
    assert effective["proxy_server"] == REAL
    assert effective["max_concurrency"] == 6


def test_a_masked_proxy_that_is_not_the_saved_one_is_refused_with_words(db):
    app_settings.set_overrides(db, {"proxy_server": REAL})
    with pytest.raises(app_settings.SettingsValidationError) as refused:
        app_settings.set_overrides(db, {"proxy_server": "http://***@other.example:9000"})
    assert refused.value.key == "proxy_server" and "type the whole address again" in refused.value.message
    assert app_settings.get_effective(db)["proxy_server"] == REAL


def test_a_new_proxy_and_no_proxy_are_both_saved(db):
    app_settings.set_overrides(db, {"proxy_server": REAL})
    app_settings.set_overrides(db, {"proxy_server": "socks5://10.0.0.5:1080"})
    assert app_settings.get_effective(db)["proxy_server"] == "socks5://10.0.0.5:1080"
    app_settings.set_overrides(db, {"proxy_server": "proxy.example:3128"})  # the browser's short form
    assert app_settings.get_effective(db)["proxy_server"] == "proxy.example:3128"
    app_settings.set_overrides(db, {"proxy_server": ""})
    assert app_settings.get_effective(db)["proxy_server"] == ""


@pytest.mark.parametrize("value", ["not a proxy", "ftp://host:21", "http://", "http://host:port"])
def test_what_is_not_a_proxy_address_is_refused(db, value):
    with pytest.raises(app_settings.SettingsValidationError) as refused:
        app_settings.set_overrides(db, {"proxy_server": value})
    assert refused.value.key == "proxy_server"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_concurrency", app_settings.MAX_CONCURRENCY_CEILING + 1),
        ("max_concurrency", 5000),
        ("otp_poll_interval", 0),
        ("otp_poll_interval", 100_000),
        ("otp_fetch_timeout", 1),
        ("otp_fetch_timeout", 601),
    ],
)
def test_a_number_outside_what_can_work_is_refused(db, key, value):
    with pytest.raises(app_settings.SettingsValidationError) as refused:
        app_settings.set_overrides(db, {key: value})
    assert refused.value.key == key
    assert "or more" in refused.value.message or "or less" in refused.value.message


def test_the_limits_themselves_are_allowed(db):
    saved = app_settings.set_overrides(
        db,
        {
            "max_concurrency": app_settings.MAX_CONCURRENCY_CEILING,
            "otp_poll_interval": 60,
            "otp_fetch_timeout": 5,
        },
    )
    assert (saved["max_concurrency"], saved["otp_poll_interval"], saved["otp_fetch_timeout"]) == (50, 60, 5)


def test_asking_the_command_line_for_help_never_opens_the_database(monkeypatch, capsys):
    """``python -m app.cli --help`` used to set the database up first: a
    backup, the start-up repairs, on the real data, for a help text."""
    import sys

    from app import cli

    opened = []
    monkeypatch.setattr(cli, "init_db", lambda: opened.append("init_db"))
    monkeypatch.setattr(sys, "argv", ["app.cli", "--help"])

    with pytest.raises(SystemExit) as stopped:
        cli.main()

    assert stopped.value.code == 0
    assert opened == []
    assert "usage:" in capsys.readouterr().out
