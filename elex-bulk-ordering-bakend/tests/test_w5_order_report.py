"""The order report upload (spec C-1, FR-021, FR-022, FR-024, FR-060; the
project's format, docs/specs/order-unit-ledger/order-report-format.md).

A report adds units, a later one updates them, a unit id names one unit,
unreadable rows are refused one by one while the rest go in — and what is
already held is kept safe: the read-back wins over a report for any unit it
has seen, an empty cell blanks nothing, card columns are never read.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus, OrderUnit
from app.io import order_report

from tests import layout

TEMPLATE = layout.DOCS / "specs" / "order-unit-ledger" / "order-report-template.csv"
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


def _row(**over) -> str:
    """One report line in the template's columns; give only what a test is about."""
    values = {name: "" for name in order_report.TEMPLATE_HEADERS}
    values.update(
        platform="flipkart",
        order_id="OD338967210239333101",
        unit_id="439123456789012345601",
        order_date="24-10-2026",
        order_time="10:10:15",
        platform_email="buyer1@example.com",
        status="Shipped",
        product="vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)",
        quantity="1",
        total_order_amount="18207.00",
    )
    values.update({key: str(value) for key, value in over.items()})
    return ",".join(f'"{values[name]}"' if "," in values[name] else values[name] for name in order_report.TEMPLATE_HEADERS)


def _upload(api, text: str, name: str = "report.csv"):
    return api.post("/orders/import-report", files={"file": (name, text.encode("utf-8"), "text/csv")})


def _units(db, order_id: str) -> list[OrderUnit]:
    db.expire_all()
    order = db.query(Order).filter(Order.order_id == order_id).one()
    return sorted(order.units, key=lambda unit: unit.position)


def test_the_templates_flipkart_row_goes_in_and_its_elex_row_is_refused(api, db, buyer):
    response = _upload(api, TEMPLATE.read_text(encoding="utf-8"))
    assert response.status_code == 200, response.text
    out = response.json()
    assert (out["created"], out["updated"], out["unchanged"]) == (1, 0, 0)
    assert [(r["row"], r["reason"]) for r in out["rejected"]] == [
        (3, "platform 'elex' is not supported yet: Flipkart only for now")
    ]
    assert {"imei", "billing_name"} <= set(out["not_kept"])

    db.expire_all()
    order = db.query(Order).filter(Order.order_id == "OD338967210239333100").one()
    (unit,) = order.units
    assert unit.unit_id == "439123456789012345678"
    assert unit.platform_status == "Cancelled - Order confirmed but not shipped"
    assert unit.promised_date == datetime(2026, 10, 25)
    assert unit.seller == "Example Retail Pvt Ltd"
    assert unit.delivery_pincode == "226013" and "Garden Bay" in unit.delivery_address
    assert unit.source == "upload" and unit.synced_at is None
    assert (order.model, order.qty, order.amount, order.payment_mode) == (
        "vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)", 1, 18207.0, "cod"
    )
    assert (order.gst_name, order.gstin) == ("Example Trading Pvt Ltd", "09AAAAA0000A1Z5")
    assert order.placed_at == datetime(2026, 10, 24, 4, 40, 15)  # 10:10:15 IST
    # Its one unit reads cancelled: the order Flipkart took is failed, as the read-back would.
    assert order.status == OrderStatus.failed

    # The same file again adds nothing.
    again = _upload(api, TEMPLATE.read_text(encoding="utf-8")).json()
    assert (again["created"], again["updated"], again["unchanged"]) == (0, 0, 1)
    db.expire_all()
    assert db.query(Order).count() == 1 and db.query(OrderUnit).count() == 1


def test_a_three_unit_order_makes_three_units_in_their_order(api, db, buyer):
    lines = [HEADER] + [
        _row(unit_id=f"43912345678901234561{n}", quantity="3", total_order_amount="54621.00") for n in range(3)
    ]
    out = _upload(api, "\n".join(lines)).json()
    assert out["created"] == 3 and out["rejected"] == []
    units = _units(db, "OD338967210239333101")
    assert [(u.position, u.unit_id) for u in units] == [(n + 1, f"43912345678901234561{n}") for n in range(3)]


def test_a_later_report_moves_a_unit_never_read_back_but_only_fills_one_the_read_back_saw(api, db, buyer):
    _upload(api, "\n".join([HEADER, _row(), _row(order_id="OD338967210239333102", unit_id="439123456789012345602")]))
    (seen,) = _units(db, "OD338967210239333102")
    seen.synced_at = datetime(2026, 10, 26, 6, 0)  # Flipkart's page was read since
    seen.platform_status = "Delivery expected by Nov 01"
    db.commit()

    later = "\n".join(
        [
            HEADER,
            _row(status="Delivered", actual_delivery_date="27-10-2026", tracking_id="TRK0001"),
            _row(
                order_id="OD338967210239333102", unit_id="439123456789012345602",
                status="Delivered", actual_delivery_date="27-10-2026", tracking_id="TRK0002",
            ),
        ]
    )
    out = _upload(api, later).json()
    assert out["updated"] == 2
    # The second row's status was not taken, and the answer says so.
    assert out["kept_platform"] == 1

    (moved,) = _units(db, "OD338967210239333101")
    assert (moved.platform_status, moved.delivered_date, moved.tracking_id) == ("Delivered", datetime(2026, 10, 27), "TRK0001")
    (kept,) = _units(db, "OD338967210239333102")
    # Flipkart's own reading stands; only what it left empty is filled.
    assert kept.platform_status == "Delivery expected by Nov 01"
    assert (kept.delivered_date, kept.tracking_id) == (datetime(2026, 10, 27), "TRK0002")


def test_an_empty_cell_blanks_nothing_and_a_new_address_is_a_change(api, db, buyer):
    _upload(api, "\n".join([HEADER, _row(seller_name="Retailer A", delivery_line1="Tower 5", delivery_pincode="226013")]))
    _upload(api, "\n".join([HEADER, _row(delivery_line1="Tower 9", delivery_pincode="226010")]))
    (unit,) = _units(db, "OD338967210239333101")
    assert unit.seller == "Retailer A"  # the second report said nothing of the seller
    assert unit.delivery_address.startswith("Tower 9") and unit.delivery_pincode == "226010"
    assert unit.address_changed_at is not None  # the address-change alert's cue


def test_rows_that_cannot_be_trusted_are_refused_one_by_one(api, db, buyer):
    other = Account(email="other@example.com", status=AccountStatus.session_valid)
    db.add(other)
    db.add(Order(account_id=buyer.id, status=OrderStatus.placed, order_id="OD338967210239333199"))
    db.commit()
    held = db.query(Order).filter(Order.order_id == "OD338967210239333199").one()
    held.units[0].unit_id = "439123456789012345699"
    db.commit()

    lines = [
        HEADER,
        _row(unit_id="4.39123E+20"),  # a spreadsheet rounded it
        _row(order_id="OD338967210239333103", unit_id="439123456789012345699"),  # another order's unit
        _row(order_id="OD338967210239333104", unit_id="439123456789012345604", platform_email="nobody@example.com"),
        _row(order_id="OD338967210239333199", unit_id="439123456789012345605", platform_email="other@example.com"),
        _row(order_id="12345", unit_id="439123456789012345606"),
        _row(unit_id="439123456789012345607", status=""),
        _row(unit_id="439123456789012345608", cash_on_delivery="maybe"),
        _row(order_id="OD338967210239333105", unit_id="439123456789012345609"),  # fine
    ]
    out = _upload(api, "\n".join(lines)).json()
    reasons = {r["row"]: r["reason"] for r in out["rejected"]}
    assert out["created"] == 1
    assert "saved as a number" in reasons[2]
    assert reasons[3] == "unit id 439123456789012345699 belongs to OD338967210239333199, not OD338967210239333103"
    assert reasons[4] == "no account with the email nobody@example.com"
    assert reasons[5] == "OD338967210239333199 is in the ledger under another account than other@example.com"
    assert "not a Flipkart order id" in reasons[6]
    assert reasons[7] == "status is empty"
    assert reasons[8] == "cash_on_delivery must be Yes or No"
    assert sorted(reasons) == [2, 3, 4, 5, 6, 7, 8]


def test_card_columns_are_never_read_stored_or_echoed(api, db, buyer, caplog):
    card = "4111111111111111"
    text = HEADER + ",Card Number,CVV\n" + _row(card_masked=card) + f",{card},123\n"
    with caplog.at_level(logging.DEBUG):
        response = _upload(api, text)
    assert response.status_code == 200
    assert card not in response.text
    assert not [r for r in caplog.records if card in r.getMessage()]
    db.expire_all()
    order = db.query(Order).filter(Order.order_id == "OD338967210239333101").one()
    assert order.card_number is None


def test_what_is_not_a_report_is_refused_plainly(api, buyer):
    assert _upload(api, "x", name="report.pdf").status_code == 400
    missing = _upload(api, "platform,order_id\nflipkart,OD338967210239333101\n")
    assert missing.status_code == 422
    assert missing.json()["detail"].startswith("the header row needs the columns")
    # Not a workbook at all: one fixed sentence, never the library's own words.
    broken = api.post("/orders/import-report", files={"file": ("r.xlsx", b"PK\x03\x04 not a workbook", "application/octet-stream")})
    assert broken.status_code == 422
    assert broken.json()["detail"] == orders_api.UNREADABLE_REPORT


def test_an_xlsx_report_with_a_numeric_unit_id_cell_is_refused_by_row(api, db, buyer):
    wb = Workbook()
    sheet = wb.active
    sheet.append(list(order_report.TEMPLATE_HEADERS))
    good = [""] * len(order_report.TEMPLATE_HEADERS)
    names = list(order_report.TEMPLATE_HEADERS)
    for key, value in dict(platform="flipkart", order_id="OD338967210239333106", unit_id="439123456789012345610",
                           order_date="24-10-2026", status="Shipped", product="vivo", platform_email="buyer1@example.com").items():
        good[names.index(key)] = value
    rounded = list(good)
    rounded[names.index("unit_id")] = 439123456789012345611  # a number cell: 15 digits survive
    rounded[names.index("order_id")] = "OD338967210239333107"
    sheet.append(good)
    sheet.append(rounded)
    buffer = io.BytesIO()
    wb.save(buffer)
    out = api.post("/orders/import-report", files={"file": ("r.xlsx", buffer.getvalue(), "application/octet-stream")}).json()
    assert out["created"] == 1
    assert [r["row"] for r in out["rejected"]] == [3]


def test_the_template_can_be_downloaded(api):
    response = api.get("/orders/report-template.csv")
    assert response.status_code == 200
    assert response.text.lstrip("﻿").strip() == HEADER


def test_a_new_order_waits_while_the_account_has_an_unconfirmed_one(api, db, buyer):
    """A run's order Flipkart never confirmed may be the very order a report
    names: it is settled first, not doubled (as the order-id sheet asks)."""
    db.add(Order(account_id=buyer.id, status=OrderStatus.unconfirmed, order_id=None, model="vivo"))
    db.commit()
    out = _upload(api, "\n".join([HEADER, _row()])).json()
    assert out["created"] == 0
    (refusal,) = out["rejected"]
    assert "unconfirmed order awaiting settling" in refusal["reason"]
    db.expire_all()
    assert db.query(Order).filter(Order.order_id == "OD338967210239333101").count() == 0


def test_the_answer_counts_only_rows_that_flipkarts_page_contradicts(api, db, buyer):
    _upload(api, "\n".join([HEADER, _row(seller_name="Example Retail Pvt Ltd")]))
    (seen,) = _units(db, "OD338967210239333101")
    seen.synced_at = datetime(2026, 10, 26, 6, 0)
    seen.platform_status = "Delivered"
    seen.delivered_date = datetime(2026, 10, 27)
    db.commit()

    # The same reading in other spacing and case, the same day: nothing to say.
    same = _row(status="  delivered ", actual_delivery_date="27-10-2026", seller_name="EXAMPLE RETAIL PVT LTD")
    out = _upload(api, "\n".join([HEADER, same])).json()
    assert out["kept_platform"] == 0
    # Nor when the page words the same state its own way.
    seen = _units(db, "OD338967210239333101")[0]
    seen.platform_status = "Delivered on Oct 27"
    db.commit()
    out = _upload(api, "\n".join([HEADER, same])).json()
    assert out["kept_platform"] == 0

    # Another delivery day than the page's: Flipkart's stands, and is counted.
    other = _row(status="Delivered", actual_delivery_date="28-10-2026")
    out = _upload(api, "\n".join([HEADER, other])).json()
    assert out["kept_platform"] == 1
    (unit,) = _units(db, "OD338967210239333101")
    assert unit.delivered_date == datetime(2026, 10, 27)

    # A unit never read back takes the report's word: nothing was kept over it.
    fresh = _row(order_id="OD338967210239333109", unit_id="439123456789012345609")
    _upload(api, "\n".join([HEADER, fresh]))
    out = _upload(api, "\n".join([HEADER, _row(order_id="OD338967210239333109", unit_id="439123456789012345609", status="Delivered")])).json()
    assert (out["updated"], out["kept_platform"]) == (1, 0)
