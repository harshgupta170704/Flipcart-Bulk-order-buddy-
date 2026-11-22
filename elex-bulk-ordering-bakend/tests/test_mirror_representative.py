"""The order row mirrors the unit the Order Details API also stands on
(order_units.representative_unit, 27 Oct 2026)."""

from app.automation.sync import ScrapedOrder
from app.db.models import Account, AccountStatus, Order, OrderStatus
from app.jobs import sync_tasks


def _order(db, qty):
    account = Account(email="mirror@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    order = Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=qty, status=OrderStatus.placed, order_id="OD9")
    db.add(order)
    db.commit()
    return account, order


def _row(db, order):
    db.expire_all()
    return db.get(Order, order.id)


def test_a_unit_delivered_and_one_cancelled_reads_delivered(db):
    account, order = _order(db, 2)
    sync_tasks._reconcile(
        account.id,
        [
            ScrapedOrder(order_id="OD9", unit_id="U1", platform_status="Cancelled"),
            ScrapedOrder(order_id="OD9", unit_id="U2", platform_status="Delivered"),
        ],
        import_unknown=False,
    )
    row = _row(db, order)
    assert row.platform_status == "Delivered" and row.status == OrderStatus.placed


def test_a_unit_still_on_its_way_stands_for_the_order(db):
    account, order = _order(db, 3)
    sync_tasks._reconcile(
        account.id,
        [
            ScrapedOrder(order_id="OD9", unit_id="U1", platform_status="Delivered"),
            ScrapedOrder(order_id="OD9", unit_id="U2", platform_status="Cancelled"),
            ScrapedOrder(order_id="OD9", unit_id="U3", platform_status="Out For Delivery"),
        ],
        import_unknown=False,
    )
    assert _row(db, order).platform_status == "Out For Delivery"


def test_all_cancelled_still_fails_it(db):
    account, order = _order(db, 2)
    sync_tasks._reconcile(
        account.id,
        [
            ScrapedOrder(order_id="OD9", unit_id="U1", platform_status="Cancelled"),
            ScrapedOrder(order_id="OD9", unit_id="U2", platform_status="Cancelled"),
        ],
        import_unknown=False,
    )
    assert _row(db, order).status == OrderStatus.failed
