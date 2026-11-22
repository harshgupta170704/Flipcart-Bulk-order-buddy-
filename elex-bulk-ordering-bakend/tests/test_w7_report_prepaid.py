"""A report's "Cash on Delivery: No" (review of 27 Oct 2026).

The report's "No" was dropped, as saying nothing of how the order was paid
(card, UPI…): the order's payment mode stayed "unknown", which the CSV writes
blank, so the report's own answer was lost. It is now kept as "prepaid" — not
Cash on Delivery, paid some way the report does not name — which reads as
such everywhere the mode is read, and never as a card: the CSV's Cash on
Delivery says "No" and its card number stays blank, and the Order Details API
names no method but reads the money as paid.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import order_details
from app.core.security import create_access_token
from app.db.models import Order, OrderStatus
from tests.w7_report_rows import ORDER, UNITS, api, buyer, line, order_of, report, units_of, upload  # noqa: F401 — fixtures


def _csv_row(api) -> dict:
    text = api.get("/orders/export.csv").text.lstrip("﻿")
    (row,) = list(csv.DictReader(io.StringIO(text)))
    return row


@pytest.fixture
def details() -> TestClient:
    application = FastAPI()
    application.include_router(order_details.router)
    return TestClient(application, headers={"Authorization": f"Bearer {create_access_token('ops@test.com')}"})


def _payment(details) -> tuple[str, str]:
    data = details.post("/orders/orderDetails", json={"orderId": ORDER}).json()["data"]
    (payment,) = data["payments"]
    return payment["method"], payment["status"]


def test_a_reports_no_is_kept_and_the_csv_says_no(api, db, buyer):
    upload(api, report(line(cash_on_delivery="No")))

    assert order_of(db).payment_mode == "prepaid"
    row = _csv_row(api)
    assert (row["Cash on Delivery"], row["Card Number"], row["Payment Mode"]) == ("No", "", "prepaid")


def test_a_later_reports_no_fills_an_order_of_unknown_payment(api, db, buyer):
    upload(api, report(line()))  # the column left empty: not known
    assert order_of(db).payment_mode == "unknown"
    assert _csv_row(api)["Cash on Delivery"] == ""

    out = upload(api, report(line(cash_on_delivery="No"))).json()

    assert out["updated"] == 1
    assert order_of(db).payment_mode == "prepaid"
    assert _csv_row(api)["Cash on Delivery"] == "No"


@pytest.mark.parametrize("mode", ["card", "cod"])
def test_a_runs_own_mode_is_never_overwritten(api, db, buyer, mode):
    db.add(Order(account_id=buyer.id, order_id=ORDER, status=OrderStatus.placed, payment_mode=mode, qty=1))
    db.commit()

    upload(api, report(line(cash_on_delivery="Yes" if mode == "card" else "No")))

    assert order_of(db).payment_mode == mode


def test_the_order_details_answer_names_no_method_but_reads_the_money_as_paid(api, db, buyer, details):
    upload(api, report(line(cash_on_delivery="No")))

    # Placed and standing: paid before it came, by a means nobody named.
    assert _payment(details) == ("UNKNOWN", "CAPTURED")
    summary = details.post("/orders/orderDetails", json={"emailid": "buyer1@example.com"}).json()["data"]
    assert [(entry["paymentMethod"], entry["paymentStatus"]) for entry in summary] == [("UNKNOWN", "CAPTURED")]

    # Cancelled: the money is owed back, as for a card — never "not charged".
    upload(api, report(line(status="Cancelled")))
    assert order_of(db).status == OrderStatus.failed
    assert _payment(details) == ("UNKNOWN", "REFUND_DUE")


def test_an_order_of_unknown_payment_still_says_nothing(api, db, buyer, details):
    upload(api, report(line()))

    assert _payment(details) == ("UNKNOWN", "UNKNOWN")


def test_flipkarts_page_saying_cash_on_delivery_stands_over_a_reports_no(api, db, buyer, details):
    """The read-back wins wherever a report meets it: the unit's own page
    says how it is paid."""
    upload(api, report(line(cash_on_delivery="No")))
    (unit,) = units_of(db)
    unit.synced_at = datetime(2026, 10, 26, 6, 0)
    unit.payment_method = "Cash On Delivery"
    db.commit()

    assert _csv_row(api)["Cash on Delivery"] == "Yes"
    assert _payment(details) == ("COD", "COD_ON_DELIVERY")
