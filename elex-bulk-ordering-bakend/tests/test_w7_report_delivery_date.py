"""The unit's delivery date, from a report (review of 27 Oct 2026).

``OrderUnit.delivery_date`` is what the ledger's Delivery date column, its
date filter and the "Product invoice missing" clock read; the read-back always
sets it (the delivered day, else the promise), a report never did — so a unit
a report said was delivered was in no delivery-date range at all.
"""

from __future__ import annotations

from datetime import datetime

from tests.w7_report_rows import UNITS, api, buyer, line, report, units_of, upload  # noqa: F401 — fixtures

DELIVERED_ORDER = "OD338967210239333101"
SHIPPED_ORDER = "OD338967210239333102"
CANCELLED_ORDER = "OD338967210239333103"


def _upload_three(api) -> dict:
    return upload(
        api,
        report(
            line(order_id=DELIVERED_ORDER, unit_id=UNITS[0], status="Delivered",
                 estimated_delivery="26-10-2026", actual_delivery_date="27-10-2026"),
            line(order_id=SHIPPED_ORDER, unit_id=UNITS[1], status="Shipped", estimated_delivery="30-10-2026"),
            line(order_id=CANCELLED_ORDER, unit_id=UNITS[2], status="Cancelled", estimated_delivery="29-10-2026"),
        ),
    ).json()


def test_a_delivered_unit_is_found_by_its_delivery_day(api, db, buyer):
    assert _upload_three(api)["created"] == 3

    (delivered,) = units_of(db, DELIVERED_ORDER)
    assert delivered.delivery_date == datetime(2026, 10, 27)
    found = api.get("/orders", params={"date_field": "delivery_date", "date_from": "2026-10-27", "date_to": "2026-10-27"})
    assert found.status_code == 200
    assert [item["unit_id"] for item in found.json()["items"]] == [UNITS[0]]


def test_a_unit_on_its_way_is_dated_by_its_promise_and_a_cancelled_one_not_at_all(api, db, buyer):
    _upload_three(api)

    (shipped,) = units_of(db, SHIPPED_ORDER)
    assert shipped.delivery_date == shipped.promised_date == datetime(2026, 10, 30)
    (cancelled,) = units_of(db, CANCELLED_ORDER)
    assert cancelled.delivery_date is None


def test_a_later_report_moves_the_day_with_the_unit(api, db, buyer):
    _upload_three(api)

    later = upload(
        api,
        report(
            line(order_id=SHIPPED_ORDER, unit_id=UNITS[1], status="Delivered",
                 estimated_delivery="30-10-2026", actual_delivery_date="29-10-2026"),
        ),
    ).json()

    assert later["updated"] == 1
    (shipped,) = units_of(db, SHIPPED_ORDER)
    assert shipped.delivery_date == datetime(2026, 10, 29)


def test_a_day_the_read_back_set_stands(api, db, buyer):
    upload(api, report(line(unit_id=UNITS[0], status="Shipped")))
    (unit,) = units_of(db)
    unit.synced_at = datetime(2026, 10, 26, 6, 0)
    unit.delivery_date = datetime(2026, 10, 31)
    db.commit()

    upload(api, report(line(unit_id=UNITS[0], status="Shipped", estimated_delivery="30-10-2026")))

    (unit,) = units_of(db)
    assert unit.delivery_date == datetime(2026, 10, 31)
    # Its empty promise was filled, as any empty field is.
    assert unit.promised_date == datetime(2026, 10, 30)
