"""Which placeholder a report's new unit id goes to (review of 27 Oct 2026).

A read-back that opens an order by id reads a page with no unit links and
files what it saw on the next placeholder: a reading with no unit id. The
report gave its first new unit id to the first placeholder by position even
when that one held another unit's reading — which, the read-back's word
winning, then stood over the report's for that unit.
"""

from __future__ import annotations

from datetime import datetime

from app.db.models import Order, OrderStatus
from tests.w7_report_rows import ORDER, UNITS, api, buyer, line, report, units_of, upload  # noqa: F401 — fixtures


def _order_with_a_read_placeholder(db, buyer) -> None:
    """A run's order of two whose first placeholder the read-back filled with
    a delivered unit's reading, no unit id; the second is still unread."""
    order = Order(account_id=buyer.id, status=OrderStatus.placed, order_id=ORDER, qty=2, model="vivo")
    db.add(order)
    db.commit()
    first, _second = units_of(db)
    first.synced_at = datetime(2026, 10, 26, 6, 0)
    first.platform_status = "Delivered"
    first.delivered_date = datetime(2026, 10, 25)
    db.commit()


def test_a_unit_on_its_way_does_not_take_a_delivered_units_reading(api, db, buyer):
    _order_with_a_read_placeholder(db, buyer)

    out = upload(api, report(line(unit_id=UNITS[1], status="Shipped"))).json()

    assert (out["created"], out["kept_platform"]) == (1, 0)
    first, second = units_of(db)
    assert (first.unit_id, first.platform_status) == (None, "Delivered")
    assert (second.unit_id, second.platform_status) == (UNITS[1], "Shipped")


def test_a_unit_whose_row_agrees_with_the_reading_takes_it(api, db, buyer):
    _order_with_a_read_placeholder(db, buyer)

    upload(
        api,
        report(
            line(unit_id=UNITS[1], status="Shipped"),
            line(unit_id=UNITS[0], status="Delivered", actual_delivery_date="25-10-2026"),
        ),
    )

    first, second = units_of(db)
    assert (first.unit_id, first.platform_status) == (UNITS[0], "Delivered")
    assert (second.unit_id, second.platform_status) == (UNITS[1], "Shipped")


def test_with_no_other_placeholder_left_the_first_by_position_is_taken(api, db, buyer):
    """No unit appears out of nowhere: the reading may be this unit's own,
    read a day earlier, and the read-back's word stands until the next one."""
    _order_with_a_read_placeholder(db, buyer)
    upload(api, report(line(unit_id=UNITS[1], status="Shipped")))

    out = upload(api, report(line(unit_id=UNITS[2], status="Shipped"))).json()

    assert (out["created"], out["kept_platform"]) == (1, 1)
    assert [unit.unit_id for unit in units_of(db)] == [UNITS[2], UNITS[1]]
