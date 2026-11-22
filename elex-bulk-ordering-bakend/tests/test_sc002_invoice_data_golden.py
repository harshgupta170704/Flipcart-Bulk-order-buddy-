"""SC-002, file by file: the invoice-data CSV over all six real invoice PDFs
the tests keep — the owner's first batch (25 Oct 2026: a product invoice
and two fee invoices of one order) and second (26 Oct: a two-line fee
invoice, Flipkart India's transport invoice and an inter-state product
invoice). They go in the way the operator puts them in — uploaded — and
the CSV comes out with every type ticked.

Every row must carry the Taxable Amount, at least one tax, the Total Amount
and the seller's GSTIN; and the buyer's GSTIN wherever the PDF prints one.
Where it prints none — the two fee invoices of 25 Oct — the cell stays
blank: the CSV carries what the document says, and does not fill in a
GSTIN from the order or another invoice. Whether a filing CSV should is a
question for the owner's accountant, not something to decide here.

The golden table below is each PDF's lines as printed; the buyer rule is
also checked against the PDF's own text, so a blank cell is only ever one
the document leaves blank.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invoices as invoices_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus
from app.io import invoice_data_csv, invoice_store
from app.io.invoice_pdf import extract_text

FIXTURES = Path(__file__).parent / "fixtures" / "invoices"
BOM = "﻿"
#: A GSTIN as printed: state code, PAN, entity digit, "Z", check character.
GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z\d]Z[A-Z\d]\b")

#: (order folder, file) -> (invoice number, invoice type, seller GSTIN, buyer
#: GSTIN as printed or "" when the PDF prints none, and each line as
#: (Invoice Product, Taxable, IGST, SGST, CGST, Total) the way the CSV writes it).
GOLDEN = {
    ("OD332627468086718100", "file_1.pdf"): (
        "FASUUT2500113513", "Product", "09AAGCK4304E1ZT", "09AAUFT2204P1ZI",
        [
            ("Apple iPhone 15 (Blue, 128 GB)", "43219.50", "", "3889.75", "3889.75", "50999.00"),
            ("Shipping And Packaging Charges", "0.00", "", "0.00", "0.00", "0.00"),
        ],
    ),
    ("OD332627468086718100", "file_2.pdf"): (
        "BFF8825001967376", "Secure Packaging Fee", "09AAGCK4304E1ZT", "",
        [("Secure Packaging Fee", "83.90", "", "7.55", "7.55", "99.00")],
    ),
    ("OD332627468086718100", "file_3.pdf"): (
        "BFF8825001967375", "Offer Handling Fee", "29AACCF0683K1ZD", "",
        [("Offer Handling Fee", "41.53", "7.47", "", "", "49.00")],
    ),
    ("OD338918803190451100", "file_1.pdf"): (
        "MXAAB27007778336", "Credit Card Fee + Offer Handling Fee", "29AACCF0683K1ZD", "09BUAPA6939F1ZP",
        [
            ("Offer Handling Fee", "109.32", "19.68", "", "", "129.00"),
            ("Credit Card Fee", "40.68", "7.32", "", "", "48.00"),
        ],
    ),
    ("OD338918803190451100", "file_2.pdf"): (
        "NBABJ27012027019", "GT Charges", "29AABCF8078M1ZX", "09BUAPA6939F1ZP",
        [("GT Charges", "497.46", "89.54", "", "", "587.00")],
    ),
    ("OD338918803190451100", "file_3.pdf"): (
        "LIBFX4S270000245", "Product", "29AAHCH2846P2ZU", "09BUAPA6939F1ZP",
        [("vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)", "12849.15", "2312.85", "", "", "15162.00")],
    ),
}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    return tmp_path / "invoices"


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(invoices_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def csv_rows(api, db) -> list[dict[str, str]]:
    """Both orders in the ledger, all six PDFs uploaded, and the invoice-data
    CSV of every type found, as dicts by header."""
    account = Account(email="golden@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    db.add_all(
        [
            Order(account_id=account.id, order_id=order_id, status=OrderStatus.placed, qty=1,
                  model=model, placed_at=datetime(2026, 10, day, 4, 0))
            for order_id, model, day in (
                ("OD332627468086718100", "Apple iPhone 15 (Blue, 128 GB)", 20),
                ("OD338918803190451100", "vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)", 18),
            )
        ]
    )
    db.commit()

    files = [
        ("files", (f"{folder}-{name}", (FIXTURES / folder / name).read_bytes(), "application/pdf"))
        for folder, name in GOLDEN
    ]
    upload = api.post("/invoices/upload", files=files).json()
    assert upload["stored"] == 6
    assert {(f["status"], f["reading"]) for f in upload["files"]} == {("stored", "read")}

    types = [item["type"] for item in api.get("/invoices/types").json()["types"]]
    assert sorted(types) == sorted({golden[1] for golden in GOLDEN.values()})
    response = api.get("/invoices/data.csv", params=[("type", t) for t in types])
    assert response.status_code == 200, response.text
    assert response.headers["x-units-left-out"] == "0"
    assert response.text.startswith(BOM)
    rows = list(csv.reader(io.StringIO(response.text[len(BOM):])))
    assert rows[0] == invoice_data_csv.HEADERS
    assert response.headers["x-invoice-rows"] == str(len(rows) - 1)
    return [dict(zip(rows[0], row)) for row in rows[1:]]


def _printed_buyer_gstins(folder: str, name: str, seller: str) -> list[str]:
    """The GSTINs the PDF prints other than the seller's — read off the
    document's own text, not the parser's reading of it."""
    text = extract_text((FIXTURES / folder / name).read_bytes()).replace("\t", " ")
    return [gstin for gstin in dict.fromkeys(GSTIN.findall(text)) if gstin != seller]


def test_every_row_carries_the_amounts_a_tax_and_the_seller_gstin(csv_rows):
    assert len(csv_rows) == sum(len(golden[4]) for golden in GOLDEN.values()) == 8
    for row in csv_rows:
        where = (row["Invoice Number"], row["Invoice Product"])
        assert row["Taxable Amount"], where
        assert any(row[tax] for tax in ("IGST", "SGST", "CGST")), where
        assert row["Total Amount"], where
        assert row["Seller GST"], where
        # The columns are the right way round: the taxes on the taxable make the total.
        parts = sum(float(row[column] or 0) for column in ("Taxable Amount", "IGST", "SGST", "CGST"))
        assert abs(parts - float(row["Total Amount"])) <= 0.01, where


@pytest.mark.parametrize(("folder", "name"), list(GOLDEN))
def test_each_pdf_is_in_the_csv_line_for_line_as_printed(csv_rows, folder, name):
    number, kind, seller, buyer, lines = GOLDEN[(folder, name)]
    rows = [row for row in csv_rows if row["Invoice Number"] == number]

    assert [
        (row["Invoice Product"], row["Taxable Amount"], row["IGST"], row["SGST"], row["CGST"], row["Total Amount"])
        for row in rows
    ] == lines
    assert {(row["Invoice Type"], row["Order ID"], row["Seller GST"], row["Buyer GST"]) for row in rows} == {
        (kind, folder, seller, buyer)
    }


@pytest.mark.parametrize(("folder", "name"), list(GOLDEN))
def test_the_buyer_gstin_is_there_exactly_when_the_pdf_prints_one(csv_rows, folder, name):
    number, _kind, seller, _buyer, _lines = GOLDEN[(folder, name)]
    printed = _printed_buyer_gstins(folder, name, seller)
    cells = {row["Buyer GST"] for row in csv_rows if row["Invoice Number"] == number}

    if printed:
        assert cells == {printed[0]}
    else:
        # Not filled in from elsewhere: the document prints none (a question
        # for the owner's accountant, not a gap to paper over here).
        assert cells == {""}


def test_the_two_fee_invoices_of_25_october_are_the_ones_that_print_no_buyer_gstin():
    """Pins which documents leave the buyer blank, so a fixture change that
    silently removes the blank case is seen."""
    blank = [key for key, golden in GOLDEN.items() if not _printed_buyer_gstins(*key, golden[2])]
    assert blank == [("OD332627468086718100", "file_2.pdf"), ("OD332627468086718100", "file_3.pdf")]
