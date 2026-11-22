"""An order filed from a report whose quantity cell is blank (review of 27 Oct
2026).

It was filed with qty 1 however many unit rows it came with: the page and the
CSV showed the whole total as each unit's price beside a quantity of 1, the
Order Details answer said 2 units, and unit limits — a sum of ``qty`` —
counted one unit for two. And a row's unit price was read and never used.
"""

from __future__ import annotations

import csv
import io

from app.core import unit_limits
from app.db.models import LimitScope, UnitLimit
from tests.w7_report_rows import UNITS, api, buyer, line, order_of, report, units_of, upload  # noqa: F401 — fixtures


def _csv_rows(api) -> list[dict[str, str]]:
    response = api.get("/orders/export.csv")
    assert response.status_code == 200
    return list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))


def test_two_rows_with_the_quantity_blank_make_an_order_of_two_units(api, db, buyer):
    text = report(
        line(unit_id=UNITS[0], quantity="", total_order_amount="36414.00"),
        line(unit_id=UNITS[1], quantity="", total_order_amount="36414.00"),
    )

    out = upload(api, text).json()

    assert (out["created"], out["rejected"]) == (2, [])
    order = order_of(db)
    assert (order.qty, len(order.units), order.amount) == (2, 2, 36414.0)
    # Each unit's price, on the page and in the CSV: half the total, not all of it.
    page = api.get("/orders").json()
    assert sorted((item["unit_id"], item["unit_amount"], item["qty"]) for item in page["items"]) == [
        (UNITS[0], 18207.0, 2),
        (UNITS[1], 18207.0, 2),
    ]
    assert sorted((row["Unit ID"], row["Unit Amount"], row["Quantity"]) for row in _csv_rows(api)) == [
        (f'="{UNITS[0]}"', "18207.00", "2"),
        (f'="{UNITS[1]}"', "18207.00", "2"),
    ]
    # Unit limits count what the account has bought: two units.
    every_model = UnitLimit(scope=LimitScope.model, value="", max_units=20, window_days=36500)
    assert unit_limits.units_used(db, buyer.id, every_model, unit_limits.ProductFacts()) == 2


def test_a_quantity_any_row_states_makes_the_units_at_once(api, db, buyer):
    """The first row says nothing, the second says 3: the order is one of
    three, the unit not listed yet a placeholder, as from a first row
    saying 3."""
    text = report(
        line(unit_id=UNITS[0], quantity="", total_order_amount=""),
        line(unit_id=UNITS[1], quantity="3", total_order_amount="54621.00"),
    )

    upload(api, text)

    order = order_of(db)
    assert (order.qty, order.amount) == (3, 54621.0)
    assert [unit.unit_id for unit in units_of(db)] == [UNITS[0], UNITS[1], None]


def test_a_unit_price_alone_prices_the_order(api, db, buyer):
    text = report(
        line(unit_id=UNITS[0], quantity="", total_order_amount="", unit_amount="18207.00"),
        line(unit_id=UNITS[1], quantity="", total_order_amount="", unit_amount="18207.00"),
    )

    upload(api, text)

    order = order_of(db)
    assert (order.qty, order.amount) == (2, 36414.0)


def test_a_later_report_brings_an_order_never_read_back_up_to_its_units(api, db, buyer):
    upload(api, report(line(unit_id=UNITS[0], quantity="", total_order_amount="36414.00")))
    assert order_of(db).qty == 1

    upload(api, report(line(unit_id=UNITS[1], quantity="", total_order_amount="36414.00")))

    assert (order_of(db).qty, len(units_of(db))) == (2, 2)


def test_a_stated_quantity_is_never_lowered_to_the_units_listed(api, db, buyer):
    upload(api, report(line(unit_id=UNITS[0], quantity="3", total_order_amount="54621.00")))

    order = order_of(db)
    assert order.qty == 3
    assert [unit.unit_id for unit in units_of(db)] == [UNITS[0], None, None]
