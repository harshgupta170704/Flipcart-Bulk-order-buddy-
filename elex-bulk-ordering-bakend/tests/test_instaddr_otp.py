"""Tests for InstAddr (m.kuku.lu) OTP parsing, freshness, and routing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.automation.instaddr import (
    InstAddrMail,
    InstAddrOtpReader,
    _mail_within_window,
    _row_matches_sender,
    _row_targets_address,
    parse_age_seconds,
)
from app.automation.otp import ImapOtpReader, NullOtpReader, OtpReader, RoutedOtpReader, get_otp_reader


def test_parse_age_seconds():
    assert parse_age_seconds("20:10 (just now) | x") == 0.0
    assert parse_age_seconds("20:10 (25sec) | x") == 25.0
    assert parse_age_seconds("20:10 (2min) | x") == 120.0
    assert parse_age_seconds("19:17 (1hour) | x") == 3600.0
    assert parse_age_seconds("19:17 (2days) | x") == 172800.0
    assert parse_age_seconds("no age here") is None


def test_row_targets_address_exact_boundary():
    meta = "19:17 (1hour) | Flipkart.com <noreply@rmo.flipkart.com> » earweeput@catbar.net"
    assert _row_targets_address(meta, "earweeput@catbar.net") is True
    assert _row_targets_address(meta, "EARWEEPUT@CATBAR.NET") is True
    assert _row_targets_address(meta, "weeput@catbar.net") is False
    assert _row_targets_address(meta, "other@catbar.net") is False


def test_row_matches_sender():
    mail = InstAddrMail(
        mail_id="1",
        subject="Flipkart Account - 697415 is your verification code",
        meta="19:17 (1hour) | Flipkart.com <noreply@rmo.flipkart.com> » earweeput@catbar.net",
    )
    assert _row_matches_sender(mail, ("flipkart",)) is True
    unrelated = InstAddrMail(
        mail_id="2",
        subject="Random passcode 112233",
        meta="19:17 (10sec) | Alerts <alerts@example.com> » earweeput@catbar.net",
    )
    assert _row_matches_sender(unrelated, ("flipkart",)) is False


def test_mail_within_window_rejects_old_otps_and_accepts_fresh():
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    now_local = datetime(2026, 11, 18, 20, 40, 15, tzinfo=local_tz)
    now_utc = now_local.astimezone(timezone.utc)
    # login.py stamps requested_at = now - 1 minute
    not_before = (now_local - timedelta(minutes=1)).astimezone(timezone.utc)
    date_label = "Wednesday 18th of November 2026"

    old_hour = InstAddrMail(
        mail_id="1",
        subject="Flipkart Account - 697415 is your verification code",
        meta="19:17 (1hour) | Flipkart.com <noreply@rmo.flipkart.com> » earweeput@catbar.net",
        date_label=date_label,
    )
    assert _mail_within_window(old_hour, now=now_utc, not_before=not_before, max_age_minutes=15) is False

    old_minutes = InstAddrMail(
        mail_id="2",
        subject="Flipkart Account - 111222 is your verification code",
        meta="20:36 (4min) | Flipkart.com <noreply@rmo.flipkart.com> » earweeput@catbar.net",
        date_label=date_label,
    )
    assert _mail_within_window(old_minutes, now=now_utc, not_before=not_before, max_age_minutes=15) is False

    fresh = InstAddrMail(
        mail_id="3",
        subject="Flipkart Account - 482910 is your verification code",
        meta="20:40 (10sec) | Flipkart.com <noreply@rmo.flipkart.com> » earweeput@catbar.net",
        date_label=date_label,
    )
    assert _mail_within_window(fresh, now=now_utc, not_before=not_before, max_age_minutes=15) is True


def _settings(**over):
    base = dict(
        otp_imap_host="imap.gmail.com",
        otp_imap_port=993,
        otp_imap_user="main@gmail.com",
        otp_imap_password="pw",
        otp_imap_mailbox="INBOX",
        otp_imap_use_ssl=True,
        otp_poll_interval=3,
        otp_sender_filter="flipkart",
        otp_since_days=1,
        otp_max_age_minutes=15,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_get_otp_reader_routes_instaddr_domains_and_keeps_gmail_on_imap():
    reader = get_otp_reader(
        _settings(),
        extras=[],
        instaddr_state={"cookies": [{"name": "cookie_sessionhash", "value": "x"}]},
        instaddr_domains=("catbar.net", "savatv.com"),
    )
    assert isinstance(reader, RoutedOtpReader)
    cat_reader, cat_label = reader.reader_for("earweeput@catbar.net")
    assert isinstance(cat_reader, InstAddrOtpReader)
    assert isinstance(cat_reader, OtpReader)
    assert "@catbar.net" in cat_label

    sav_reader, _ = reader.reader_for("dyebigfad@savatv.com")
    assert isinstance(sav_reader, InstAddrOtpReader)

    gmail_reader, gmail_label = reader.reader_for("buyer@gmail.com")
    assert isinstance(gmail_reader, ImapOtpReader)
    assert gmail_label == "the main inbox"


def test_instaddr_works_even_when_imap_not_configured():
    reader = get_otp_reader(
        _settings(otp_imap_host="", otp_imap_user=""),
        extras=[],
        instaddr_state={"cookies": [{"name": "cookie_sessionhash", "value": "x"}]},
        instaddr_domains=("catbar.net",),
    )
    assert isinstance(reader, RoutedOtpReader)
    assert isinstance(reader.default, NullOtpReader)
    cat_reader, _ = reader.reader_for("earweeput@catbar.net")
    assert isinstance(cat_reader, InstAddrOtpReader)
