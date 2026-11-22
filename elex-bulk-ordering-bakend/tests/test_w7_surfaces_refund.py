"""A card order Flipkart cancelled reads REFUNDED once every unit of it that
carries Flipkart's wording reads a completed refund (review of 27 Oct 2026,
surfaces).

Two units asked for; Flipkart lists one, cancels it under its seller policy
and later refunds it. The other was never listed: a placeholder with no
wording, cancelled with its order (app.core.cancellations.unit_was_cancelled),
which can never read "Refund Completed" — so the Order Details API said
REFUND_DUE for ever, while the order dialog's payment line for the listed
unit said "refunded". The refund is now judged on the cancelled units that
carry Flipkart's wording.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import order_details
from app.automation.sync import ScrapedOrder
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus, OrderUnit
from app.jobs import sync_tasks

#: Flipkart's own reason, read back live on 24 and 25 Oct 2026.
SELLER_POLICY = "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand."
#: A cancelled order's page: its "Cancelled" step stays once it is refunded.
CANCELLED_STEPS = [
    {"status": "Order Confirmed", "date": "2026-10-24T00:00:00"},
    {"status": "Cancelled", "date": "2026-10-24T00:00:00"},
]


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(order_details.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def buyer(db) -> Account:
    row = Account(email="refund@test.com", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    return row


def _paid(api, account: Account, order_id: str) -> tuple[str, str]:
    """``(method, status)`` of the order's payment, the same in full and in the list."""
    detail = api.post("/orders/orderDetails", json={"orderId": order_id}).json()["data"]
    listed = api.post("/orders/orderDetails", json={"emailid": account.email}).json()["data"]
    (summary,) = [entry for entry in listed if entry["orderId"] == order_id]
    (payment,) = detail["payments"]
    assert (summary["paymentMethod"], summary["paymentStatus"]) == (payment["method"], payment["status"])
    return payment["method"], payment["status"]


def test_a_refunded_card_order_with_a_unit_flipkart_never_listed_reads_refunded(api, db, buyer):
    order = Order(
        account_id=buyer.id,
        order_id="OD338967210239377031",
        status=OrderStatus.placed,
        model="vivo T5 Lite 44W 5G",
        qty=2,
        amount=36414.0,
        payment_mode="card",
        placed_at=datetime(2026, 10, 24, 10, 0),
    )
    db.add(order)
    db.commit()
    sync_tasks._reconcile(
        buyer.id,
        [
            ScrapedOrder(
                order_id=order.order_id,
                unit_id="r1",
                platform_status="Cancelled",
                cancel_reason=SELLER_POLICY,
                timeline=CANCELLED_STEPS,
                payment_method="Credit Card",
                amount_paid=18207.0,
            )
        ],
        import_unknown=False,
    )
    db.expire_all()
    stored = db.get(Order, order.id)
    assert stored.status == OrderStatus.failed
    assert [unit.platform_status for unit in sorted(stored.units, key=lambda unit: unit.position)] == ["Cancelled", None]
    assert _paid(api, buyer, order.order_id) == ("CARD", "REFUND_DUE")

    refunded = [*CANCELLED_STEPS, {"status": "Refund Completed", "date": "2026-10-27T00:00:00"}]
    sync_tasks._reconcile(
        buyer.id,
        [ScrapedOrder(order_id=order.order_id, unit_id="r1", platform_status="Refund Completed", timeline=refunded)],
        import_unknown=False,
    )
    # The listed unit's money is back; the unit never listed was never charged
    # on a page of its own, and holds no wording that could ever say so.
    assert _paid(api, buyer, order.order_id) == ("CARD", "REFUNDED")


def test_nothing_read_says_the_money_is_back_so_it_is_still_owed():
    """No cancelled unit carries any wording: nothing says a refund was made,
    so the money stays owed back rather than claimed refunded."""
    order = Order(status=OrderStatus.failed, order_id="OD338967210239377032", payment_mode="card")
    for position in (1, 2):
        order.units.append(OrderUnit(platform="flipkart", position=position, platform_status=None))
    assert order_details._payment(order) == ("CARD", "REFUND_DUE")
