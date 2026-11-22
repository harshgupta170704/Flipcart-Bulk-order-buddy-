"""The CSV's Business Name and Business GST No come from one source (review
of 27 Oct 2026, surfaces).

The two fell back apart — the order's business name, else the page's; the
order's GSTIN, else the page's — so an order that names a business but holds
no GSTIN (a report gave the name alone; a row recorded before the GSTIN was)
was written beside the GSTIN Flipkart's page showed, which may be another
entity's. The pair is now the order's whenever it names either, its GSTIN
filled from the GST book's one registration for that name as the Order
Details API fills it (app.io.orders_csv.gst_numbers, now shared by both), and
blank when the book holds several or none; the page's pair only when the
order names neither.
"""

from __future__ import annotations

import contextlib
import csv
import io
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.api import order_details
from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.base import engine
from app.db.models import Account, AccountStatus, Gst, Order, OrderStatus

#: Made up, in the shape of a GSTIN.
BOOK_ONE = "09ABCDE1234F1Z5"
BOOK_UP, BOOK_MH = "09PQRST6789K1Z2", "27PQRST6789K1Z0"
PAGE_GSTIN = "27AAPFU0939F1ZV"


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    application.include_router(order_details.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def account(db) -> Account:
    row = Account(email="gstpair@test.com", status=AccountStatus.session_valid)
    db.add(row)
    db.add_all(
        [
            Gst(label="Example", gstin=BOOK_ONE, business_name="Example Trading Pvt Ltd"),
            # One business, a registration per state: the name alone names neither.
            Gst(label="Two States UP", gstin=BOOK_UP, business_name="Two States Pvt Ltd"),
            Gst(label="Two States MH", gstin=BOOK_MH, business_name="Two States Pvt Ltd"),
        ]
    )
    db.commit()
    return row


def _order(db, account: Account, order_id: str, *, page: tuple[str, str] | None, **over) -> Order:
    """An order whose page showed it billed under ``page`` (name, GSTIN)."""
    order = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        order_id=order_id,
        qty=1,
        amount=18207.0,
        model="vivo T5 Lite 44W 5G",
        placed_at=datetime(2026, 10, 24, 4, 30),
        **over,
    )
    db.add(order)
    db.commit()
    if page is not None:
        order.units[0].billed_business_name, order.units[0].billed_gstin = page
        db.commit()
    return order


def _csv_pairs(api) -> dict[str, tuple[str, str]]:
    text = api.get("/orders/export.csv").text.lstrip("﻿")
    return {row["Order ID"]: (row["Business Name"], row["Business GST No"]) for row in csv.DictReader(io.StringIO(text))}


def test_the_pair_is_the_orders_own_or_the_pages_never_half_of_each(api, db, account):
    other_entity = ("Other Entity Pvt Ltd", PAGE_GSTIN)
    named = _order(db, account, "OD338975799606677101", page=other_entity, gst_name="Example Trading Pvt Ltd")
    two_states = _order(db, account, "OD338975799606677102", page=other_entity, gst_name="Two States Pvt Ltd")
    by_gstin = _order(db, account, "OD338975799606677103", page=other_entity, gstin=BOOK_ONE)
    by_page = _order(db, account, "OD338975799606677104", page=("Page Entity", "09BUAPA6939F1ZP"))

    pairs = _csv_pairs(api)

    # A business named without its GSTIN: the GST book's one registration for
    # it, never the GSTIN of whoever the page says billed it.
    assert pairs[named.order_id] == ("Example Trading Pvt Ltd", BOOK_ONE)
    # A name with a registration per state names neither: blank, not a guess.
    assert pairs[two_states.order_id] == ("Two States Pvt Ltd", "")
    # A GSTIN without its name is the order's own, beside no other entity's name.
    assert pairs[by_gstin.order_id] == ("", BOOK_ONE)
    # The order names neither (handed in by id): the page's pair, whole.
    assert pairs[by_page.order_id] == ("Page Entity", "09BUAPA6939F1ZP")

    # The Order Details API answers each of the order's own pairs the same way.
    for order in (named, two_states, by_gstin):
        answer = api.post("/orders/orderDetails", json={"orderId": order.order_id}).json()["data"]["customer"]
        assert (answer["gstHolder"] or "", answer["gstNumber"] or "") == pairs[order.order_id]


@contextlib.contextmanager
def _statements():
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_the_gst_book_is_read_once_per_chunk_and_only_when_an_order_needs_it(api, db, account):
    for n in range(3):
        _order(db, account, f"OD3389757996066772{n:02d}", page=None, gst_name="Example Trading Pvt Ltd", gstin=BOOK_ONE)
    with _statements() as none_needed:
        _csv_pairs(api)
    assert not [statement for statement in none_needed if "FROM gsts" in statement]

    for n in range(3, 25):
        _order(db, account, f"OD3389757996066772{n:02d}", page=None, gst_name="Example Trading Pvt Ltd")
    with _statements() as needed:
        pairs = _csv_pairs(api)
    assert len(pairs) == 25 and set(pairs.values()) == {("Example Trading Pvt Ltd", BOOK_ONE)}
    assert len([statement for statement in needed if "FROM gsts" in statement]) == 1
