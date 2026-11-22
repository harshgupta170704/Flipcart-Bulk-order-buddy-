"""The CLI's ``order`` command records a run exactly as the portal does.

It used to build its own Order row and left out ``payment_mode`` and
``placed_at``, so two Cash on Delivery runs were filed as card payments with
no time placed. It now uses the portal's saver; these pin what that means.
"""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from app import cli
from app.automation import ordering as order_flow
from app.db.models import Account, AccountStatus, Address, Order, OrderStatus, Session as SessionModel
from tests.fakes import FakePage

PRODUCT = "https://dl.flipkart.com/s/uFy91dNNNN"


@pytest.fixture
def account(db) -> Account:
    row = Account(email="ayush@test.com", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    db.add(SessionModel(account_id=row.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    db.commit()
    return row


@pytest.fixture
def book(db) -> Address:
    """The CLI ships to the Address Book like the portal does; this is the default."""
    row = Address(
        label="lucknow",
        name="Ayush",
        line1="Garden Bay",
        city="Lucknow",
        state="Uttar Pradesh",
        pincode="226001",
        is_default=True,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def no_browser(monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_browser_page(**_):
        page = FakePage()
        yield page.context, page

    monkeypatch.setattr(cli, "browser_page", fake_browser_page)


def _args(**overrides) -> SimpleNamespace:
    values = dict(
        product=PRODUCT,
        email="ayush@test.com",
        address="Lucknow",
        address_id=None,
        gst_id=None,
        target=18000.0,
        qty=1,
        payment_mode=order_flow.PAYMENT_COD,
        card=None,
        month=None,
        year=None,
        cvv=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _stub(monkeypatch, result: dict) -> None:
    async def fake_run_order(page, **_):
        return result

    monkeypatch.setattr(order_flow, "run_order", fake_run_order)


def test_the_order_command_needs_an_account_named_or_an_explicit_all(db, account, book, no_browser, monkeypatch):
    """Audit F-21 (25 Oct 2026): with no account named the command once ordered
    on every account that had a session. Now it refuses, unless told --all."""
    calls: list = []

    async def fake_run_order(page, **kwargs):
        calls.append(kwargs)
        return {"status": OrderStatus.failed, "amount": None, "target": 18000.0, "payment_mode": order_flow.PAYMENT_COD}

    monkeypatch.setattr(order_flow, "run_order", fake_run_order)

    asyncio.run(cli.cmd_order(_args(email=None)))
    assert calls == []  # nothing ran: no account was named

    asyncio.run(cli.cmd_order(_args(email=None, all_accounts=True)))
    assert len(calls) == 1  # said in so many words: every account with a session (one here)


def test_a_cod_order_is_recorded_as_cod_with_its_time(db, account, book, no_browser, monkeypatch):
    _stub(
        monkeypatch,
        {
            "status": OrderStatus.placed,
            "order_id": "OD338963864435840100",
            "amount": 17707.0,
            "target": 18000.0,
            "payment_mode": order_flow.PAYMENT_COD,
            "qty": 1,
            "model": "vivo T5 Lite 44W 5G",
        },
    )
    asyncio.run(cli.cmd_order(_args()))

    order = db.query(Order).one()
    assert order.status == OrderStatus.placed
    assert order.payment_mode == order_flow.PAYMENT_COD  # was "card" by column default
    assert order.placed_at is not None  # was never set
    assert order.order_id == "OD338963864435840100"
    assert order.amount == 17707.0
    assert order.pin == "226001"  # the Address Book row, not a config value


def test_without_an_address_book_address_the_cli_refuses_to_order(
    db, account, no_browser, monkeypatch, caplog
):
    """Same rule as the portal: no address, no run — and the reason is logged."""

    async def never(page, **_):
        raise AssertionError("run_order must not be called without an address")

    monkeypatch.setattr(order_flow, "run_order", never)

    asyncio.run(cli.cmd_order(_args()))

    assert db.query(Order).count() == 0
    assert any(
        record.levelname == "ERROR" and "no delivery address" in record.getMessage()
        for record in caplog.records
    )


def test_an_unconfirmed_order_is_recorded_and_shouted_about(
    db, account, book, no_browser, monkeypatch, caplog
):
    _stub(
        monkeypatch,
        {
            "status": OrderStatus.unconfirmed,
            "order_id": None,
            "amount": 17707.0,
            "target": 18000.0,
            "payment_mode": order_flow.PAYMENT_COD,
            "qty": 1,
            "error": "confirmation page did not appear. The order MAY exist",
        },
    )
    asyncio.run(cli.cmd_order(_args()))

    order = db.query(Order).one()
    assert order.status == OrderStatus.unconfirmed
    assert order.placed_at is not None  # counts in unit-limit windows
    assert any(
        record.levelname == "ERROR" and "UNCONFIRMED" in record.getMessage()
        for record in caplog.records
    )
