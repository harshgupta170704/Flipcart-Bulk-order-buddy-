"""What the checker of the surfaces fixes found (27 Oct 2026).

- An order the read-back failed keeps the reading of the units it read: a unit
  a report filed since must not give the failed order a "Shipped" status.
- A refund is judged on every cancelled unit that says anything — read back,
  or carrying a reason — not only on the units whose status line was read.
- An order naming its business without a GSTIN takes the GSTIN Flipkart's
  page billed when the page billed that very business.
"""

from __future__ import annotations

from datetime import datetime

from app.api import order_details
from app.core.order_units import representative_unit
from app.db.models import Order, OrderStatus, OrderUnit
from app.io import orders_csv

ORDER_ID = "OD338975799606677101"
READ = datetime(2026, 10, 26, 6, 0)


def _unit(position: int, status: str | None, *, read: bool, reason: str | None = None) -> OrderUnit:
    return OrderUnit(
        platform="flipkart",
        position=position,
        unit_id=f"43912345678901237710{position}",
        platform_status=status,
        cancel_reason=reason,
        synced_at=READ if read else None,
    )


def test_a_failed_order_keeps_the_reading_of_the_units_the_read_back_read():
    order = Order(status=OrderStatus.failed, order_id=ORDER_ID)
    order.units.append(_unit(1, "Cancelled", read=True, reason="cancelled as per seller policy"))
    order.units.append(_unit(2, "Shipped", read=False))  # filed by a report since

    assert representative_unit(order).platform_status == "Cancelled"

    # An order that stands still counts the filed unit (the review's case).
    order.status = OrderStatus.placed
    assert representative_unit(order).platform_status == "Shipped"


def test_a_refund_waits_on_a_listed_unit_whose_status_line_was_not_read():
    order = Order(status=OrderStatus.failed, order_id=ORDER_ID, payment_mode="card")
    order.units.append(_unit(1, "Refund Completed", read=True))
    # Listed and cancelled by Flipkart (its reason is stored) but no status
    # line read: nothing says its money is back.
    order.units.append(_unit(2, None, read=True, reason="cancelled as per seller policy"))

    assert order_details._payment(order)[1] == "REFUND_DUE"

    # A placeholder Flipkart never listed still decides nothing.
    order.units[1] = _unit(2, None, read=False)
    assert order_details._payment(order)[1] == "REFUNDED"


def test_the_billed_gstin_is_used_when_the_page_billed_the_business_the_order_names():
    order = Order(gst_name="Two States Pvt Ltd", gstin=None)
    unit = OrderUnit(billed_business_name="  two states pvt ltd ", billed_gstin="27PQRST6789K1Z0")

    # The GST book holds several registrations for the name: none chosen.
    assert orders_csv.business_gst(order, unit, None) == ("Two States Pvt Ltd", "27PQRST6789K1Z0")
    # The book's one registration still wins where there is one.
    assert orders_csv.business_gst(order, unit, "09PQRST6789K1Z2") == ("Two States Pvt Ltd", "09PQRST6789K1Z2")
    # Another business billed: never its GSTIN beside this name.
    other = OrderUnit(billed_business_name="Transparent Deals", billed_gstin="09AAUFT2204P1ZI")
    assert orders_csv.business_gst(order, other, None) == ("Two States Pvt Ltd", "")
