"""The morning review of 27 Oct 2026, in the read-back's own files.

* A read-back that finds an account signed out, and a login run (or a command
  line login) that ends without a session, leave no cookie set behind: the dead
  one went on counting as a valid session, made a plain login run pass the
  account over, and was loaded into order and coupon runs. A sign-in that
  finished meanwhile is kept.
* The numbered OTP inboxes keep the server their password was set up for when
  the first inbox is moved on the Settings page; a Gmail address needs no host
  at all; one that cannot be used is shown on the Settings page with why.
* An order handed in by id takes its product name and colour from its page,
  never overwriting what the row holds, never naming two products as one, and
  never pairing one product's colour with another's name.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.otp import RoutedOtpReader, get_otp_reader
from app.automation.sync import NotSignedIn, ScrapedOrder
from app.config import get_settings
from app.core import app_settings, session_store
from app.core.security import create_access_token
from app.db.base import SessionLocal
from app.db.models import (
    Account,
    AccountStatus,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
)
from app.jobs import sync_tasks, tasks
from tests.fakes import FakeContext, FakePage

KURTA = "Sa Rasa Women Printed Straight Kurta"
SHIRT = "Roadster Men Checked Casual Shirt"
FRESH = [{"name": "SN", "value": "fresh"}]


# --- helpers -----------------------------------------------------------------
def _account(db, email):
    account = Account(email=email, status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": email}], valid=True))
    db.commit()
    return account


def _job(db, job_type):
    job = Job(type=job_type, status=JobStatus.queued)
    db.add(job)
    db.commit()
    return job


def _cookie_sets(db, account_id):
    db.expire_all()
    return [s.cookies for s in db.query(SessionModel).filter(SessionModel.account_id == account_id).all()]


def _status(db, account_id):
    db.expire_all()
    return db.get(Account, account_id).status


def _item(db, job):
    db.expire_all()
    return db.query(JobItem).filter(JobItem.job_id == job.id).one()


def _fake_browser(monkeypatch, module):
    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    monkeypatch.setattr(module, "browser_page", fake_browser_page)


def _save_fresh_login(account_id):
    """A login run elsewhere finishing: a new cookie set, saved the usual way."""
    with SessionLocal() as other:
        session_store.save_session(other, other.get(Account, account_id), FRESH)


# --- a dead sign-in leaves no cookie set -------------------------------------------
def test_a_read_back_that_finds_the_account_signed_out_deletes_its_cookie_set(db, monkeypatch):
    gone = _account(db, "gone@test.com").id
    fine = _account(db, "fine@test.com").id
    job = _job(db, JobType.sync)
    _fake_browser(monkeypatch, sync_tasks)

    async def bounce(page, **kwargs):
        raise NotSignedIn("Flipkart sent the orders page to its login page")

    monkeypatch.setattr(sync_tasks, "scrape_orders", bounce)

    asyncio.run(sync_tasks.run_sync_job(job.id, [gone]))

    assert _cookie_sets(db, gone) == []
    assert _status(db, gone) == AccountStatus.session_invalid
    assert not session_store.has_valid_session(db, db.get(Account, gone))
    assert "not signed in" in (_item(db, job).message or "")
    # Another account's sign-in is not touched.
    assert len(_cookie_sets(db, fine)) == 1
    assert _status(db, fine) == AccountStatus.session_valid


def test_a_login_that_finished_during_the_read_back_is_kept(db, monkeypatch):
    """The read-back took the old cookies; a login run saved new ones before
    Flipkart turned the old ones away. The new set is not the one that failed."""
    account_id = _account(db, "again@test.com").id
    job = _job(db, JobType.sync)
    _fake_browser(monkeypatch, sync_tasks)

    async def login_then_bounce(page, **kwargs):
        _save_fresh_login(account_id)
        raise NotSignedIn("Flipkart sent the orders page to its login page")

    monkeypatch.setattr(sync_tasks, "scrape_orders", login_then_bounce)

    asyncio.run(sync_tasks.run_sync_job(job.id, [account_id]))

    assert _cookie_sets(db, account_id) == [FRESH]
    assert _status(db, account_id) == AccountStatus.session_valid


def test_a_login_run_that_does_not_complete_deletes_the_cookie_set(db, monkeypatch):
    account_id = _account(db, "relog@test.com").id
    job = _job(db, JobType.login)
    _fake_browser(monkeypatch, tasks)

    async def no_session(page, email, otp_reader=None, **kwargs):
        return []

    monkeypatch.setattr(tasks, "run_login", no_session)

    asyncio.run(tasks.run_login_job(job.id, [account_id], force=True))

    assert _cookie_sets(db, account_id) == []
    assert _status(db, account_id) == AccountStatus.session_invalid
    item = _item(db, job)
    assert item.status == JobItemStatus.failed and "login not completed" in (item.message or "")


def test_a_failed_login_leaves_a_sign_in_that_finished_meanwhile(db, monkeypatch):
    """Two login runs on one account: the one that fails must not undo the one
    that saved a session while it ran."""
    account_id = _account(db, "twice@test.com").id
    job = _job(db, JobType.login)
    _fake_browser(monkeypatch, tasks)

    async def the_other_run_wins(page, email, otp_reader=None, **kwargs):
        _save_fresh_login(account_id)
        return []

    monkeypatch.setattr(tasks, "run_login", the_other_run_wins)

    asyncio.run(tasks.run_login_job(job.id, [account_id], force=True))

    assert _cookie_sets(db, account_id) == [FRESH]
    assert _status(db, account_id) == AccountStatus.session_valid


def test_a_command_line_login_that_does_not_complete_deletes_the_cookie_set(db, monkeypatch):
    from app import cli

    account_id = _account(db, "cli@test.com").id
    _fake_browser(monkeypatch, cli)

    async def no_session(page, email, otp_reader=None, **kwargs):
        return []

    monkeypatch.setattr(cli.login_flow, "run_login", no_session)

    asyncio.run(cli.cmd_login(SimpleNamespace(force=True)))

    assert _cookie_sets(db, account_id) == []
    assert _status(db, account_id) == AccountStatus.session_invalid


def test_forgetting_a_session_touches_no_other_account(db):
    gone = _account(db, "a@test.com")
    kept_id = _account(db, "b@test.com").id

    assert session_store.forget_session(db, gone) is True

    assert _cookie_sets(db, gone.id) == []
    assert len(_cookie_sets(db, kept_id)) == 1
    assert _status(db, kept_id) == AccountStatus.session_valid


# --- the numbered inboxes keep their own server -------------------------------------
def _server_inbox(monkeypatch, **values):
    """The first inbox as the server's own settings (.env) give it."""
    server = get_settings()
    base = {
        "otp_imap_host": "",
        "otp_imap_port": 993,
        "otp_imap_user": "",
        "otp_imap_password": "",
        "otp_imap_mailbox": "INBOX",
        "otp_imap_use_ssl": True,
    }
    for name, value in {**base, **values}.items():
        monkeypatch.setattr(server, name, value)


def _numbered(monkeypatch, number, user, password="numbered-app-password"):
    monkeypatch.setenv(f"ORDERING_OTP_IMAP_USER_{number}", user)
    if password:
        monkeypatch.setenv(f"ORDERING_OTP_IMAP_PASSWORD_{number}", password)


def _inbox_for(address):
    reader = get_otp_reader(tasks._effective_settings())
    assert isinstance(reader, RoutedOtpReader)
    inbox, label = reader.reader_for(address)
    assert "the main inbox" not in label
    return reader, inbox


def _settings_page():
    from app.api import settings as settings_api

    application = FastAPI()
    application.include_router(settings_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"}).get("/settings")


def test_a_numbered_inbox_keeps_the_server_it_was_set_up_beside(db, monkeypatch):
    """Inbox 2 names only its address and App Password, so it borrows the first
    inbox's host and SSL: the .env's. The first inbox is then moved to another
    provider on the Settings page, with SSL off. A login run's reader still
    sends inbox 2's password to the .env's server, over SSL, the server the
    Settings page lists for it."""
    _server_inbox(monkeypatch, otp_imap_host="imap.zoho.in", otp_imap_user="main@zoho.in", otp_imap_password="x")
    _numbered(monkeypatch, 2, "codes@zoho.in")
    app_settings.set_overrides(
        db,
        {
            "otp_imap_host": "outlook.office365.com",
            "otp_imap_user": "main@outlook.com",
            "otp_imap_password": "outlook-password",
            "otp_imap_use_ssl": False,
        },
    )

    reader, second = _inbox_for("codes@zoho.in")

    # The first inbox follows the Settings page...
    assert (reader.default.host, reader.default.use_ssl) == ("outlook.office365.com", False)
    # ...and inbox 2 stays where its password belongs, as the Settings page lists it.
    assert (second.host, second.port, second.mailbox, second.use_ssl) == ("imap.zoho.in", 993, "INBOX", True)
    assert _settings_page().json()["otp_extra_inboxes"][0]["host"] == "imap.zoho.in"


def test_a_gmail_inbox_is_read_from_gmail_whatever_the_first_inbox(db, monkeypatch):
    """A Gmail App Password belongs to Gmail's server: never lent another
    provider's host, nor its port, mailbox or plain connection."""
    _server_inbox(
        monkeypatch,
        otp_imap_host="imap.zoho.in",
        otp_imap_port=1993,
        otp_imap_mailbox="Codes",
        otp_imap_use_ssl=False,
        otp_imap_user="main@zoho.in",
        otp_imap_password="x",
    )
    _numbered(monkeypatch, 2, "second@gmail.com")

    _, second = _inbox_for("second@gmail.com")

    assert (second.host, second.port, second.mailbox, second.use_ssl) == ("imap.gmail.com", 993, "INBOX", True)


def test_a_gmail_inbox_beside_a_gmail_first_inbox_borrows_its_settings(db, monkeypatch):
    _server_inbox(
        monkeypatch,
        otp_imap_host="imap.gmail.com",
        otp_imap_mailbox="[Gmail]/All Mail",
        otp_imap_user="main@gmail.com",
        otp_imap_password="x",
    )
    _numbered(monkeypatch, 2, "second@gmail.com")

    _, second = _inbox_for("second@gmail.com")

    assert (second.host, second.mailbox) == ("imap.gmail.com", "[Gmail]/All Mail")


def test_a_gmail_inbox_works_when_the_first_inbox_is_set_only_on_the_settings_page(db, monkeypatch):
    """The server's .env leaves the first inbox blank and the Settings page sets
    it, as the deployment notes allow: a second Gmail still gets its codes."""
    _server_inbox(monkeypatch)
    _numbered(monkeypatch, 2, "second@gmail.com")
    app_settings.set_overrides(
        db, {"otp_imap_host": "imap.gmail.com", "otp_imap_user": "main@gmail.com", "otp_imap_password": "x"}
    )

    _, second = _inbox_for("second@gmail.com")

    assert (second.host, second.port, second.use_ssl) == ("imap.gmail.com", 993, True)


def test_an_inbox_that_cannot_be_used_is_shown_on_the_settings_page_with_why(db, monkeypatch, caplog):
    """No host of its own, none in the server's .env to borrow: never the
    Settings page's. The page says so, and names the setting to fix."""
    _server_inbox(monkeypatch)
    _numbered(monkeypatch, 3, "codes@zoho.in", password="zoho-app-password")
    _numbered(monkeypatch, 4, "half@zoho.in", password="")
    app_settings.set_overrides(
        db, {"otp_imap_host": "imap.zoho.in", "otp_imap_user": "main@zoho.in", "otp_imap_password": "x"}
    )

    with caplog.at_level("WARNING"):
        response = _settings_page()
        reader = get_otp_reader(tasks._effective_settings())

    body = response.json()
    assert body["otp_extra_inboxes"] == []
    assert [(i["number"], i["user"]) for i in body["otp_unused_inboxes"]] == [(3, "codes@zoho.in"), (4, "half@zoho.in")]
    assert "ORDERING_OTP_IMAP_HOST_3" in body["otp_unused_inboxes"][0]["problem"]
    assert "never lent" in body["otp_unused_inboxes"][0]["problem"]
    assert "ORDERING_OTP_IMAP_PASSWORD_4" in body["otp_unused_inboxes"][1]["problem"]
    assert "zoho-app-password" not in response.text
    assert "OTP inbox 3 is not used" in caplog.text
    # Nothing is routed to a host it was not set up for.
    assert not isinstance(reader, RoutedOtpReader)


# --- an order handed in by id is named after its page -------------------------------
def _by_id(db, account, **fields):
    """An order handed in on the id sheet: an order id, and no product."""
    row = Order(
        **{
            "account_id": account.id,
            "order_id": "OD1",
            "qty": 1,
            "status": OrderStatus.placed,
            "payment_mode": "unknown",
            **fields,
        }
    )
    db.add(row)
    db.commit()
    return row.id


def _product(db, row_id):
    db.expire_all()
    row = db.get(Order, row_id)
    return row.model, row.color


def _seen(**fields):
    return ScrapedOrder(**{"order_id": "OD1", "unit_id": "U1", "platform_status": "Order Confirmed", **fields})


def test_an_order_handed_in_by_id_takes_its_product_from_the_page(db):
    account = _account(db, "byid@test.com")
    row_id = _by_id(db, account)

    sync_tasks._reconcile(account.id, [_seen(model=KURTA, color="Flamingo Pink")], import_unknown=False)

    assert _product(db, row_id) == (KURTA, "Flamingo Pink")


def test_a_product_the_order_names_is_kept(db):
    """A run's own record, read off the product page, is never rewritten in the
    orders page's wording: nor its colour."""
    account = _account(db, "run@test.com")
    row_id = _by_id(db, account, model="Sa Rasa Kurta (product page)", color="Pink")

    sync_tasks._reconcile(account.id, [_seen(model=KURTA, color="Flamingo Pink")], import_unknown=False)

    assert _product(db, row_id) == ("Sa Rasa Kurta (product page)", "Pink")


def test_the_name_and_the_colour_come_from_one_unit(db):
    """The unit the order row mirrors (the one on its way) names no product; the
    other one does. Its colour comes with its name: never the first unit's
    colour beside another unit's name."""
    account = _account(db, "two@test.com")
    row_id = _by_id(db, account, qty=2)
    on_its_way = _seen(unit_id="U1", platform_status="Shipped", color="Red")
    delivered = _seen(unit_id="U2", platform_status="Delivered", model=KURTA, color="Flamingo Pink")

    sync_tasks._reconcile(account.id, [on_its_way, delivered], import_unknown=False)

    assert _product(db, row_id) == (KURTA, "Flamingo Pink")


def test_an_order_whose_units_name_two_products_stays_unnamed(db):
    """One name for two products would count the shirt's units under the
    kurta's name for good: the order stays unnamed instead."""
    account = _account(db, "mixed@test.com")
    row_id = _by_id(db, account, qty=2)
    shirt = _seen(unit_id="U1", platform_status="Shipped", model=SHIRT, color="Navy Blue")
    kurta = _seen(unit_id="U2", platform_status="Shipped", model=KURTA, color="Flamingo Pink")

    sync_tasks._reconcile(account.id, [shirt, kurta], import_unknown=False)

    assert _product(db, row_id) == (None, None)


def test_one_product_in_two_wordings_still_names_the_order(db):
    account = _account(db, "wording@test.com")
    row_id = _by_id(db, account, qty=2)
    first = _seen(unit_id="U1", platform_status="Shipped", model=KURTA, color="Flamingo Pink")
    second = _seen(unit_id="U2", platform_status="Shipped", model="  sa rasa women  PRINTED straight kurta ")

    sync_tasks._reconcile(account.id, [first, second], import_unknown=False)

    assert _product(db, row_id) == (KURTA, "Flamingo Pink")


def test_a_named_order_takes_a_colour_only_from_its_own_product(db):
    """An order report names the shirt and has no colour column. The kurta on
    its way shows first; the colour is the shirt's, or none."""
    account = _account(db, "report@test.com")
    row_id = _by_id(db, account, qty=2, model=SHIRT)
    kurta = _seen(unit_id="U1", platform_status="Shipped", model=KURTA, color="Flamingo Pink")
    shirt = _seen(unit_id="U2", platform_status="Delivered", model=SHIRT, color="Navy Blue")

    sync_tasks._reconcile(account.id, [kurta, shirt], import_unknown=False)
    assert _product(db, row_id) == (SHIRT, "Navy Blue")

    other_id = _by_id(db, account, order_id="OD2", model=SHIRT)
    sync_tasks._reconcile(
        account.id, [_seen(order_id="OD2", unit_id="U9", model=KURTA, color="Flamingo Pink")], import_unknown=False
    )
    assert _product(db, other_id) == (SHIRT, None)


def test_an_imported_order_pairs_its_name_and_colour_too(db):
    account = _account(db, "imported-pair@test.com")
    on_its_way = _seen(unit_id="U1", platform_status="Shipped", color="Red")
    delivered = _seen(unit_id="U2", platform_status="Delivered", model=KURTA, color="Flamingo Pink")

    sync_tasks._reconcile(account.id, [on_its_way, delivered], import_unknown=True)

    db.expire_all()
    (row,) = db.query(Order).filter(Order.account_id == account.id).all()
    assert (row.model, row.color) == (KURTA, "Flamingo Pink")


def test_a_long_product_name_is_cut_to_its_column(db):
    """PostgreSQL refuses a value longer than its column, and one long name
    would fail the account's whole read-back; SQLite would keep it whole."""
    account = _account(db, "long@test.com")
    row_id = _by_id(db, account)
    model_width = Order.__table__.c.model.type.length
    color_width = Order.__table__.c.color.type.length

    sync_tasks._reconcile(
        account.id, [_seen(model="K" * (model_width + 45), color="C" * (color_width + 1))], import_unknown=False
    )

    assert _product(db, row_id) == ("K" * model_width, "C" * color_width)


def test_an_imported_order_s_long_name_is_cut_too(db):
    account = _account(db, "imported@test.com")
    model_width = Order.__table__.c.model.type.length

    sync_tasks._reconcile(account.id, [_seen(model="K" * (model_width + 1))], import_unknown=True)

    db.expire_all()
    (row,) = db.query(Order).filter(Order.account_id == account.id).all()
    assert row.model == "K" * model_width
