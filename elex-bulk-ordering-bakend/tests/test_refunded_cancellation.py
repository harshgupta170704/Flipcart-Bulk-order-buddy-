"""A refunded cancellation is a cancellation, and an imported order's payment
is unknown until its page says cash on delivery (review of 27 Oct 2026)."""

from app.automation.sync import ScrapedOrder
from app.db.models import Account, AccountStatus, Order, OrderStatus
from app.jobs import sync_tasks

REASON = "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand."
CANCELLED_STEPS = [{"status": "Order Confirmed", "date": "2026-10-25T00:00:00"}, {"status": "Cancelled", "date": "2026-10-26T00:00:00"}]


def _order(db, *, payment_mode="card"):
    account = Account(email="refund@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    order = Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD5", payment_mode=payment_mode)
    db.add(order)
    db.commit()
    return account, order


def _read(account, **fields):
    return sync_tasks._reconcile(account.id, [ScrapedOrder(order_id="OD5", unit_id="U1", **fields)], import_unknown=True)


def _row(db, order_id="OD5"):
    db.expire_all()
    return db.query(Order).filter(Order.order_id == order_id).one()


def test_a_refunded_cancellation_with_a_reason_is_failed(db):
    account, _ = _order(db)
    tally = _read(account, platform_status="Refund Completed", cancel_reason=REASON)

    assert _row(db).status == OrderStatus.failed
    assert "1 cancelled — marked failed" in tally.message()


def test_a_refunded_cancellation_with_only_its_cancelled_step_is_failed(db):
    account, _ = _order(db)
    _read(account, platform_status="Refund Completed", timeline=CANCELLED_STEPS)
    assert _row(db).status == OrderStatus.failed


def test_on_its_way_then_refunded_is_failed(db):
    account, _ = _order(db)
    _read(account, platform_status="Delivery expected")
    assert _row(db).status == OrderStatus.placed

    _read(account, platform_status="Refund Completed", timeline=CANCELLED_STEPS)
    assert _row(db).status == OrderStatus.failed


def test_delivered_then_refunded_is_a_return_and_stays_placed(db):
    account, _ = _order(db)
    _read(account, platform_status="Delivered", delivered_date="2026-10-29T00:00:00")

    _read(account, platform_status="Refund Completed")

    assert _row(db).status == OrderStatus.placed


def test_an_imported_order_is_of_unknown_payment_until_its_page_says_cash_on_delivery(db):
    account = Account(email="imported@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()

    sync_tasks._reconcile(account.id, [ScrapedOrder(order_id="OD-HAND", unit_id="U1", platform_status="Shipped", payment_method="UPI")], import_unknown=True)
    assert _row(db, "OD-HAND").payment_mode == sync_tasks.PAYMENT_UNKNOWN

    sync_tasks._reconcile(account.id, [ScrapedOrder(order_id="OD-HAND", unit_id="U1", platform_status="Shipped", payment_method="Cash On Delivery")], import_unknown=True)
    assert _row(db, "OD-HAND").payment_mode == "cod"


def test_a_runs_own_card_is_never_overwritten(db):
    account, _ = _order(db, payment_mode="card")
    _read(account, platform_status="Shipped", payment_method="Cash On Delivery")
    assert _row(db).payment_mode == "card"
