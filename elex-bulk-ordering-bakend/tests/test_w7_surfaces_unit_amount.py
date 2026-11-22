"""A unit's share of its order's amount never adds up to more than the order
(review of 27 Oct 2026, surfaces).

The share was the amount over the order's quantity. An order filed from a
report can hold more units than its quantity says — two rows of one order id
with the quantity 1, or with none given (app.io.order_report) — and each of
its two units then showed the whole amount: on the ledger page's Unit amount
and in the CSV's Unit Amount, twice the order's total between them. The
share is now over the quantity or the units the order holds, whichever is
more.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus, OrderUnit
from app.schemas.order import OrderOut


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def two_units_quantity_one(db) -> Order:
    account = Account(email="share@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    order = Order(
        account_id=account.id,
        order_id="OD338975799606677301",
        status=OrderStatus.placed,
        model="vivo T5 Lite 44W 5G",
        qty=1,
        amount=36414.0,
        payment_mode="unknown",
        placed_at=datetime(2026, 10, 24, 4, 30),
    )
    # Two units, as two report rows of one order id file them (made-up ids).
    for position in (1, 2):
        order.units.append(
            OrderUnit(platform="flipkart", position=position, source="upload", unit_id=f"43912345678901237730{position}")
        )
    db.add(order)
    db.commit()
    return order


def test_the_ledger_page_shares_the_amount_over_every_unit(api, db, two_units_quantity_one):
    rows = sorted(api.get("/orders").json()["items"], key=lambda row: row["unit_position"])
    assert [(row["units_in_order"], row["qty"], row["unit_amount"]) for row in rows] == [(2, 1, 18207.0), (2, 1, 18207.0)]
    assert sum(row["unit_amount"] for row in rows) == two_units_quantity_one.amount
    # And for a caller that holds the one order (the settle and detail answers).
    assert OrderOut.from_order(db.get(Order, two_units_quantity_one.id)).unit_amount == 18207.0


def test_the_csv_shares_the_amount_over_every_unit(api, two_units_quantity_one):
    text = api.get("/orders/export.csv").text.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text)))
    # The quantity stays the order's own word; only the share changes.
    assert [(row["Quantity"], row["Unit Amount"], row["Total Order Amount"]) for row in rows] == [
        ("1", "18207.00", "36414.00"),
        ("1", "18207.00", "36414.00"),
    ]


@pytest.mark.parametrize(
    ("qty", "units", "share"),
    [
        pytest.param(2, 2, 18207.0, id="as-many-units-as-the-quantity"),
        pytest.param(1, 2, 18207.0, id="more-units-than-the-quantity"),
        pytest.param(None, 3, 12138.0, id="no-quantity-given"),
        pytest.param(2, 1, 18207.0, id="fewer-units-on-file-than-the-quantity"),
        pytest.param(0, None, 36414.0, id="a-zero-quantity-and-no-count-is-one-unit"),
    ],
)
def test_the_share_is_over_the_quantity_or_the_units_whichever_is_more(qty, units, share):
    from app.io.orders_csv import unit_amount

    assert unit_amount(Order(qty=qty, amount=36414.0), units) == share
    assert unit_amount(Order(qty=qty, amount=None), units) is None
