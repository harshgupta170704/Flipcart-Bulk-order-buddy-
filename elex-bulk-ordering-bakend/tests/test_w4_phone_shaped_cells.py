"""A phone as a person types it reaches the order-units CSV exactly as typed
(spec SC-007, NFR-008; 27 Oct 2026).

Until now only an all-digit value in a phone or identifier column was written
in the ="..." form a spreadsheet keeps as text. Any other went through the
free-text guard and gained a leading space: "+919876543210" became
" +919876543210", which is no longer the value, and which a spreadsheet still
reads as the number 919876543210, dropping the "+" and showing it in
E-notation. A phone-shaped value (digits with a "+", spaces or dashes) is now
written the ="..." way too.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus
from app.io.orders_csv import identifier_cell

#: How people write an Indian mobile number.
PHONES = ["+919876543210", "98765 43210", "+91-98765-43210", "+91 98765 43210"]

_AS_TEXT = re.compile(r'^="(.*)"$')


def as_a_spreadsheet_shows(cell: str) -> str:
    """What a spreadsheet shows for a cell of the file: a ="..." cell is the
    text inside it; any other cell is what it holds, leading space and all."""
    match = _AS_TEXT.match(cell)
    return match.group(1) if match else cell


@pytest.mark.parametrize("phone", PHONES)
def test_a_phone_shaped_value_is_written_as_text_exactly_as_typed(phone):
    assert identifier_cell(phone) == f'="{phone}"'
    assert as_a_spreadsheet_shows(identifier_cell(phone)) == phone


def test_a_card_number_written_in_groups_is_kept_as_text_too():
    assert identifier_cell("4111 1111 1111 1111") == '="4111 1111 1111 1111"'
    assert identifier_cell("4111-1111-1111-1111") == '="4111-1111-1111-1111"'


def test_anything_else_is_guarded_as_before():
    # All digits: text, as it always was.
    assert identifier_cell("9876543210") == '="9876543210"'
    assert identifier_cell("0123") == '="0123"'
    # Not a phone: free text still never starts like a formula (audit F-36).
    assert identifier_cell('=HYPERLINK("http://x")') == ' =HYPERLINK("http://x")'
    assert identifier_cell("-123") == " -123"
    assert identifier_cell("+") == " +"
    assert identifier_cell("+91 call me") == " +91 call me"
    assert identifier_cell("n/a") == "n/a"
    assert identifier_cell(None) == ""


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_every_phone_and_identifier_column_of_the_export_keeps_the_value(api, db):
    account = Account(email="phones@test.com", phone="+91 90000 00001", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    for n, phone in enumerate(PHONES, start=1):
        db.add(
            Order(
                account_id=account.id,
                status=OrderStatus.placed,
                order_id=f"OD20000000000{n}",
                model="vivo T5 Lite",
                payment_mode="card",
                card_number="4111 1111 1111 1111",
                billing_phone=phone,
                delivery_otp="0482",
                placed_at=datetime(2026, 10, 26, 4, 30),
            )
        )
    db.commit()

    text = api.get("/orders/export.csv").text.lstrip("﻿")
    rows = {row["Order ID"]: row for row in csv.DictReader(io.StringIO(text))}

    assert len(rows) == len(PHONES)
    for n, phone in enumerate(PHONES, start=1):
        row = rows[f"OD20000000000{n}"]
        assert row["Billing Phone"] == f'="{phone}"'
        assert row["Card Number"] == '="4111 1111 1111 1111"'
        assert row["Delivery OTP"] == '="0482"'
        assert row["Phone Last 6"] == '="000001"'
        assert row["IMEI"] == ""  # not held yet; nothing to keep
        # Opened in a spreadsheet, each shows exactly what was stored.
        assert as_a_spreadsheet_shows(row["Billing Phone"]) == phone
        assert as_a_spreadsheet_shows(row["Card Number"]) == "4111 1111 1111 1111"
        # And no cell anywhere on the row is a phone pushed along by a space.
        assert not [cell for cell in row.values() if cell.startswith(" +")]
