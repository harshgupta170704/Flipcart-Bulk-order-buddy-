"""The order's own status counts a unit an order report filed (review of
27 Oct 2026, surfaces).

A report files a unit's status without any read-back seeing it, so the unit's
``synced_at`` stays empty on purpose (app.io.order_report: a report never
passes for Flipkart's own page). The unit that stands for the order
(app.core.order_units.representative_unit) was picked from the units read
back alone as soon as there was one: a two-unit order with one unit read back
delivered and the other filed as shipped read DELIVERED in the Order Details
API, while its own items said SHIPPED and the dashboard's "To be delivered"
counted the shipped unit. A unit that carries a reading now counts, whoever
wrote it; a placeholder Flipkart never listed still decides nothing.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import order_details
from app.api import orders as orders_api
from app.automation.sync import ScrapedOrder
from app.core.order_units import representative_unit
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus, OrderUnit
from app.io import order_report
from app.jobs import sync_tasks

#: Made up, in Flipkart's shapes.
ORDER_ID = "OD338975799606677001"
FILED_UNIT_ID = "439123456789012377002"


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(order_details.router)
    # The report upload (POST /orders/import-report) is the ledger's own route.
    application.include_router(orders_api.router)
    token = create_access_token("ops@test.com", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def buyer(db) -> Account:
    account = Account(email="filed@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    return account


def _report(**cells: str) -> bytes:
    """A one-row order report in the template's columns; give only what the
    test is about."""
    values = {name: "" for name in order_report.TEMPLATE_HEADERS}
    values.update(platform="flipkart", order_date="24-10-2026", product="vivo T5 Lite 44W 5G", **cells)
    lines = [",".join(order_report.TEMPLATE_HEADERS), ",".join(values[name] for name in order_report.TEMPLATE_HEADERS)]
    return "\n".join(lines).encode("utf-8")


def test_a_unit_a_report_filed_as_shipped_keeps_its_order_from_reading_delivered(api, db, buyer):
    order = Order(
        account_id=buyer.id,
        order_id=ORDER_ID,
        status=OrderStatus.placed,
        model="vivo T5 Lite 44W 5G",
        qty=2,
        amount=36414.0,
        payment_mode="card",
        placed_at=datetime(2026, 10, 24, 10, 0),
    )
    db.add(order)
    db.commit()
    # The read-back lists one unit of the two, delivered (its list cut at its row limit) …
    sync_tasks._reconcile(
        buyer.id, [ScrapedOrder(order_id=ORDER_ID, unit_id="r1", platform_status="Delivered")], import_unknown=False
    )
    # … and the operator files the other from a report: shipped.
    response = api.post(
        "/orders/import-report",
        files={"file": ("report.csv", _report(order_id=ORDER_ID, unit_id=FILED_UNIT_ID, status="Shipped"), "text/csv")},
    )
    assert response.status_code == 200, response.text
    assert (response.json()["created"], response.json()["rejected"]) == (1, [])
    db.expire_all()
    units = sorted(db.get(Order, order.id).units, key=lambda unit: unit.position)
    assert [(unit.unit_id, unit.platform_status, unit.synced_at is not None) for unit in units] == [
        ("R1", "Delivered", True),
        (FILED_UNIT_ID, "Shipped", False),
    ]

    detail = api.post("/orders/orderDetails", json={"orderId": ORDER_ID}).json()["data"]
    (summary,) = api.post("/orders/orderDetails", json={"emailid": buyer.email}).json()["data"]
    assert [item["status"] for item in detail["items"]] == ["DELIVERED", "SHIPPED"]
    # One unit of two still on its way is not a delivered order, whoever said so.
    for answer in (detail, summary):
        assert (answer["status"], answer["platformStatus"], answer["deliveryState"]) == ("SHIPPED", "Shipped", "in_transit")


@pytest.mark.parametrize(
    ("units", "stands"),
    [
        # (Flipkart's wording, how it came: "read" by a read-back, "filed" by
        # a report, or None for a placeholder nothing was ever read of), by position.
        pytest.param([("Delivered", "read"), ("Shipped", "filed")], 2, id="a-filed-unit-on-its-way-stands-for-the-order"),
        pytest.param([("Cancelled", "read"), ("Delivered", "filed")], 2, id="a-filed-delivery-before-a-cancellation"),
        pytest.param([("Delivered", "filed"), (None, None)], 1, id="a-placeholder-beside-filed-units-decides-nothing"),
        pytest.param([("Delivered", "read"), (None, None)], 1, id="a-placeholder-beside-read-units-decides-nothing"),
        pytest.param([(None, None), (None, None)], 1, id="nothing-read-of-any-unit-every-unit-counts"),
    ],
)
def test_a_unit_that_carries_a_reading_counts_and_a_placeholder_never_does(units, stands):
    order = Order(status=OrderStatus.placed, order_id=ORDER_ID)
    for position, (wording, came) in enumerate(units, start=1):
        order.units.append(
            OrderUnit(
                platform="flipkart",
                position=position,
                source="upload" if came == "filed" else "run",
                platform_status=wording,
                synced_at=datetime(2026, 10, 27, 9, 0) if came == "read" else None,
            )
        )
    assert representative_unit(order).position == stands
