"""Refunds, the courier and who cancelled, from an order report (spec FR-022,
review of 27 Oct 2026).

"WHEN the user uploads a later order report from a platform, the system shall
update the status, tracking, delivery dates, refunds, and cancellations of the
order units it contains." The reader read past a report's refund status,
amount and method, its courier and its last tracking update, naming them "not
kept", and past ``cancelled_by_user`` without a word. A later report saying a
unit was cancelled by the user, refunded and carried by a named courier
answered "updated 1", left every one of those CSV cells blank, and the row's
Outcome read "cancelled by Flipkart" — the opposite of what the report said.

A unit now keeps them, under the report's own rules: one the read-back has
seen on Flipkart only has its empty fields filled, and an empty cell blanks
nothing.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import order_details
from app.core import cancellations
from app.core.security import create_access_token
from app.db.models import OrderStatus, OrderUnit
from tests.w7_report_rows import ORDER, UNITS, api, buyer, line, order_of, report, units_of, upload  # noqa: F401 — fixtures

#: A later report's row: the unit cancelled at the user's request, its money
#: back, and the courier that had it.
REFUNDED = dict(
    status="Cancelled",
    cancelled="Yes",
    cancelled_by_user="Yes",
    refund_status="Refund Completed",
    refund_amount="18207.00",
    refund_method="Original payment method",
    shipment_partner="Ekart Logistics",
    last_tracking_update="25-10-2026 Cancelled",
)
KEPT_NOW = {"refund_status", "refund_amount", "refund_method", "shipment_partner", "last_tracking_update"}


def _csv(api) -> dict[str, dict]:
    """The full ledger CSV, one row per unit id (written as ="…" there)."""
    text = api.get("/orders/export.csv").text.lstrip("﻿")
    return {row["Unit ID"].strip('="'): row for row in csv.DictReader(io.StringIO(text))}


def test_a_later_report_puts_the_refund_the_courier_and_who_cancelled_in_the_csv(api, db, buyer):
    first = upload(api, report(line())).json()
    assert first["created"] == 1

    out = upload(api, report(line(**REFUNDED))).json()

    assert (out["created"], out["updated"], out["rejected"]) == (0, 1, [])
    assert not KEPT_NOW & set(out["not_kept"])
    row = _csv(api)[UNITS[0]]
    assert (row["Refund Status"], row["Refund Amount"], row["Refund Method"]) == (
        "Refund Completed",
        "18207.00",
        "Original payment method",
    )
    assert (row["Shipment Partner"], row["Last Tracking Update"]) == ("Ekart Logistics", "25-10-2026 Cancelled")
    # Who cancelled is what the report said, here and wherever the ledger asks.
    assert (row["Cancelled"], row["Cancelled by User"], row["Outcome"]) == ("Yes", "Yes", "cancelled_by_user")
    (item,) = api.get("/orders").json()["items"]
    assert item["cancelled_by"] == "user"
    order = order_of(db)
    assert order.status == OrderStatus.failed
    assert cancellations.cancel_readings(db, [order.id])[order.id].user_cancelled  # the dashboard's and the alerts' verdict


def test_the_ledger_page_shows_the_refund_and_the_courier(api, db, buyer):
    upload(api, report(line(**REFUNDED)))

    (item,) = api.get("/orders").json()["items"]

    assert (item["refund_status"], item["refund_amount"], item["refund_method"]) == (
        "Refund Completed",
        18207.0,
        "Original payment method",
    )
    assert (item["shipment_partner"], item["last_tracking_text"]) == ("Ekart Logistics", "25-10-2026 Cancelled")


def test_a_reports_no_says_the_user_did_not_cancel(api, db, buyer):
    """Before, a cancellation with no reason and no request was "nothing says
    by whom": the CSV's Cancelled by User blank."""
    upload(api, report(line(status="Cancelled", cancelled_by_user="No")))

    row = _csv(api)[UNITS[0]]
    assert (row["Cancelled"], row["Cancelled by User"], row["Outcome"]) == ("Yes", "No", "cancelled_by_flipkart")
    (unit,) = units_of(db)
    assert unit.cancelled_by_user is False


def test_a_no_answers_who_cancelled_only_for_a_unit_the_row_says_was_cancelled(api, db, buyer):
    """The ledger's own CSV says "Cancelled by User: No" of every unit that
    was not cancelled. Handed back, that "No" must not be kept: a unit
    cancelled later, with nothing on its page saying by whom, would read as
    Flipkart's cancellation on a sheet's word that answered nothing."""
    upload(api, report(line()))
    exported = api.get("/orders/export.csv").text.lstrip("﻿")
    assert _csv(api)[UNITS[0]]["Cancelled by User"] == "No"

    out = upload(api, exported).json()

    assert (out["updated"], out["unchanged"], out["rejected"]) == (0, 1, [])
    (unit,) = units_of(db)
    assert unit.cancelled_by_user is None
    # The row's own "cancelled" cell, or a status that reads cancelled, makes it an answer.
    upload(api, report(line(status="Refund Completed", cancelled="Yes", cancelled_by_user="No")))
    (unit,) = units_of(db)
    assert unit.cancelled_by_user is False
    # A "Yes" is kept whatever the status: the user's cancellation may not show yet.
    upload(api, report(line(order_id="OD338967210239333102", unit_id=UNITS[1], status="Cancellation requested", cancelled_by_user="Yes")))
    (asked,) = units_of(db, "OD338967210239333102")
    assert asked.cancelled_by_user is True


def test_a_unit_the_read_back_saw_keeps_its_own_values_and_only_has_empty_ones_filled(api, db, buyer):
    upload(api, report(line(status="Cancelled", shipment_partner="Delhivery", cancelled_by_user="No", refund_method="Bank account")))
    (unit,) = units_of(db)
    unit.synced_at = datetime(2026, 10, 26, 6, 0)  # Flipkart's page was read since
    unit.platform_status = "Cancelled"
    unit.timeline = [{"status": "Order Confirmed", "date": "2026-10-24T00:00:00"}, {"status": "Cancelled", "date": "2026-10-26T00:00:00"}]
    db.commit()

    out = upload(api, report(line(**REFUNDED))).json()

    assert out["updated"] == 1
    (unit,) = units_of(db)
    # What it already held stands...
    assert (unit.shipment_partner, unit.cancelled_by_user, unit.refund_method) == ("Delhivery", False, "Bank account")
    assert unit.platform_status == "Cancelled"
    # ...and what it did not is filled.
    assert (unit.refund_status, unit.refund_amount, unit.last_tracking_text) == ("Refund Completed", 18207.0, "25-10-2026 Cancelled")
    # The last tracking update is Flipkart's own page's, which it dates.
    row = _csv(api)[UNITS[0]]
    assert (row["Last Tracking Update"], row["Shipment Partner"], row["Cancelled by User"]) == ("26-10-2026 Cancelled", "Delhivery", "No")


def test_the_ledgers_own_tracking_line_handed_back_is_not_kept_as_a_reports(api, db, buyer):
    """The CSV writes Flipkart's newest dated step; that line handed back is
    the page's reading, not the report's, and must not outlive it."""
    upload(api, report(line()))
    (unit,) = units_of(db)
    unit.synced_at = datetime(2026, 10, 26, 6, 0)
    unit.timeline = [{"status": "Shipped", "date": "2026-10-25T00:00:00"}]
    db.commit()
    assert _csv(api)[UNITS[0]]["Last Tracking Update"] == "25-10-2026 Shipped"

    upload(api, api.get("/orders/export.csv").text.lstrip("﻿"))

    (unit,) = units_of(db)
    assert unit.last_tracking_text is None


def test_a_unit_never_read_back_takes_a_later_reports_values_and_an_empty_cell_blanks_none(api, db, buyer):
    upload(api, report(line(**REFUNDED)))

    later = dict(status="Cancelled", shipment_partner="Delhivery", refund_status="Refund initiated")
    out = upload(api, report(line(**later))).json()

    assert out["updated"] == 1
    (unit,) = units_of(db)
    assert (unit.shipment_partner, unit.refund_status) == ("Delhivery", "Refund initiated")
    # The cells the later report left empty blanked nothing.
    assert (unit.refund_amount, unit.refund_method, unit.cancelled_by_user, unit.last_tracking_text) == (
        18207.0,
        "Original payment method",
        True,
        "25-10-2026 Cancelled",
    )

    again = upload(api, report(line(**later))).json()
    assert (again["updated"], again["unchanged"]) == (0, 1)


def test_flipkarts_own_words_for_a_refunded_cancellation_stand_over_a_reports(api, db, buyer):
    """The read-back wins wherever a report meets it: a cancelled card order
    Flipkart words "Refund Completed" once the money is back."""
    upload(api, report(line(status="Cancelled", refund_status="Refund pending")))
    (unit,) = units_of(db)
    unit.synced_at = datetime(2026, 10, 27, 6, 0)
    unit.platform_status = "Refund Completed"
    db.commit()
    assert order_of(db).status == OrderStatus.failed  # Flipkart took it, then cancelled it

    row = _csv(api)[UNITS[0]]
    assert (row["Cancelled"], row["Refund Status"]) == ("Yes", "Refund Completed")


@pytest.mark.parametrize("column", ["shipment_partner", "refund_status", "refund_method", "last_tracking_update"])
def test_a_value_longer_than_its_column_refuses_its_row_alone(api, db, buyer, column):
    stored = {"last_tracking_update": "last_tracking_text"}.get(column, column)
    width = OrderUnit.__table__.c[stored].type.length
    lines = [
        line(**{column: "x" * (width + 1)}),
        line(order_id="OD338967210239333102", unit_id=UNITS[1], **{column: "x" * width}),
    ]

    out = upload(api, report(*lines)).json()

    assert out["created"] == 1
    assert [(r["row"], r["reason"]) for r in out["rejected"]] == [(2, f"{column} is longer than {width} characters")]


def test_a_refund_amount_or_a_yes_no_it_cannot_read_refuses_its_row(api, db, buyer):
    lines = [
        line(refund_amount="about 200"),
        line(order_id="OD338967210239333102", unit_id=UNITS[1], cancelled_by_user="maybe"),
        line(order_id="OD338967210239333103", unit_id=UNITS[2], refund_amount="-5"),
    ]

    out = upload(api, report(*lines)).json()

    assert out["created"] == 0
    reasons = {r["row"]: r["reason"] for r in out["rejected"]}
    assert reasons[2].startswith("refund_amount is not an amount")
    assert reasons[3] == "cancelled_by_user must be Yes or No"
    assert reasons[4] == "refund_amount cannot be negative"


def test_the_order_details_answer_names_the_courier(api, db, buyer):
    """The owner's API document gives each shipment a "carrier"."""
    upload(api, report(line(shipment_partner="Ekart Logistics", tracking_id="FMPP0012345678")))
    application = FastAPI()
    application.include_router(order_details.router)
    details = TestClient(application, headers={"Authorization": f"Bearer {create_access_token('ops@test.com')}"})

    data = details.post("/orders/orderDetails", json={"orderId": ORDER}).json()["data"]

    (shipment,) = data["shipments"]
    assert (shipment["carrier"], shipment["trackingNo"]) == ("Ekart Logistics", "FMPP0012345678")
