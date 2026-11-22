"""A read-back says an order was cancelled once — when it marks it failed —
not at every hourly pass while the cancelled order stays in the recent list
(seen 26 Oct 2026: "4 cancelled — marked failed" every hour, for the same four)."""

from app.automation.sync import ScrapedOrder
from app.db.models import Account, AccountStatus, Order, OrderStatus
from app.jobs import sync_tasks


def test_a_cancellation_is_reported_once(db):
    account = Account(email="once@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    order = Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD1")
    db.add(order)
    db.commit()
    seen = [ScrapedOrder(order_id="OD1", unit_id="U1", platform_status="Cancelled")]

    first = sync_tasks._reconcile(account.id, seen, import_unknown=False)
    second = sync_tasks._reconcile(account.id, seen, import_unknown=False)

    assert first.cancelled == 1 and "1 cancelled — marked failed" in first.message()
    assert second.cancelled == 0 and "cancelled" not in second.message()
    db.expire_all()
    assert db.get(Order, order.id).status == OrderStatus.failed
