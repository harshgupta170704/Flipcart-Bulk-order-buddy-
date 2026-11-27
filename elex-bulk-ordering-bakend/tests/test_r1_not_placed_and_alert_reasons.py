"""11 Nov 2026: what the owner's own data showed wrong after a fortnight off.

* Flipkart's "Order Not Placed" (a card payment the bank never confirmed) read
  as on its way: the order stood as placed, counted as to be delivered and
  went overdue. It now reads cancelled — never placed — everywhere, and
  start-up corrects an order that only stood because of the old reading.
* A failed attempt's alert said "Order 1 failed for …" and nothing else. It
  now gives the run's own reason, and the ledger row carries it too.
* A fortnight of rehearsals left the queue full of run outcomes nobody could
  act on; they now leave it after a few days, while an unconfirmed order's
  alert stays until someone settles the order.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core import alerts, order_status
from app.core.order_units import every_read_unit_cancelled, sync_unit_readings
from app.db.models import (
    Account,
    AccountStatus,
    Alert,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    utcnow,
)

OD = "OD338996689296590100"


@pytest.fixture
def account(db) -> Account:
    row = Account(email="buyer@test.com", phone="6000000001", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    return row


def _order(db, account: Account, **fields) -> Order:
    defaults = {"account_id": account.id, "status": OrderStatus.placed, "model": "Pigeon 3 L", "qty": 1}
    order = Order(**{**defaults, **fields})
    db.add(order)
    db.commit()
    return order


def _read_back(db, order: Order, wording: str, *, state: str | None = None) -> None:
    """The order's first unit as a read-back left it, with a stored reading."""
    unit = order.units[0]
    unit.platform_status = wording
    unit.synced_at = utcnow()
    unit.delivery_state = state or order_status.classify(wording)
    order.platform_status = wording
    order.delivery_state = unit.delivery_state
    db.commit()


def _open(db, kind: str | None = None) -> list[Alert]:
    rows = db.scalars(select(Alert).where(Alert.resolved.is_(False)).order_by(Alert.id)).all()
    return [row for row in rows if kind is None or row.kind == kind]


# --- the reading -----------------------------------------------------------------
@pytest.mark.parametrize(
    "wording",
    ["Order Not Placed", "Not placed", "Order could not be placed", "Payment Failed", "Your payment has failed"],
)
def test_an_order_never_placed_reads_cancelled_not_on_its_way(wording):
    assert order_status.classify(wording) == order_status.CANCELLED
    assert order_status.never_placed(wording)
    assert order_status.is_settled(wording)


@pytest.mark.parametrize("wording", ["Order Placed", "Your order has been placed", "Order Confirmed", "Payment pending"])
def test_an_order_that_was_placed_still_reads_on_its_way(wording):
    assert order_status.classify(wording) == order_status.IN_TRANSIT
    assert not order_status.never_placed(wording)


@pytest.mark.parametrize("wording", ["Seller Cancelled", "Auto Cancelled", "Order Cancelled", "Cancelled - Order shipped"])
def test_cancelled_is_read_wherever_the_wording_puts_it(wording):
    assert order_status.classify(wording) == order_status.CANCELLED
    assert not order_status.never_placed(wording)


@pytest.mark.parametrize("wording", ["Return Cancelled", "Replacement Cancelled", "Return request cancelled"])
def test_a_withdrawn_return_is_still_no_cancellation_of_the_order(wording):
    assert order_status.classify(wording) == order_status.RETURNED


def test_a_request_to_cancel_is_not_a_cancellation():
    assert order_status.classify("Cancellation requested") == order_status.UNKNOWN


@pytest.mark.parametrize(
    "wording", ["Shipment delayed", "Delivery attempted", "Seller has processed your order", "On hold", "Confirmed"]
)
def test_more_wordings_of_a_unit_still_to_come(wording):
    assert order_status.classify(wording) == order_status.IN_TRANSIT


# --- start-up brings an order in line with the new reading ---------------------------
def test_start_up_fails_an_order_that_stood_only_because_of_the_old_reading(db, account):
    order = _order(db, account, order_id=OD, payment_mode="card", placed_at=utcnow())
    _read_back(db, order, "Order Not Placed", state="in_transit")  # as stored before 11 Nov 2026

    changed = sync_unit_readings()

    db.refresh(order)
    assert changed["orders"] == 1
    assert order.status == OrderStatus.failed
    assert order.units[0].delivery_state == order_status.CANCELLED
    assert sync_unit_readings() == {"states": 0, "timelines": 0, "orders": 0}  # and only once


def test_start_up_leaves_an_order_with_a_unit_still_on_its_way(db, account):
    order = _order(db, account, order_id=OD, qty=2, placed_at=utcnow())
    first, second = order.units
    first.platform_status, first.delivery_state, first.synced_at = "Order Not Placed", "in_transit", utcnow()
    second.platform_status, second.delivery_state, second.synced_at = "Shipped", "in_transit", utcnow()
    db.commit()

    sync_unit_readings()

    db.refresh(order)
    assert order.status == OrderStatus.placed
    assert not every_read_unit_cancelled(order)


def test_start_up_never_fails_an_attempt_flipkart_gave_no_order_id(db, account):
    order = _order(db, account, status=OrderStatus.placed, order_id=None, placed_at=utcnow())
    _read_back(db, order, "Order Not Placed", state="in_transit")
    sync_unit_readings()
    db.refresh(order)
    assert order.status == OrderStatus.placed


# --- what the alerts say ---------------------------------------------------------------
def test_an_order_never_placed_is_said_for_what_it_is_and_never_overdue(db, account):
    order = _order(db, account, order_id=OD, payment_mode="card", placed_at=utcnow() - timedelta(days=14))
    _read_back(db, order, "Order Not Placed")
    order.status = OrderStatus.failed  # as the read-back, or start-up, now leaves it
    db.commit()

    alerts.evaluate(db)

    assert _open(db, "delivery_overdue") == []
    (alert,) = _open(db, "order_failed")
    assert f"Order {OD}" in alert.message
    assert "was not placed" in alert.message and "Order Not Placed" in alert.message
    assert "cancelled by Flipkart" not in alert.message
    assert alert.details["never_placed"] is True


def test_a_failed_attempt_says_why_in_the_runs_own_words(db, account):
    job = Job(type=JobType.order, status=JobStatus.completed)
    db.add(job)
    db.commit()
    db.add(
        JobItem(
            job_id=job.id,
            account_id=account.id,
            status=JobItemStatus.failed,
            message="not placed (failed): Cash on Delivery is not available for this product — nothing was charged",
        )
    )
    order = _order(db, account, status=OrderStatus.failed, job_id=job.id, placed_at=None)

    alerts.evaluate(db)

    (alert,) = _open(db, "order_failed")
    assert alert.message == (
        "The order (Pigeon 3 L) for buyer@test.com was not placed: "
        "Cash on Delivery is not available for this product — nothing was charged"
    )
    assert f"Order {order.id} " not in alert.message  # never the ledger's own row number


def _never_placed_then_failed_attempt(db, account) -> tuple[Order, Order]:
    """The live data of 23 Nov 2026, in its order: row 56, a card order
    Flipkart says "Order Not Placed" (run #629), then row 58, an attempt that
    never reached Flipkart, with its run's own line (run #641)."""
    never = _order(db, account, order_id=OD, payment_mode="card", placed_at=utcnow() - timedelta(hours=2))
    _read_back(db, never, "Order Not Placed")
    never.status = OrderStatus.failed  # as the read-back, or start-up, now leaves it
    db.commit()
    job = Job(type=JobType.order, status=JobStatus.completed)
    db.add(job)
    db.commit()
    db.add(
        JobItem(
            job_id=job.id,
            account_id=account.id,
            status=JobItemStatus.failed,
            message="not placed (failed): The account's GST could not be changed — nothing was charged",
        )
    )
    attempt = _order(db, account, status=OrderStatus.failed, job_id=job.id, placed_at=None)
    assert never.id < attempt.id
    return never, attempt


def test_a_never_placed_order_before_a_failed_attempt_keeps_the_pass_going(db, account):
    """Until 24 Nov 2026 the never-placed branch reused ``said``, the name of
    the run lines looked up for the attempts: the next attempt met a string,
    the pass failed, and no alert of any kind was raised or closed."""
    never, attempt = _never_placed_then_failed_attempt(db, account)

    counts = alerts.evaluate(db)

    assert counts["order_failed"] == 2
    by_order = {alert.order_id: alert for alert in _open(db, "order_failed")}
    assert 'Flipkart says "Order Not Placed"' in by_order[never.id].message
    assert by_order[never.id].details["cancel_reason"] == "Order Not Placed"
    assert by_order[attempt.id].message == (
        "The order (Pigeon 3 L) for buyer@test.com was not placed: "
        "The account's GST could not be changed — nothing was charged"
    )


def test_the_evaluate_button_answers_with_both_kinds_of_failure(db, account, client, auth_headers):
    _never_placed_then_failed_attempt(db, account)

    response = client.post("/alerts/evaluate", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json()["counts"]["order_failed"] == 2


def test_a_failed_attempt_with_no_run_line_still_reads_as_a_sentence(db, account):
    _order(db, account, status=OrderStatus.failed, placed_at=None)
    alerts.evaluate(db)
    (alert,) = _open(db, "order_failed")
    assert alert.message.endswith("the run stopped before anything was sent to Flipkart")


def test_run_outcomes_leave_the_queue_after_a_few_days(db, account):
    old = utcnow() - timedelta(days=alerts.RUN_OUTCOME_WINDOW_DAYS + 1)
    _order(db, account, status=OrderStatus.failed, placed_at=None, created_at=old)
    _order(db, account, status=OrderStatus.over_target, amount=698, target=1, placed_at=None, created_at=old)
    fresh = _order(db, account, status=OrderStatus.over_target, amount=698, target=1, placed_at=None)

    alerts.evaluate(db)

    assert [alert.order_id for alert in _open(db)] == [fresh.id]


def test_a_cancellation_stays_a_fortnight(db, account):
    recent = _order(db, account, status=OrderStatus.failed, order_id=OD, created_at=utcnow() - timedelta(days=10))
    _order(
        db,
        account,
        status=OrderStatus.failed,
        order_id="OD338980309549917100",
        created_at=utcnow() - timedelta(days=alerts.CANCELLED_WINDOW_DAYS + 1),
    )
    alerts.evaluate(db)
    assert [alert.order_id for alert in _open(db, "order_failed")] == [recent.id]


def test_an_unconfirmed_orders_alert_stays_until_the_order_is_settled(db, account):
    order = _order(
        db, account, status=OrderStatus.unconfirmed, amount=638, created_at=utcnow() - timedelta(days=200)
    )
    alerts.evaluate(db)
    (alert,) = _open(db, "order_unconfirmed")
    assert alert.order_id == order.id and "say what you found" in alert.message


def test_settling_an_unconfirmed_order_closes_its_alert_at_once(db, account, client, auth_headers):
    order = _order(db, account, status=OrderStatus.unconfirmed, amount=638, placed_at=utcnow())
    alerts.evaluate(db)
    assert len(_open(db, "order_unconfirmed")) == 1

    resp = client.post(f"/orders/{order.id}/settle", json={"status": "failed"}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    db.expire_all()
    assert _open(db, "order_unconfirmed") == []


# --- the ledger row and the API ----------------------------------------------------------
def test_the_ledger_row_carries_the_runs_reason_and_can_be_opened_by_its_row(db, account, client, auth_headers):
    job = Job(type=JobType.order, status=JobStatus.completed)
    db.add(job)
    db.commit()
    db.add(
        JobItem(
            job_id=job.id,
            account_id=account.id,
            status=JobItemStatus.failed,
            message="not placed (failed): could not place the cart order: Place Order did not respond",
        )
    )
    failed = _order(db, account, status=OrderStatus.failed, job_id=job.id, placed_at=None)
    placed = _order(db, account, order_id=OD, job_id=None, placed_at=utcnow())

    page = client.get("/orders", params={"row": failed.id}, headers=auth_headers).json()

    assert page["total"] == 1
    (row,) = page["items"]
    assert row["order_row_id"] == failed.id
    assert row["run_message"] == "Could not place the cart order: Place Order did not respond"

    everything = client.get("/orders", headers=auth_headers).json()
    by_row = {item["order_row_id"]: item for item in everything["items"]}
    assert by_row[placed.id]["run_message"] is None


def test_a_card_order_never_placed_owes_no_refund(db, account, client, auth_headers):
    order = _order(db, account, order_id=OD, payment_mode="card", amount=638, placed_at=utcnow())
    _read_back(db, order, "Order Not Placed")
    order.status = OrderStatus.failed
    db.commit()

    resp = client.post("/orders/orderDetails", json={"orderIds": [OD]}, headers=auth_headers)

    assert resp.status_code == 200, resp.text
    assert '"NOT_CHARGED"' in resp.text and "REFUND_DUE" not in resp.text


def test_many_alerts_are_resolved_in_one_call(db, account, client, auth_headers):
    for _ in range(3):
        _order(db, account, status=OrderStatus.over_target, amount=698, target=1, placed_at=None)
    alerts.evaluate(db)
    ids = [alert.id for alert in _open(db)]
    assert len(ids) == 3

    resp = client.post(
        "/alerts/resolve", json={"ids": ids[:2] + [999_999], "note": "test runs"}, headers=auth_headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"resolved": 2, "open": 1}
    db.expire_all()
    closed = db.scalars(select(Alert).where(Alert.id.in_(ids[:2]))).all()
    assert all(alert.resolved and alert.resolution_note == "test runs" for alert in closed)
    # An acceptance, like a single resolve: the next pass leaves them closed.
    alerts.evaluate(db)
    assert len(_open(db)) == 1


def test_resolving_many_needs_at_least_one_id(client, auth_headers):
    assert client.post("/alerts/resolve", json={"ids": []}, headers=auth_headers).status_code == 422
