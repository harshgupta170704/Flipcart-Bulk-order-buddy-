"""The command-line ``order`` holds its numbers to the portal's bounds (audit F-23).

A price cap that is not a finite number above zero, or a quantity below 1, is
refused before anything else happens: before the card is asked for, before an
account is read, and before a browser opens. ``inf`` would let every price
through, ``nan`` refuses every one, 0 or less refuses every order, and the
flow reads a quantity below 1 as one unit.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
from types import SimpleNamespace

import pytest

from app import cli
from app.automation import ordering as order_flow
from app.db.models import Account, AccountStatus, Address, Order, OrderStatus, Session as SessionModel
from tests.fakes import FakePage

PRODUCT = "https://dl.flipkart.com/s/uFy91dNNNN"


@pytest.fixture
def account_and_address(db) -> None:
    account = Account(email="ayush@test.com", status=AccountStatus.session_valid)
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
    """Counts every browser opened and every order run, and refuses the card
    prompt outright: a refused run must reach none of them."""
    seen = {"browsers": 0, "orders": 0}

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_):
        seen["browsers"] += 1
        page = FakePage()
        yield page.context, page

    async def fake_run_order(page, **_):
        seen["orders"] += 1
        return {"status": OrderStatus.failed, "amount": None, "payment_mode": order_flow.PAYMENT_COD}

    def no_prompt(*_args, **_kwargs):
        raise AssertionError("the card prompt must not be reached")

    monkeypatch.setattr(cli, "browser_page", fake_browser_page)
    monkeypatch.setattr(order_flow, "run_order", fake_run_order)
    monkeypatch.setattr(getpass, "getpass", no_prompt)
    return seen


def _args(**overrides) -> SimpleNamespace:
    values = dict(
        product=PRODUCT,
        email="ayush@test.com",
        all_accounts=False,
        address=None,
        address_id=None,
        gst_id=None,
        target=18250.0,
        qty=1,
        payment_mode=order_flow.PAYMENT_COD,
        card=None,
        month=None,
        year=None,
        cvv=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _refusal(caplog, flag: str) -> bool:
    return any(
        record.levelname == "ERROR" and flag in record.getMessage() and "nothing was ordered" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("target", [float("inf"), float("-inf"), float("nan"), 0.0, -1.0])
def test_a_target_that_is_not_a_finite_price_above_zero_is_refused_first(
    db, account_and_address, watched, caplog, target
):
    # A card run with no card flags: had the bounds come later, the hidden
    # card prompt would have been the first thing to happen.
    asyncio.run(cli.cmd_order(_args(target=target, payment_mode=order_flow.PAYMENT_CARD)))

    assert watched == {"browsers": 0, "orders": 0}
    assert db.query(Order).count() == 0
    assert _refusal(caplog, "--target")


@pytest.mark.parametrize("qty", [0, -1])
def test_a_quantity_below_one_is_refused_first(db, account_and_address, watched, caplog, qty):
    asyncio.run(cli.cmd_order(_args(qty=qty, payment_mode=order_flow.PAYMENT_CARD)))

    assert watched == {"browsers": 0, "orders": 0}
    assert db.query(Order).count() == 0
    assert _refusal(caplog, "--qty")


def test_the_bounds_hold_for_values_typed_on_the_command_line(db, account_and_address, watched):
    """argparse's float() accepts "inf" and "nan" as they are; the command
    refuses them."""
    # The quantity case carries a good cap, so the cap rule cannot be what
    # stops it: it is refused for its quantity.
    for typed in (["--target", "inf"], ["--target", "nan"], ["--target", "-5"], ["--target", "18250", "--qty", "0"]):
        args = cli.build_parser().parse_args(
            ["order", "--product", PRODUCT, "--email", "ayush@test.com", "--payment-mode", "cod", *typed]
        )
        asyncio.run(args.func(args))

    assert watched == {"browsers": 0, "orders": 0}
    assert db.query(Order).count() == 0


@pytest.mark.parametrize(
    ("target", "no_cap"), [(18250.0, False), (None, True)], ids=["a cap", "no cap, said with --no-cap"]
)
def test_sound_numbers_still_run(db, account_and_address, watched, target, no_cap):
    asyncio.run(cli.cmd_order(_args(target=target, no_cap=no_cap)))

    assert watched == {"browsers": 1, "orders": 1}
