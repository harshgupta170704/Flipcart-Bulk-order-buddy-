"""The order report and the id sheet after the checker of 27 Oct 2026, and
the last two review areas of that morning.

- A cancelled order a report filed is put back only by a unit on its way or
  delivered: "Refund Completed" on a cancelled card order is no revival.
- A unit never read back takes a corrected flat or phone for the same place,
  without an address change; a short first line, or a held address missing
  its pincode, is the same place as the row that completes it.
- A word typed for an unknown id ("NA") is no unit id.
- A unit id a spreadsheet rounded and showed whole ("…000000") or grouped
  ("439,123,…") is refused like its exponent form.
- A CSV whose rows end in a bare carriage return (Excel for Mac) is read.
- The id sheet is read and filed off the event loop.
"""

from __future__ import annotations

import asyncio

import pytest

from app.api import orders as orders_api
from app.core import unit_limits
from app.db.models import Account, Order, OrderStatus
from app.io import order_ids
from tests.w7_report_rows import ORDER, UNITS, api, buyer, line, order_of, report, units_of, upload  # noqa: F401 — fixtures

PLACE = dict(
    delivery_name="Asha Verma",
    delivery_line1="12 MG Road",
    delivery_line2="Flat 5",
    delivery_city="Lucknow",
    delivery_state="Uttar Pradesh",
    delivery_pincode="226001",
    delivery_phone="9876543210",
)


def _cancelled_cell(api) -> str:
    response = api.get("/orders/export.csv")
    assert response.status_code == 200
    header, first = response.text.lstrip("﻿").splitlines()[:2]
    import csv

    names = next(csv.reader([header]))
    return dict(zip(names, next(csv.reader([first]))))["Cancelled"]


# --- a refund is no revival -----------------------------------------------------------
def test_a_refunded_card_order_a_report_cancelled_stays_cancelled(api, db, buyer):
    assert upload(api, report(line(status="Cancelled"))).status_code == 200
    assert order_of(db).status == OrderStatus.failed

    out = upload(api, report(line(status="Refund Completed"))).json()

    assert out["rejected"] == []
    # The money came back: still cancelled, so unit limits still leave it out
    # (they count placed orders only) and the CSV still says so.
    assert order_of(db).status == OrderStatus.failed
    assert OrderStatus.failed not in unit_limits.COUNTED_STATUSES
    assert _cancelled_cell(api) == "Yes"


def test_a_unit_on_its_way_still_revives_the_order(api, db, buyer):
    upload(api, report(line(status="Cancelled")))
    assert order_of(db).status == OrderStatus.failed
    upload(api, report(line(status="Shipped")))
    assert order_of(db).status == OrderStatus.placed


# --- the same place, corrected or completed -----------------------------------------
def test_a_corrected_flat_or_phone_is_taken_without_an_address_change(api, db, buyer):
    upload(api, report(line(**PLACE)))
    upload(api, report(line(**{**PLACE, "delivery_line2": "Flat 6"})))
    (unit,) = units_of(db)
    assert "Flat 6" in unit.delivery_address and unit.address_changed_at is None

    upload(api, report(line(**{**PLACE, "delivery_line2": "Flat 6", "delivery_phone": "9123456789"})))
    (unit,) = units_of(db)
    assert "9123456789" in unit.delivery_address and unit.address_changed_at is None

    # A row that leaves the phone out keeps the held text whole.
    upload(api, report(line(**{**PLACE, "delivery_line2": "Flat 6", "delivery_phone": ""})))
    (unit,) = units_of(db)
    assert "9123456789" in unit.delivery_address and unit.address_changed_at is None


def test_a_short_first_line_left_whole_is_no_address_change(api, db, buyer):
    place = {**PLACE, "delivery_line1": "12", "delivery_line2": "MG Road"}
    upload(api, report(line(**place)))
    upload(api, report(line(**{**place, "delivery_phone": ""})))
    (unit,) = units_of(db)
    assert unit.address_changed_at is None and "9876543210" in unit.delivery_address


def test_a_held_address_without_its_pincode_is_completed_not_changed(api, db, buyer):
    upload(api, report(line(**PLACE)))
    (unit,) = units_of(db)
    # As the old reader stored a row whose pincode cell was empty.
    unit.delivery_address = "Asha Verma, 12 MG Road, Flat 5, Lucknow, Uttar Pradesh"
    unit.delivery_pincode = None
    db.commit()

    upload(api, report(line(**PLACE)))

    (unit,) = units_of(db)
    assert unit.delivery_pincode == "226001" and unit.address_changed_at is None


# --- what is no unit id ---------------------------------------------------------------
@pytest.mark.parametrize("cell", ["NA", "TBD", "PENDING"])
def test_a_word_for_an_unknown_id_is_no_unit_id(api, db, buyer, cell):
    out = upload(api, report(line(unit_id=cell))).json()
    (refusal,) = out["rejected"]
    assert refusal["row"] == 2 and "is not a unit id" in refusal["reason"]
    sheet = order_ids.read_id_sheet(f"Email,Order ID,Unit ID\nbuyer1@example.com,OD1111111111,{cell}\n".encode(), "ids.csv")
    assert sheet.rows == [] and "is not a unit id" in sheet.rejected[0].reason


@pytest.mark.parametrize(
    "cell",
    ["439123456789012000000", '"439,123,456,789,012,000,000"', "439123456789012000000.00", '"4,39,12,34,56,78,90,12,000"'],
)
def test_a_unit_id_a_spreadsheet_rounded_is_refused_by_row(api, db, buyer, cell):
    text = "\n".join(
        [report(line(order_id="OD338967210239333102", unit_id=UNITS[1])).splitlines()[0]]
        + [line(unit_id="PLACEHOLDER").replace("PLACEHOLDER", cell)]
    )
    out = upload(api, text).json()
    (refusal,) = out["rejected"]
    assert refusal["row"] == 2 and "saved as a number" in refusal["reason"]

    sheet = order_ids.read_id_sheet(f"Email,Order ID,Unit ID\nbuyer1@example.com,OD1111111111,{cell}\n".encode(), "ids.csv")
    assert sheet.rows == [] and "saved as a number" in sheet.rejected[0].reason


def test_a_real_unit_id_still_goes_in(api, db, buyer):
    out = upload(api, report(line(unit_id="439123456789012345601"))).json()
    assert (out["created"], out["rejected"]) == (1, [])


# --- Excel for Mac's CSV ----------------------------------------------------------------
def test_a_csv_with_bare_carriage_returns_is_read(api, db, buyer):
    text = report(line()).replace("\n", "\r") + "\r"
    out = upload(api, text).json()
    assert (out["created"], out["rejected"]) == (1, [])

    sheet = "Email,Order ID\rbuyer1@example.com,OD1111111111\r"
    response = upload(api, sheet, "/orders/import-ids")
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1


# --- the id sheet off the event loop ----------------------------------------------------
def test_the_id_sheet_is_read_and_filed_off_the_event_loop(api, db, buyer, monkeypatch):
    seen: list[str] = []
    real_read, real_import = order_ids.read_id_sheet, order_ids.import_sheet

    def where() -> str:
        try:
            asyncio.get_running_loop()
            return "loop"
        except RuntimeError:
            return "thread"

    def read(data, name):
        seen.append(where())
        return real_read(data, name)

    def file(db_session, sheet):
        seen.append(where())
        return real_import(db_session, sheet)

    monkeypatch.setattr(orders_api.id_sheets, "read_id_sheet", read)
    monkeypatch.setattr(orders_api.id_sheets, "import_sheet", file)
    response = upload(api, "Email,Order ID\nbuyer1@example.com,OD1111111111\n", "/orders/import-ids")
    assert response.status_code == 200, response.text
    assert seen == ["thread", "thread"]
    assert db.query(Order).count() == 1 and db.query(Account).count() == 1
