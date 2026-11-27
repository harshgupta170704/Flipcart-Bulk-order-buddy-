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
    assert reader.can_read("earweeput@catbar.net") is True
    assert reader.can_read("buyer@gmail.com") is False
    assert NullOtpReader().can_read("earweeput@catbar.net") is False


def test_order_job_auto_logins_zero_session_catbar_account_and_skips_unrouted(monkeypatch):
    import asyncio
    import contextlib

    from app.automation.login import NotRegistered
    from app.automation.ordering import PAYMENT_COD
    from app.core import session_store
    from app.db.base import SessionLocal
    from app.db.models import Account, AccountStatus, Address, Job, JobStatus, JobType, OrderStatus
    from app.jobs import tasks

    @contextlib.asynccontextmanager
    async def _fake_browser_page(*, headless=True, mobile=False, proxy=None, cookies=None, lean=True, pool=None):
        yield SimpleNamespace(), SimpleNamespace(url="https://www.flipkart.com/", _cookies=cookies)

    class _FakePool:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    routed_reader = get_otp_reader(
        _settings(otp_imap_host="", otp_imap_user=""),
        extras=[],
        instaddr_state={"cookies": [{"name": "cookie_sessionhash", "value": "x"}]},
        instaddr_domains=("catbar.net",),
    )

    logged_in_emails: list[str] = []
    ordered_cookies: list[list[dict]] = []

    async def _fake_run_login(page, email, otp_reader, **kwargs):
        logged_in_emails.append(email)
        if email.startswith("missing"):
            raise NotRegistered("not registered")
        return [{"name": "S", "value": "fresh-catbar-cookie", "domain": ".flipkart.com", "path": "/"}]

    async def _fake_run_order(page, **kwargs):
        ordered_cookies.append(page._cookies)
        return {"status": OrderStatus.placed, "order_id": "OD-CATBAR-1", "amount": 999.0, "qty": 1}

    monkeypatch.setattr(tasks, "browser_page", _fake_browser_page)
    monkeypatch.setattr(tasks, "_pool_for", lambda *a, **kw: _FakePool())
    monkeypatch.setattr(tasks, "get_otp_reader", lambda settings: routed_reader)
    monkeypatch.setattr(tasks, "run_login", _fake_run_login)
    monkeypatch.setattr(tasks, "run_order", _fake_run_order)

    with SessionLocal() as db:
        addr = Address(
            label="WH",
            name="Buyer",
            phone="9876543210",
            pincode="560001",
            line1="1 MG Road",
            city="Bengaluru",
            state="Karnataka",
            is_default=True,
        )
        cat_ok = Account(email="freshuser@catbar.net", status=AccountStatus.new)
        cat_missing = Account(email="missing@catbar.net", status=AccountStatus.new)
        gmail_unrouted = Account(email="nosession@gmail.com", status=AccountStatus.new)
        db.add_all([addr, cat_ok, cat_missing, gmail_unrouted])
        db.flush()
        job = Job(type=JobType.order, status=JobStatus.running, total_items=3)
        db.add(job)
        db.commit()
        ids = [cat_ok.id, cat_missing.id, gmail_unrouted.id]
        job_id = job.id
        addr_id = addr.id

    asyncio.run(
        tasks.run_order_job(
            job_id,
            ids,
            product_link="https://www.flipkart.com/p/item",
            qty=1,
            target=2000.0,
            payment_mode=PAYMENT_COD,
            address_id=addr_id,
        )
    )

    assert sorted(logged_in_emails) == ["freshuser@catbar.net", "missing@catbar.net"]
    assert len(ordered_cookies) == 1
    assert ordered_cookies[0][0]["value"] == "fresh-catbar-cookie"

    with SessionLocal() as db:
        refreshed_ok = db.get(Account, ids[0])
        refreshed_missing = db.get(Account, ids[1])
        refreshed_gmail = db.get(Account, ids[2])
        assert refreshed_ok.status == AccountStatus.session_valid
        assert session_store.load_cookies(db, refreshed_ok)[0]["value"] == "fresh-catbar-cookie"
        assert refreshed_missing.status == AccountStatus.not_registered
        assert refreshed_gmail.status == AccountStatus.new
        done_job = db.get(Job, job_id)
        assert done_job.done_items == 3
        assert done_job.failed_items == 1
        assert done_job.skipped_items == 1



# --- several InstAddr accounts (24 Nov 2026: @apmail.org is on a second one) ---

_STATE_A = {"cookies": [{"name": "cookie_sessionhash", "value": "a"}]}
_STATE_B = {"cookies": [{"name": "cookie_sessionhash", "value": "b"}]}


def test_each_instaddr_account_reads_its_own_domain():
    reader = get_otp_reader(
        _settings(),
        extras=[],
        instaddr_accounts=[("main", _STATE_A, ("catbar.net",)), ("vikramten", _STATE_B, ("apmail.org",))],
    )
    assert isinstance(reader, RoutedOtpReader)
    cat_reader, cat_label = reader.reader_for("earweeput@catbar.net")
    ap_reader, ap_label = reader.reader_for("prygemcar@apmail.org")
    assert isinstance(cat_reader, InstAddrOtpReader) and cat_reader.state is _STATE_A
    assert isinstance(ap_reader, InstAddrOtpReader) and ap_reader.state is _STATE_B
    assert "vikramten" in ap_label and "@apmail.org" in ap_label
    assert reader.can_read("prygemcar@apmail.org") is True
    gmail_reader, _ = reader.reader_for("buyer@gmail.com")
    assert isinstance(gmail_reader, ImapOtpReader)


def test_a_domain_two_accounts_hold_is_read_from_the_first():
    reader = get_otp_reader(
        _settings(),
        extras=[],
        instaddr_accounts=[("main", _STATE_A, ("catbar.net",)), ("other", _STATE_B, ("catbar.net", "apmail.org"))],
    )
    cat_reader, _ = reader.reader_for("earweeput@catbar.net")
    assert cat_reader.state is _STATE_A


def test_saved_accounts_keep_the_first_one_where_it_was(db):
    from app.automation import instaddr

    assert instaddr.load_accounts(db) == []
    instaddr.save_account(db, None, _STATE_A, ["catbar.net", "nekosan.uk"])
    instaddr.save_account(db, "vikramten", _STATE_B, ["@APMAIL.org"])
    # the first account is still read from the original keys, as before
    assert instaddr.load_state(db) == _STATE_A
    assert instaddr.load_domains(db) == ("catbar.net", "nekosan.uk")
    accounts = instaddr.load_accounts(db)
    assert [(a["name"], a["domains"]) for a in accounts] == [
        ("main", ["catbar.net", "nekosan.uk"]),
        ("vikramten", ["apmail.org"]),
    ]
    assert instaddr.account_state(db, "vikramten") == _STATE_B
    assert instaddr.account_state(db, None) == _STATE_A

    # signing the same name in again replaces it, never adds a second copy
    instaddr.save_account(db, "vikramten", _STATE_A, ["apmail.org"])
    assert [a["name"] for a in instaddr.load_accounts(db)] == ["main", "vikramten"]
    assert instaddr.account_state(db, "vikramten") == _STATE_A


def test_a_further_account_with_no_domain_is_refused(db):
    import pytest

    from app.automation import instaddr

    with pytest.raises(ValueError):
        instaddr.save_account(db, "empty", _STATE_B, [])
    assert instaddr.load_accounts(db) == []


def test_the_reader_built_from_the_database_routes_both_accounts(db):
    from app.automation import instaddr

    instaddr.save_account(db, None, _STATE_A, ["catbar.net"])
    instaddr.save_account(db, "vikramten", _STATE_B, ["apmail.org"])
    reader = get_otp_reader(_settings(), extras=[])
    assert isinstance(reader, RoutedOtpReader)
    assert reader.reader_for("prygemcar@apmail.org")[0].state == _STATE_B
    assert reader.reader_for("earweeput@catbar.net")[0].state == _STATE_A


def test_two_accounts_on_one_domain_are_told_apart_by_address():
    # 24 Nov 2026: the old @catbar.net accounts are on the first InstAddr
    # account, the new ones (and every @apmail.org one) on the second.
    reader = get_otp_reader(
        _settings(),
        extras=[],
        instaddr_accounts=[
            ("main", _STATE_A, ("catbar.net",), ()),
            ("vikramten", _STATE_B, ("catbar.net", "apmail.org"), ("doace@catbar.net", "prygemcar@apmail.org")),
        ],
    )
    assert reader.reader_for("earweeput@catbar.net")[0].state is _STATE_A  # old one: by domain
    assert reader.reader_for("DOACE@catbar.net")[0].state is _STATE_B  # new one: by address
    assert reader.reader_for("prygemcar@apmail.org")[0].state is _STATE_B
    assert reader.reader_for("another@apmail.org")[0].state is _STATE_B  # its own domain


def test_a_further_account_keeps_its_addresses(db):
    from app.automation import instaddr

    instaddr.save_account(db, "vikramten", _STATE_B, ["apmail.org"], ["Doace@catbar.net", "x@apmail.org", "nope"])
    (account,) = instaddr.load_accounts(db)
    assert account["addresses"] == ["doace@catbar.net", "x@apmail.org"]
    assert instaddr.domains_of(account["addresses"]) == ("apmail.org", "catbar.net")
