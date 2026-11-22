"""An order a report failed on a partial picture, put back by a later one
(review of 27 Oct 2026).

A report that brought only an order's cancelled unit — the other row refused —
failed the order, as every unit it knew read cancelled. A corrected report
then showing the other unit on its way left it failed for good: its units no
longer counted against limits, nothing followed it. The read-back mends the
same case (app.jobs.sync_tasks._revive); a report now does too.
"""

from __future__ import annotations

from app.db.models import OrderStatus
from tests.w7_report_rows import UNITS, api, buyer, line, order_of, report, upload  # noqa: F401 — fixtures


def test_a_corrected_report_puts_a_failed_order_back_to_placed(api, db, buyer):
    first = upload(
        api,
        report(
            line(unit_id=UNITS[0], status="Cancelled", quantity="2", total_order_amount="36414.00"),
            line(unit_id=UNITS[1], status="Shipped", quantity="2", cash_on_delivery="maybe"),
        ),
    ).json()
    assert [r["row"] for r in first["rejected"]] == [3]
    assert order_of(db).status == OrderStatus.failed

    corrected = upload(api, report(line(unit_id=UNITS[1], status="Shipped", quantity="2"))).json()

    assert corrected["created"] == 1
    order = order_of(db)
    assert order.status == OrderStatus.placed
    assert order.platform_status == "Shipped"  # the unit on its way stands for the order


def test_an_order_whose_units_all_still_read_cancelled_stays_failed(api, db, buyer):
    upload(api, report(line(unit_id=UNITS[0], status="Cancelled", quantity="2")))

    upload(api, report(line(unit_id=UNITS[1], status="Cancelled", quantity="2")))

    assert order_of(db).status == OrderStatus.failed
