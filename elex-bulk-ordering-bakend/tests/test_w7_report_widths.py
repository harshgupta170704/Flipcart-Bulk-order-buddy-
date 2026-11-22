"""One over-long cell in an order report (review of 27 Oct 2026).

A status, billing phone or billing pincode was read up to 255 characters and
the delivery pincode at any length, into columns 64, 20, 16 and 8 wide.
SQLite keeps a longer value; PostgreSQL refuses it, and with it the whole
report — a bare 500 and nothing imported. FR-060 has such a row refused by
name and the rest imported, on either database (run this file on both:
runpg.sh for PostgreSQL).
"""

from __future__ import annotations

import pytest

from app.db.models import Order, OrderUnit
from tests.w7_report_rows import UNITS, api, buyer, line, report, units_of, upload  # noqa: F401 — fixtures

STATUS = OrderUnit.__table__.c.platform_status.type.length
PHONE = Order.__table__.c.billing_phone.type.length
PIN = Order.__table__.c.pin.type.length


@pytest.mark.parametrize(
    "cells, reason",
    [
        ({"status": "S" * (STATUS + 1)}, f"status is longer than {STATUS} characters"),
        ({"billing_phone": "9" * (PHONE + 1)}, f"billing_phone is longer than {PHONE} characters"),
        ({"billing_pincode": "2" * (PIN + 1)}, f"billing_pincode is longer than {PIN} characters"),
        (
            {"delivery_line1": "Tower 5", "delivery_pincode": "2260131"},
            "delivery_pincode '2260131' is not a pincode: write its 6 digits, such as 226013",
        ),
        (
            {"delivery_line1": "Tower 5", "delivery_pincode": "226013 Lucknow"},
            "delivery_pincode '226013 Lucknow' is not a pincode: write its 6 digits, such as 226013",
        ),
    ],
)
def test_an_over_long_cell_refuses_its_row_by_name_and_the_rest_go_in(api, db, buyer, cells, reason):
    good = line(unit_id=UNITS[0])
    long = line(order_id="OD338967210239333102", unit_id=UNITS[1], **cells)

    response = upload(api, report(good, long))

    assert response.status_code == 200, response.text
    out = response.json()
    assert out["created"] == 1
    assert out["rejected"] == [{"row": 3, "reason": reason}]
    (unit,) = units_of(db)
    assert unit.unit_id == UNITS[0]
    db.expire_all()
    assert db.query(Order).filter(Order.order_id == "OD338967210239333102").count() == 0


def test_values_as_wide_as_their_columns_go_in(api, db, buyer):
    """The limit is the column's width, not one less."""
    text = report(
        line(
            status="S" * STATUS,
            billing_phone="9" * PHONE,
            billing_pincode="2" * PIN,
            delivery_line1="Tower 5",
            delivery_pincode="226013",
        )
    )

    out = upload(api, text).json()

    assert (out["created"], out["rejected"]) == (1, [])
    (unit,) = units_of(db)
    assert (unit.platform_status, unit.delivery_pincode) == ("S" * STATUS, "226013")
    assert (unit.order.billing_phone, unit.order.pin) == ("9" * PHONE, "2" * PIN)
