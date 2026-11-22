"""An order failed on the cancelled units one read-back saw goes back to
placed when another unit, read for the first time, is on its way (found by
review, 26 Oct 2026). The other units cannot be fetched on purpose: an order
page opened by id links none of them."""

from app.automation.sync import ScrapedOrder
from app.core.order_units import out_for_delivery_rows
from app.db.models import Account, AccountStatus, Order, OrderStatus, naive_utcnow
from app.jobs import schedule, sync_tasks


def _two_unit_order(db):
    account = Account(email="two@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    order = Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=2, status=OrderStatus.placed, order_id="OD7")
    db.add(order)
    db.commit()
    assert len(order.units) == 2
    return account, order


def _read(account, *observations):
    return sync_tasks._reconcile(account.id, list(observations), import_unknown=False)


def _status(db, order):
    db.expire_all()
    return db.get(Order, order.id).status


def test_the_cancelled_unit_seen_first_then_the_live_one_puts_it_back(db):
    account, order = _two_unit_order(db)

    _read(account, ScrapedOrder(order_id="OD7", unit_id="U1", platform_status="Cancelled"))
    assert _status(db, order) == OrderStatus.failed  # all it had seen was cancelled

    tally = _read(account, ScrapedOrder(order_id="OD7", unit_id="U2", platform_status="Out For Delivery", delivery_otp="4827"))

    assert _status(db, order) == OrderStatus.placed
    assert tally.revived == 1 and "back to placed" in tally.message()
    # ...so its code is on the dashboard's card and the timer follows it again.
    assert [o.id for _unit, o, _email in out_for_delivery_rows(db)] == [order.id]
    assert schedule.open_order_ids(db, account.id) == ["OD7"]


def test_both_seen_in_one_pass_it_never_fails(db):
    account, order = _two_unit_order(db)
    tally = _read(
        account,
        ScrapedOrder(order_id="OD7", unit_id="U1", platform_status="Cancelled"),
        ScrapedOrder(order_id="OD7", unit_id="U2", platform_status="Shipped"),
    )
    assert _status(db, order) == OrderStatus.placed and tally.revived == 0


def test_a_refund_never_puts_it_back(db):
    account, order = _two_unit_order(db)
    _read(account, ScrapedOrder(order_id="OD7", unit_id="U1", platform_status="Cancelled"))

    _read(account, ScrapedOrder(order_id="OD7", unit_id="U2", platform_status="Refund completed"))

    assert _status(db, order) == OrderStatus.failed


def test_a_unit_read_before_as_cancelled_does_not_flip_it_once_misread(db):
    account, order = _two_unit_order(db)
    _read(
        account,
        ScrapedOrder(order_id="OD7", unit_id="U1", platform_status="Cancelled"),
        ScrapedOrder(order_id="OD7", unit_id="U2", platform_status="Cancelled"),
    )

    _read(account, ScrapedOrder(order_id="OD7", unit_id="U2", platform_status="Shipped"))

    assert _status(db, order) == OrderStatus.failed


def test_an_order_with_no_id_is_never_touched(db):
    account, order = _two_unit_order(db)
    order.order_id, order.status = None, OrderStatus.failed
    second = order.units[1]
    second.platform_status, second.synced_at = "Shipped", naive_utcnow()  # read for the first time, on its way
    db.commit()

    assert sync_tasks._revive(order, cancelled=False, read_before=set()) is False
    assert order.status == OrderStatus.failed
