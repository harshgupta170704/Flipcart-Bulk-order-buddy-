"""No export carries a cell a spreadsheet would run as a formula (spec NFR-008,
audit F-36; review of 27 Oct 2026).

The ledger's CSV guarded its identifiers and a few free-text columns one by
one, and left the rest raw: Flipkart's status wording, the product, the
colour, the business name, the seller, the brand, the category, the link —
and so did the owner's two sheets, the invoice-data CSV and both workbooks.
A product or a business name starting with "=" or "@" then ran as a formula
in the accountant's spreadsheet. Every CSV cell now passes one last guard on
its way out, and the workbooks store such text as text.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Invoice, Order, OrderStatus
from app.io import excel, invoice_data_csv
from app.io.orders_csv import safe_cell, safe_row

#: What a person or a page could put in a free-text field.
EVIL = {
    "model": '=HYPERLINK("http://example.invalid","phone")',
    "color": "+cmd|' /C calc'!A0",
    "gst_name": "@SUM(1+1)*cmd|' /C calc'!A0",
    "brand": "=1+1",
    "category": "-2+3",
    "product_link": "=WEBSERVICE(\"http://example.invalid\")",
}
STATUS = "=IMPORTXML(\"http://example.invalid\",\"//a\")"
SELLER = "@seller"
#: Our own text form of an identifier: digits (with a "+", spaces or dashes) inside ="…".
OURS = re.compile(r'="[0-9 +\-]*"')


def runs_as_a_formula(cell: str) -> bool:
    """Whether a spreadsheet would run the cell: it starts with "=", "+", "-"
    or "@" and is neither our own ="digits" form nor a plain number."""
    if OURS.fullmatch(cell) or re.fullmatch(r"-?\d+(?:\.\d+)?", cell):
        return False
    return cell[:1] in ("=", "+", "-", "@", "\t", "\r")


def test_the_last_guard_passes_identifiers_and_numbers_and_defuses_the_rest():
    assert safe_cell('="439123456789012345678"') == '="439123456789012345678"'
    assert safe_cell('="+91 98765 43210"') == '="+91 98765 43210"'
    assert safe_cell("-1750.00") == "-1750.00"  # a credit note's amount stays a number
    assert safe_cell("17707.00") == "17707.00"
    assert safe_cell("24-10-2026") == "24-10-2026"
    # Free text shaped like our identifier form is not one: it carries a formula.
    assert safe_cell('="1"&HYPERLINK("http://x")') == ' ="1"&HYPERLINK("http://x")'
    assert safe_cell("=SUM(1)") == " =SUM(1)"
    assert safe_cell("+1 vivo") == " +1 vivo"
    assert safe_cell("-2+3") == " -2+3"
    assert safe_cell("@x") == " @x"
    assert safe_cell(" =already guarded") == " =already guarded"
    assert safe_cell(None) == ""
    assert safe_cell(12) == "12"
    assert safe_row(["a", "=b", None]) == ["a", " =b", ""]


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def order(db) -> Order:
    account = Account(email="guard@test.com", phone="9000000001", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    order = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        order_id="OD338975799606623101",
        payment_mode="card",
        card_number="4111111111111111",
        delivery_otp="0482",
        placed_at=datetime(2026, 10, 26, 4, 30),
        amount=18207.0,
        **EVIL,
    )
    db.add(order)
    db.commit()
    unit = order.units[0]
    unit.unit_id = "439123456789012345678"
    unit.platform_status = STATUS
    unit.seller = SELLER
    unit.delivery_otp = "0482"
    db.commit()
    return order


@pytest.mark.parametrize("sheet", [None, "out_for_delivery", "order_data"])
def test_no_cell_of_any_ledger_csv_runs_as_a_formula(api, order, sheet):
    response = api.get("/orders/export.csv", params={"sheet": sheet} if sheet else {})
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.text.lstrip("﻿"))))
    header, line = rows[0], rows[1]
    assert not [(name, cell) for name, cell in zip(header, line) if runs_as_a_formula(cell)]
    # The free text is all still there, behind its one space.
    assert f" {EVIL['model']}" in line
    # The identifiers keep their own text form, untouched.
    assert '="OD338975799606623101"' in line or "OD338975799606623101" in line
    assert '="0482"' in line
    if sheet is None:
        named = dict(zip(header, line))
        assert named["Unit ID"] == '="439123456789012345678"'
        assert named["Card Number"] == '="4111111111111111"'
        assert named["Seller Name"] == f" {SELLER}"
        assert named["Status"] == f" {STATUS}"
        assert named["Product"] == f" {EVIL['model']}"
        assert named["Business Name"] == f" {EVIL['gst_name']}"


def test_no_cell_of_the_invoice_data_csv_runs_as_a_formula():
    order = Order(order_id="OD338975799606623102", model="=model()", status=OrderStatus.placed)
    invoice = Invoice(
        invoice_type="=type()",
        invoice_number="INV1",
        reason="-reason",
        buyer_name="@buyer",
        buyer_gstin="09AAAAA0000A1Z5",
        seller_name="+seller",
        seller_gstin="29AABCF8078M1ZX",
        lines=[{"title": "=SUM(1)", "imei": "864414083556330", "taxable": -100.0, "igst": -18.0, "total": -118.0}],
    )
    text = "".join(invoice_data_csv.stream([(invoice, order)])).lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text)))
    header, line = rows[0], rows[1]
    assert not [(name, cell) for name, cell in zip(header, line) if runs_as_a_formula(cell)]
    # A credit note's negative amounts stay numbers a spreadsheet can sum.
    assert "-100.00" in line and "-118.00" in line
    assert '="864414083556330"' in line


def _formula_cells(data: bytes) -> list[str]:
    """Every <f> element (a stored formula) in the workbook's sheets."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/") and b"<f>" in archive.read(name)
        ]


def test_the_orders_workbook_stores_formula_like_text_as_text(api, order):
    response = api.get("/orders/export")
    assert response.status_code == 200
    assert _formula_cells(response.content) == []
    sheet = load_workbook(io.BytesIO(response.content)).active
    values = {cell.value: cell.data_type for row in sheet.iter_rows() for cell in row if cell.value is not None}
    assert values[EVIL["model"]] == "s"
    assert values[EVIL["gst_name"]] == "s"
    assert values[EVIL["product_link"]] == "s"


def test_the_accounts_workbook_stores_formula_like_text_as_text(db, order):
    data = excel.export_accounts(db)
    assert _formula_cells(data) == []
    sheet = load_workbook(io.BytesIO(data)).active
    values = {cell.value: cell.data_type for row in sheet.iter_rows() for cell in row if cell.value is not None}
    assert values[EVIL["model"]] == "s"
    assert values[EVIL["gst_name"]] == "s"
