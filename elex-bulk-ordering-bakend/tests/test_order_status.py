"""The portal's one reading of a platform status, and its SQL twin.

Until 2026-10-24 the sync scraper, the alert rules and the orders table each
kept their own list of what "delivered" and "cancelled" mean, with two
different answers (the table counted a return as a failure; the backend
counted it as nothing). A dashboard built on one and a table on another would
have disagreed. Everything now asks app.core.order_status; these tests pin the
wording Flipkart actually uses and that the alert rule's SQL agrees with it.
"""

import pytest
from sqlalchemy import select

from app.core import alerts, order_status
from app.core.order_status import (
    CANCELLED,
    DELIVERED,
    FAILED,
    IN_TRANSIT,
    RETURNED,
    UNKNOWN,
    classify,
    is_cancelled,
    is_delivered,
    is_settled,
    refund_completed,
)
from app.db.models import Account, AccountStatus, Order, OrderStatus

#: Flipkart's own wordings, from the live orders list and the spec's status
#: picker (ui-evidence.md S4), with the reading each must get.
WORDINGS = [
    ("Delivered", DELIVERED),
    ("Delivered on Fri Oct 23", DELIVERED),
    ("Cancelled", CANCELLED),
    ("Canceled", CANCELLED),
    ("Cancelled Today", CANCELLED),
    ("Cancellation requested", UNKNOWN),  # a request the platform may still refuse
    ("Cancelled - Order shipped", CANCELLED),
    ("Cancelled by user - Order confirmed but not shipped", CANCELLED),
    ("Order Cancelled", CANCELLED),
    # A withdrawn return or replacement: the order stands, the unit stays
    # delivered. Reading these as a cancellation would flip a placed order to
    # failed for good and hand its quota back.
    ("Return Cancelled", RETURNED),
    ("Replacement Cancelled", RETURNED),
    ("Pickup Cancelled", UNKNOWN),  # not a cancellation of the order; unfamiliar, so never guessed
    ("Return started", RETURNED),
    ("Returned", RETURNED),
    ("Refunded", RETURNED),
    ("Undelivered", FAILED),
    ("Lost in transit", FAILED),
    ("Out for delivery", IN_TRANSIT),
    ("Out for Delivery", IN_TRANSIT),
    ("Shipped", IN_TRANSIT),
    ("Dispatched", IN_TRANSIT),
    ("Ready to ship", IN_TRANSIT),
    ("Arriving by Mon Oct 28", IN_TRANSIT),
    ("Order received", IN_TRANSIT),
    ("Order Confirmed", IN_TRANSIT),
    ("Approved", IN_TRANSIT),
    ("Payment not received", UNKNOWN),  # the spec files it under "Others"
    ("Partially Delivered", UNKNOWN),  # neither delivered nor not: never guessed
    ("", UNKNOWN),
    (None, UNKNOWN),
    ("Something Flipkart invents later", UNKNOWN),
]


@pytest.mark.parametrize(("wording", "expected"), WORDINGS)
def test_classify(wording, expected):
    assert classify(wording) == expected


def test_a_cancellation_that_mentions_shipping_is_still_a_cancellation():
    """The one ordering that matters: "Cancelled - Order shipped" is cancelled,
    "Undelivered" is not delivered."""
    assert classify("Cancelled - Order shipped") == CANCELLED
    assert classify("Undelivered") == FAILED
    assert is_delivered("Undelivered") is False


def test_predicates_follow_the_classification():
    assert is_delivered("Delivered") and not is_delivered("Out for delivery")
    assert is_cancelled("Cancelled - Order shipped") and not is_cancelled("Shipped")
    assert is_settled("Returned") and is_settled("Lost") and not is_settled("Shipped")
    assert not is_settled(None)


#: Wordings of a unit's money and whether the order dialog's payment line says
#: "— refunded" for each (returnedPayment, from paidLine, in
#: frontend/src/components/OrderDetail.tsx); refund_completed answers the same.
REFUND_WORDINGS = [
    ("Refund Completed", True),  # Flipkart's own, once a cancelled card order's money is back
    ("refund completed", True),
    ("Refund Credited", True),
    ("Refund successful", True),
    ("Refund Initiated", False),  # under way: still owed
    ("Refunded", False),  # no word that it is done: the dialog says "refund due"
    ("Return Requested", False),
    ("Returned", False),
    ("Return Cancelled", False),  # a return withdrawn: the unit stays delivered, nothing moves
    ("Replacement Completed", False),  # an item swapped, no money back
    ("Return to Origin - refund completed", False),  # taken back undelivered: the dialog's own branch
    ("Return Cancelled - refund completed", False),  # a withdrawn return is no refund done, whatever follows
    ("Replacement - refund completed", False),  # a replacement moves no money back
    ("Partially refunded, refund completed", False),  # not read as returned, so never asked
    ("Cancelled", False),  # read as cancelled: the dialog says "refund due"
    ("Delivered", False),
    ("", False),
    (None, False),
]


@pytest.mark.parametrize(("wording", "refunded"), REFUND_WORDINGS)
def test_a_completed_refund_reads_as_the_order_dialog_reads_it(wording, refunded):
    assert refund_completed(wording) is refunded


def test_the_sync_module_uses_the_shared_reading():
    from app.automation import sync

    assert sync.is_cancelled is order_status.is_cancelled
    assert sync.is_delivered is order_status.is_delivered


def test_the_alert_rules_sql_agrees_with_the_python_reading(db):
    """_not_settled() is SQL and cannot call classify(); it must say the same
    thing for every wording above, or an order could be overdue in one place
    and settled in another."""
    account = Account(email="states@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    expected_unsettled: set[int] = set()
    for wording, _ in WORDINGS:
        row = Order(account_id=account.id, status=OrderStatus.placed, platform_status=wording)
        db.add(row)
        db.flush()
        if not is_settled(wording):
            expected_unsettled.add(row.id)
    db.commit()

    unsettled = set(db.scalars(select(Order.id).where(alerts._not_settled())).all())
    assert unsettled == expected_unsettled
