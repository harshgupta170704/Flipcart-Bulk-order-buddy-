"""The command-line ``order`` states its price cap too (audit F-23, 26 Oct 2026).

``--target`` or ``--no-cap``: exactly one of the two. With neither, or with
both, the command stops before anything else happens — before the card is
asked for, before an account is read, and before a browser opens. With
``--no-cap`` alone it orders at any price, and says so in the log.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import logging
from types import SimpleNamespace

import pytest

from app import cli
from app.automation import ordering as order_flow
from app.db.models import Account, AccountStatus, Address, Order, OrderStatus, Session as SessionModel
from tests.fakes import FakePage

PRODUCT = "https://dl.flipkart.com/s/uFy91dNNNN"
EMAIL = "cap-cli@test.com"


@pytest.fixture
def account_and_address(db) -> None:
    account = Account(email=EMAIL, status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    db.add(
        Address(
            label="lucknow", name="Ayush", line1="Garden Bay", city="Lucknow", pincode="226001", is_default=True
        )
    )
    db.commit()


@pytest.fixture
def watched(monkeypatch) -> dict:
    """Counts the browsers opened, keeps what each order run was told, and
    refuses the card prompt outright: a refused run must reach none of them."""
    seen: dict = {"browsers": 0, "orders": []}

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_):
        seen["browsers"] += 1
        page = FakePage()
        yield page.context, page

    async def fake_run_order(page, **kwargs):
        seen["orders"].append(kwargs)
        return {"status": OrderStatus.failed, "amount": None, "payment_mode": order_flow.PAYMENT_COD}

    def no_prompt(*_args, **_kwargs):
        raise AssertionError("the card prompt must not be reached")

    monkeypatch.setattr(cli, "browser_page", fake_browser_page)
    monkeypatch.setattr(order_flow, "run_order", fake_run_order)
    monkeypatch.setattr(getpass, "getpass", no_prompt)
    return seen


def _args(**overrides) -> SimpleNamespace:
    """What argparse builds for ``order`` — without ``no_cap`` unless it is
    given, as a caller written before the flag existed would build it."""
    values = dict(
        product=PRODUCT,
        email=EMAIL,
        all_accounts=False,
        address=None,
        address_id=None,
        gst_id=None,
        target=None,
        qty=1,
        payment_mode=order_flow.PAYMENT_COD,
        card=None,
        month=None,
        year=None,
        cvv=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _typed(*flags: str):
    """The order command as typed, Cash on Delivery, plus ``flags``."""
    return cli.build_parser().parse_args(
        ["order", "--product", PRODUCT, "--email", EMAIL, "--payment-mode", "cod", *flags]
    )


def _refused(caplog, *words: str) -> bool:
    return any(
        record.levelname == "ERROR"
        and "nothing was ordered" in record.getMessage()
        and all(word in record.getMessage() for word in words)
        for record in caplog.records
    )


def test_without_target_or_no_cap_the_command_stops_first(db, account_and_address, watched, caplog):
    # A card run with no card flags: had the cap been checked later, the
    # hidden card prompt would have been the first thing to happen.
    asyncio.run(cli.cmd_order(_args(payment_mode=order_flow.PAYMENT_CARD)))

    assert watched == {"browsers": 0, "orders": []}
    assert db.query(Order).count() == 0
    assert _refused(caplog, "--target", "--no-cap")


def test_target_and_no_cap_together_are_refused_first(db, account_and_address, watched, caplog):
    asyncio.run(cli.cmd_order(_args(target=18250.0, no_cap=True, payment_mode=order_flow.PAYMENT_CARD)))

    assert watched == {"browsers": 0, "orders": []}
    assert db.query(Order).count() == 0
    assert _refused(caplog, "contradict")


def test_no_cap_alone_orders_at_any_price_and_says_so(db, account_and_address, watched, caplog):
    caplog.set_level(logging.WARNING, logger="ordering.cli")

    asyncio.run(cli.cmd_order(_args(no_cap=True)))

    assert watched["browsers"] == 1
    assert [call["target"] for call in watched["orders"]] == [None]  # the flow's "no cap"
    assert db.query(Order).one().target is None
    assert any(record.levelname == "WARNING" and "--no-cap" in record.getMessage() for record in caplog.records)


def test_the_flag_and_the_rule_hold_on_the_command_line(db, account_and_address, watched, caplog):
    assert _typed().no_cap is False
    assert _typed("--no-cap").no_cap is True

    for flags in ([], ["--target", "18250", "--no-cap"]):
        args = _typed(*flags)
        asyncio.run(args.func(args))
    assert watched == {"browsers": 0, "orders": []}

    # With the cap stated, the quantity's own bound still stops the run.
    args = _typed("--no-cap", "--qty", "0")
    asyncio.run(args.func(args))
    assert watched == {"browsers": 0, "orders": []}
    assert _refused(caplog, "--qty")

    args = _typed("--no-cap")
    asyncio.run(args.func(args))
    assert watched["browsers"] == 1
    assert [call["target"] for call in watched["orders"]] == [None]
