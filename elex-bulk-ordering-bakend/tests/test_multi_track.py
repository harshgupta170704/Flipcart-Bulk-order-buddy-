"""E-Kart's multi_track sheet comes back as the same file with the ledger's
account, delivery OTP, phone's last six and Flipkart status added (26 Nov 2026)."""

import csv
import io
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus

E_KART_HEADER = ["MERCHANT REFERENCE ID", "TRACKING ID", "MERCHANT", "VALUE", "PINCODE", "ORDER ID", "ORDER ITEM SEQ ID"]


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def seeded(db):
    account = Account(email="track@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    order = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        model="vivo T5 Lite",
        order_id="OD439237155916543100",
        amount=15832.0,
        target=15832.0,
        placed_at=datetime(2026, 11, 24, 11, 0),
    )
    db.add(order)
    db.commit()
    unit = order.units[0]
    unit.tracking_id = "FMPP4487026473"
    unit.delivery_otp = "081571"
    unit.platform_status = "Arriving"
    unit.delivery_address = "Ravi, Civil Lines, Hardoi, Uttar Pradesh - 241001, 9876034687"
    # Second order, one unit, no tracking ID yet: found by its order ID.
    other = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        model="Ai+ Nova 2",
        order_id="OD439240224205404100",
        amount=11662.0,
        target=11662.0,
    )
    db.add(other)
    db.commit()
    other.units[0].delivery_otp = "478938"
    db.commit()
    return {"account": account, "order": order, "other": other}


def _workbook(rows: list[list]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.append([])  # E-Kart leaves the first row blank
    sheet.append(E_KART_HEADER)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


ROWS = [
    ["S14941323642", "FMPP4487026473", "FKMP", 15832, 241001, "OD439237155916543100 ", "439237155916543100 "],
    ["S18800539655", "--", "FKMP", 11662, 241001, "OD439240224205404100 ", "439240224205404100 "],
    ["S00000000001", "FMPP0000000001", "FKMP", 999, 110001, "OD100000000000000000", "1"],
]


def test_the_workbook_comes_back_as_it_was_with_four_columns_added(api, seeded):
    name = "multi_track2026-11-26 12_07_10 +0530.xlsx"
    response = api.post("/orders/multi-track", files={"file": (name, _workbook(ROWS))})
    assert response.status_code == 200, response.text
    assert response.headers["x-rows"] == "3"
    assert response.headers["x-matched"] == "2"
    assert "with%20OTP%20and%20phone.xlsx" in response.headers["content-disposition"]

    sheet = load_workbook(io.BytesIO(response.content)).active
    rows = [list(r) for r in sheet.iter_rows(values_only=True)]
    assert all(v is None for v in rows[0])  # the blank first row stays
    assert rows[1] == [*E_KART_HEADER, "ACCOUNT", "DELIVERY OTP", "LAST 6 DIGITS OF PHONE", "FLIPKART STATUS"]
    # The sheet's own cells untouched.
    assert rows[2][:7] == ROWS[0]
    # By tracking ID: OTP and last six as text, the leading zero kept.
    assert rows[2][7:] == ["track@test.com", "081571", "034687", "Arriving"]
    # No tracking ID ("--"): by its one-unit order's ID.
    assert rows[3][7] == "track@test.com"
    assert rows[3][8] == "478938"
    # Not in the ledger: left blank, the row itself kept.
    assert rows[4][:7] == ROWS[2]
    assert rows[4][7:] == [None, None, None, None]


def test_a_csv_comes_back_as_a_csv(api, seeded):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(E_KART_HEADER)
    writer.writerow(ROWS[0])
    response = api.post("/orders/multi-track", files={"file": ("multi_track.csv", out.getvalue().encode())})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.text.lstrip("﻿"))))
    assert rows[0][-4:] == ["ACCOUNT", "DELIVERY OTP", "LAST 6 DIGITS OF PHONE", "FLIPKART STATUS"]
    assert rows[1][-4:] == ["track@test.com", '="081571"', '="034687"', "Arriving"]


def test_a_sheet_without_a_tracking_id_column_is_refused_in_plain_words(api, seeded):
    response = api.post("/orders/multi-track", files={"file": ("other.csv", b"a,b\n1,2\n")})
    assert response.status_code == 422
    assert "TRACKING ID" in response.json()["detail"]


def test_a_file_that_is_not_a_sheet_is_refused(api, seeded):
    response = api.post("/orders/multi-track", files={"file": ("notes.txt", b"hello")})
    assert response.status_code == 400
