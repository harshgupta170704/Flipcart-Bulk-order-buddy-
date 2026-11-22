"""The ledger's own CSV export handed back in (review of 27 Oct 2026).

The export writes every long id, phone and IMEI in a spreadsheet's text form
``="…"`` so Excel keeps every digit (app.io.orders_csv), and its headers fit
both uploads. Read back as they were, those cells became unit ids no unit has:
the report gave the order a second, phantom unit for each real one, and the id
sheet stored ``="439…"`` as a new order's unit id. And a unit id typed in lower
case, which the ledger stores upper-cased, failed the whole report on the
database's unique key the second time it came in.
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Alert, Order, OrderUnit
from app.io import order_ids, order_report
from app.io.orders_csv import identifier_cell, text_guard

ORDER = "OD338967210239333101"
UNITS = ("439123456789012345601", "439123456789012345602")
HEADER = ",".join(order_report.TEMPLATE_HEADERS)


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("ops@test.com")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def buyer(db) -> Account:
    account = Account(email="buyer1@example.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    return account


def _csv_line(cells: list[str]) -> str:
    """One CSV line, quoted the way a spreadsheet writes it."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(cells)
    return buffer.getvalue()


def _line(**over) -> str:
    """One report line in the template's columns; give only what a test is about."""
    values = {name: "" for name in order_report.TEMPLATE_HEADERS}
    values.update(
        platform="flipkart",
        order_id=ORDER,
        unit_id=UNITS[0],
        order_date="24-10-2026",
        order_time="10:10:15",
        platform_email="buyer1@example.com",
        status="Shipped",
        product="vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)",
        quantity="1",
        total_order_amount="18207.00",
    )
    values.update({key: str(value) for key, value in over.items()})
    return _csv_line([values[name] for name in order_report.TEMPLATE_HEADERS])


def _upload(api, text: str, path: str = "/orders/import-report"):
    return api.post(path, files={"file": ("sheet.csv", text.encode("utf-8"), "text/csv")})


def _seed(api) -> None:
    """A two-unit order with a billing phone, as a report brings it in."""
    lines = [HEADER] + [
        _line(unit_id=unit, quantity="2", total_order_amount="36414.00", billing_phone="9876543210") for unit in UNITS
    ]
    out = _upload(api, "\n".join(lines)).json()
    assert (out["created"], out["rejected"]) == (2, [])


def _export(api) -> str:
    response = api.get("/orders/export.csv")
    assert response.status_code == 200
    return response.text.lstrip("﻿")


def _forget_orders(db) -> None:
    """An empty ledger, as on another machine the file is carried to (the
    alerts the first upload raised go too: they name the orders)."""
    db.query(Alert).delete()
    for order in db.query(Order).all():
        db.delete(order)
    db.commit()


def _stored(db) -> list[tuple[str | None, str | None]]:
    db.expire_all()
    return sorted((unit.unit_id, unit.order.billing_phone) for unit in db.query(OrderUnit).all())


def test_the_export_writes_ids_and_phones_in_the_text_form():
    """What the round trip is about: the cells the readers must unwrap."""
    assert text_guard(UNITS[0]) == f'="{UNITS[0]}"'
    assert identifier_cell("9876543210") == '="9876543210"'


def test_the_export_handed_back_as_a_report_adds_no_unit(api, db, buyer):
    _seed(api)
    exported = _export(api)
    assert f'="{UNITS[0]}"' in next(csv.reader(io.StringIO(exported.splitlines()[1])))

    response = _upload(api, exported)

    assert response.status_code == 200, response.text
    out = response.json()
    assert (out["created"], out["updated"], out["unchanged"], out["rejected"]) == (0, 0, 2, [])
    db.expire_all()
    assert db.query(OrderUnit).count() == 2  # no phantom unit beside each real one
    assert [unit for unit, _phone in _stored(db)] == list(UNITS)


def test_the_export_carried_to_an_empty_ledger_files_plain_ids_and_phone(api, db, buyer):
    _seed(api)
    exported = _export(api)
    _forget_orders(db)

    out = _upload(api, exported).json()

    assert (out["created"], out["rejected"]) == (2, [])
    assert _stored(db) == [(UNITS[0], "9876543210"), (UNITS[1], "9876543210")]
    order = db.query(Order).filter(Order.order_id == ORDER).one()
    assert order.qty == 2 and len(order.units) == 2


def test_the_export_handed_back_as_an_id_sheet_adds_no_unit(api, db, buyer):
    _seed(api)
    exported = _export(api)

    response = _upload(api, exported, "/orders/import-ids")

    assert response.status_code == 200, response.text
    assert response.json()["created"] == 0
    db.expire_all()
    assert db.query(OrderUnit).count() == 2
    assert [unit for unit, _phone in _stored(db)] == list(UNITS)


def test_the_export_carried_to_an_empty_ledger_as_an_id_sheet_files_the_plain_unit_id(api, db, buyer):
    _seed(api)
    exported = _export(api)
    _forget_orders(db)

    out = _upload(api, exported, "/orders/import-ids").json()

    assert out["created"] == 1
    db.expire_all()
    (order,) = db.query(Order).all()
    assert order.units[0].unit_id == UNITS[0]


def test_the_readers_unwrap_the_text_form_before_their_checks():
    sheet = order_ids.read_id_sheet(
        (
            "Email,Order ID,Unit ID\n"
            + _csv_line(["a@x.com", "OD1111111111", '="439123456789012345678"']) + "\n"
            + _csv_line(["a@x.com", "OD2222222222", '="4.39123E+20"']) + "\n"
            + _csv_line(["a@x.com", '="od3333333333"', ""]) + "\n"
        ).encode("utf-8"),
        "ids.csv",
    )
    assert [(r.row_number, r.order_id, r.unit_id) for r in sheet.rows] == [
        (2, "OD1111111111", "439123456789012345678"),
        (4, "OD3333333333", None),
    ]
    # The mangled-id test reads the value inside the quotes.
    (refused,) = sheet.rejected
    assert refused.row_number == 3 and "saved as a number" in refused.reason

    text = "\n".join(
        [HEADER, _line(unit_id=f'="{UNITS[0]}"', billing_phone='="+91 98765 43210"', total_order_amount='="18207.00"')]
    )
    (row,) = order_report.read_report(text.encode("utf-8"), "report.csv").rows
    assert (row.unit_id, row.billing_phone, row.total) == (UNITS[0], "+91 98765 43210", 18207.0)


@pytest.mark.parametrize("cell", ["=SUM(A1:A2)", "439 123", "U-1", '"439"', '="439"&"1"'])
def test_a_unit_id_that_is_not_a_run_of_digits_and_letters_is_refused_by_row(cell):
    text = "\n".join([HEADER, _line(unit_id=cell), _line(order_id="OD338967210239333102", unit_id=UNITS[1])])
    report = order_report.read_report(text.encode("utf-8"), "report.csv")
    assert [row.unit_id for row in report.rows] == [UNITS[1]]
    (refusal,) = report.rejected
    # Digits grouped by a space read as a number a spreadsheet showed, and are
    # refused as such; either way the row is refused by name.
    assert refusal.row_number == 2 and ("is not a unit id" in refusal.reason or "saved as a number" in refusal.reason)

    sheet = order_ids.read_id_sheet(
        ("Email,Order ID,Unit ID\n" + _csv_line(["a@x.com", "OD1111111111", cell]) + "\n").encode("utf-8"),
        "ids.csv",
    )
    assert sheet.rows == []
    (refused,) = sheet.rejected
    assert refused.row_number == 2 and ("is not a unit id" in refused.reason or "saved as a number" in refused.reason)


def test_a_unit_id_typed_in_lower_case_names_the_same_unit_every_time(api, db, buyer):
    text = "\n".join([HEADER, _line(unit_id="u1")])

    first = _upload(api, text)
    again = _upload(api, text)

    assert (first.status_code, again.status_code) == (200, 200), again.text
    assert first.json()["created"] == 1
    assert (again.json()["created"], again.json()["unchanged"]) == (0, 1)
    db.expire_all()
    assert [unit.unit_id for unit in db.query(OrderUnit).all()] == ["U1"]
