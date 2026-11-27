"""Reading Flipkart invoice PDFs (spec FR-028, SC-002): the three real
invoices of one order — a product tax invoice and two fee invoices — read
from bytes with every field checked; garbage that must never raise; and the
text-only cases for a wrapped title, a credit note and a three-tax header."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.io import invoice_pdf
from app.io.invoice_pdf import PARSER_VERSION, ParsedLine, parse_invoice, parse_invoice_text

FIXTURES = Path(__file__).parent / "fixtures" / "invoices" / "OD332627468086718100"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_the_parser_has_a_version():
    assert PARSER_VERSION == 5  # 2: the transport invoice and multi-line fee labels; 3: a second IMEI in one bracket; 4: reference names; 5: Nov 2026 credit-note labels


# --- the real invoices -----------------------------------------------------------------
def test_the_product_tax_invoice_is_read_in_full():
    out = parse_invoice(_read("file_1.pdf"))

    assert out.error is None
    assert (out.kind, out.invoice_type) == ("product", "Product")
    assert out.invoice_number == "FASUUT2500113513"
    assert out.order_id == "OD332627468086718100"
    assert out.order_date == date(2024, 10, 20)
    assert out.invoice_date == date(2024, 10, 21)
    assert out.seller_name == "Konde Products & Services Private Limited"
    assert out.seller_gstin == "09AAGCK4304E1ZT"
    assert out.buyer_name == "Transparent Deals"
    assert out.buyer_gstin == "09AAUFT2204P1ZI"
    assert out.grand_total == 50999.00
    assert (out.original_invoice, out.reason) == (None, None)

    assert out.lines == [
        ParsedLine(
            title="Apple iPhone 15 (Blue, 128 GB)",
            fsn="MOBGTAGPAQNVFZZY",
            hsn_sac="85171300",
            imei="358107262645105",
            qty=1,
            gross=57999.00,
            discount=-7000.00,
            taxable=43219.50,
            cgst=3889.75,
            sgst=3889.75,
            igst=None,
            total=50999.00,
        ),
        ParsedLine(
            title="Shipping And Packaging Charges",
            fsn=None,
            hsn_sac=None,
            imei=None,
            qty=1,
            gross=40.00,
            discount=-40.00,
            taxable=0.0,
            cgst=0.0,
            sgst=0.0,
            igst=None,
            total=0.0,
        ),
    ]


def test_the_secure_packaging_fee_invoice_is_read_in_full():
    out = parse_invoice(_read("file_2.pdf"))

    assert out.error is None
    assert (out.kind, out.invoice_type) == ("fee", "Secure Packaging Fee")
    assert out.invoice_number == "BFF8825001967376"
    assert out.order_id == "OD332627468086718100"
    assert (out.order_date, out.invoice_date) == (date(2024, 10, 20), date(2024, 10, 21))
    assert (out.seller_name, out.seller_gstin) == ("Konde Products & Services Private Limited", "09AAGCK4304E1ZT")
    assert out.buyer_name == "Transparent Deals"
    assert out.buyer_gstin is None  # a fee invoice prints a billing address, no GSTIN
    assert out.grand_total == 99.00
    assert out.lines == [
        ParsedLine(
            title="Secure Packaging Fee",
            fsn=None,
            hsn_sac="998599",
            imei="358107262645105",
            qty=1,
            gross=99.00,
            discount=0.0,
            taxable=83.90,
            cgst=7.55,
            sgst=7.55,
            igst=None,
            total=99.00,
        )
    ]


def test_the_offer_handling_fee_invoice_is_read_in_full():
    out = parse_invoice(_read("file_3.pdf"))

    assert out.error is None
    assert (out.kind, out.invoice_type) == ("fee", "Offer Handling Fee")
    assert out.invoice_number == "BFF8825001967375"
    assert out.order_id == "OD332627468086718100"
    assert (out.order_date, out.invoice_date) == (date(2024, 10, 20), date(2024, 10, 21))
    assert (out.seller_name, out.seller_gstin) == ("Flipkart Internet Private Limited", "29AACCF0683K1ZD")
    assert out.buyer_name == "Transparent Deals"
    assert out.buyer_gstin is None
    assert out.grand_total == 49.00
    assert out.lines == [
        ParsedLine(
            title="Offer Handling Fee",
            fsn=None,
            hsn_sac="998599",
            imei="358107262645105",
            qty=1,
            gross=49.00,
            discount=0.0,
            taxable=41.53,
            cgst=None,
            sgst=None,
            igst=7.47,  # inter-state: IGST only, no CGST/SGST columns
            total=49.00,
        )
    ]


def test_every_line_of_the_samples_carries_what_sc_002_asks_for():
    """Taxable, a tax, the total and the seller's GSTIN on every line; the
    buyer's GSTIN wherever the invoice prints one (product invoices)."""
    for name in ("file_1.pdf", "file_2.pdf", "file_3.pdf"):
        out = parse_invoice(_read(name))
        assert out.lines, name
        assert out.seller_gstin, name
        for line in out.lines:
            assert line.taxable is not None and line.total is not None, (name, line)
            assert any(tax is not None for tax in (line.cgst, line.sgst, line.igst)), (name, line)
    assert parse_invoice(_read("file_1.pdf")).buyer_gstin == "09AAUFT2204P1ZI"


# --- never raises ----------------------------------------------------------------------
@pytest.mark.parametrize("data", [b"", b"hello world", b"%PDF-1.4\n%fake invoice\n", b"\x00" * 64])
def test_garbage_comes_back_as_unknown_with_the_reason(data):
    out = parse_invoice(data)
    assert out.kind == "unknown"
    assert out.invoice_type is None
    assert out.lines == []
    assert out.error and "could not read the PDF" in out.error


def test_empty_text_is_unknown_too():
    out = parse_invoice_text("   \n\n")
    assert out.kind == "unknown"
    assert out.error == "the PDF has no text layer"


def test_a_parser_crash_is_recorded_not_raised(monkeypatch):
    def boom(_text: str):
        raise RuntimeError("layout the parser never met")

    monkeypatch.setattr(invoice_pdf, "parse_invoice_text", boom)
    out = parse_invoice(_read("file_1.pdf"))
    assert out.kind == "unknown"
    assert "layout the parser never met" in (out.error or "")


# --- text only ------------------------------------------------------------------------
WRAPPED = """Tax Invoice
Sold By: Some Seller Private Limited ,
GSTIN - 29AAACS1234A1Z5
IRN - abc\tInvoice Number # FAXYZ1
Order ID:\tBill To\tShip To
OD123456789012345678\tAcme Traders\tAcme Traders
Order Date: 01-02-2026\tACME TRADERS\tStreet 1
Invoice Date: 02-02-2026
Total items: 1
Product\tTitle\tQty\tGross\tDiscounts\tTaxable\tIGST\tTotal ₹
Amount ₹\t/Coupons\tValue ₹
Handsets\tApple iPhone 15 Pro Max (Natural\t2\t2,59,998.00\t0.00\t2,20,337.29\t39,660.71\t2,59,998.00
FSN:\tTitanium, 256
MOBXYZ1234567890\tGB)
HSN/SAC: 85171300\tWarranty: 1 year
1. [IMEI/Serial No: 111111111111111 ]
2. [IMEI/Serial No: 222222222222222 ]
IGST: 18.0 %
Total\t2\t2,59,998.00\t0.00\t2,20,337.29\t39,660.71\t2,59,998.00
Grand Total\t₹ 2,59,998.00
Some Seller Private Limited
Signature
"""


def test_a_title_wrapped_over_three_lines_is_joined_and_the_rest_is_not():
    out = parse_invoice_text(WRAPPED)

    assert out.error is None
    assert (out.kind, out.invoice_type, out.invoice_number) == ("product", "Product", "FAXYZ1")
    assert out.order_id == "OD123456789012345678"
    assert (out.order_date, out.invoice_date) == (date(2026, 2, 1), date(2026, 2, 2))
    assert (out.seller_name, out.seller_gstin) == ("Some Seller Private Limited", "29AAACS1234A1Z5")
    assert (out.buyer_name, out.buyer_gstin) == ("Acme Traders", None)
    assert out.grand_total == 259998.00
    assert len(out.lines) == 1
    line = out.lines[0]
    assert line.title == "Apple iPhone 15 Pro Max (Natural Titanium, 256 GB)"
    assert (line.fsn, line.hsn_sac) == ("MOBXYZ1234567890", "85171300")
    assert line.imei == "111111111111111, 222222222222222"
    assert (line.qty, line.gross, line.discount, line.taxable) == (2, 259998.00, 0.0, 220337.29)
    assert (line.cgst, line.sgst, line.igst, line.total) == (None, None, 39660.71, 259998.00)


def test_plain_text_without_cell_marks_still_reads_the_wrap_and_the_codes():
    """pdfplumber's plain text runs the Product column into the title; the
    labels still place the FSN, HSN and IMEI, and the wrap still joins."""
    plain = WRAPPED.replace("\t", " ")
    out = parse_invoice_text(plain)

    assert out.kind == "product"
    assert out.buyer_name == "Acme Traders"  # printed twice (billed to = shipped to), read once
    line = out.lines[0]
    assert line.title.endswith("Apple iPhone 15 Pro Max (Natural Titanium, 256 GB)")
    assert (line.fsn, line.hsn_sac, line.qty, line.igst) == ("MOBXYZ1234567890", "85171300", 2, 39660.71)
    assert line.imei == "111111111111111, 222222222222222"


CREDIT_NOTE = """Credit Note
Sold By: Konde Products & Services Private Limited ,
Ship-from Address: Gata No 193, Lucknow, UTTAR PRADESH, India - 209859, IN-UP ,
GSTIN - 09AAGCK4304E1ZT
Credit Note Number # CN2500113513
Original Invoice Number # FASUUT2500113513
Reason: Return
Order ID:\tBill To
OD332627468086718100\tTransparent Deals
Order Date: 20-10-2024\tTRANSPARENT DEALS
Credit Note Date: 25-10-2024
09AAUFT2204P1ZI
Product\tTitle\tQty\tGross\tDiscounts\tTaxable\tCGST ₹\tSGST\tTotal ₹
Handsets\tApple iPhone 15 (Blue, 128 GB)\t1\t57999.00\t-7000.00\t43219.50\t3889.75\t3889.75\t50999.00
FSN:\tWarranty: 1 year
MOBGTAGPAQNVFZZY
HSN/SAC: 85171300
Total\t1\t57999.00\t-7000.00\t43219.50\t3889.75\t3889.75\t50999.00
Grand Total\t₹ 50999.00
"""


def test_a_credit_note_is_read_on_its_labels():
    out = parse_invoice_text(CREDIT_NOTE)

    assert out.error is None
    # Named for what it reverses, as the owner's reference file names it.
    assert (out.kind, out.invoice_type) == ("credit_note", "Product Credit Note")
    assert out.invoice_number == "CN2500113513"  # not the original invoice's number
    assert out.original_invoice == "FASUUT2500113513"
    assert out.reason == "Return"
    assert out.invoice_date == date(2024, 10, 25)
    assert out.order_date == date(2024, 10, 20)
    assert (out.buyer_name, out.buyer_gstin) == ("Transparent Deals", "09AAUFT2204P1ZI")
    assert out.grand_total == 50999.00
    assert [line.title for line in out.lines] == ["Apple iPhone 15 (Blue, 128 GB)"]
    assert (out.lines[0].fsn, out.lines[0].hsn_sac) == ("MOBGTAGPAQNVFZZY", "85171300")


def test_a_credit_note_with_no_line_table_is_still_a_credit_note():
    text = "\n".join(CREDIT_NOTE.splitlines()[:12])
    out = parse_invoice_text(text)
    assert (out.kind, out.invoice_type, out.lines, out.error) == ("credit_note", "Credit Note", [], None)
    assert out.original_invoice == "FASUUT2500113513"


def test_a_header_with_all_three_tax_columns_maps_each_one():
    text = (
        "Tax Invoice\nSold By: X Ltd ,\nGSTIN - 29AAACS1234A1Z5\nInvoice Number # F1\n"
        "Order ID: OD123456789012345678\tBilling Address\nOrder Date: 01-02-2026\tAcme Traders\nInvoice Date: 02-02-2026\n"
        "Description\tQty\tGross\tDiscount\tTaxable\tCGST\tSGST\tIGST\tTotal ₹\n"
        "SAC: 998599\tSome Fee\t1\t100.00\t0.00\t84.75\t7.63\t7.62\t0.00\t100.00\n"
        "Total\t1\t100.00\t0.00\t84.75\t7.63\t7.62\t0.00\t100.00\nGrand Total\t₹ 100.00\n"
    )
    out = parse_invoice_text(text)
    assert (out.kind, out.invoice_type, out.buyer_name) == ("fee", "Some Fee", "Acme Traders")
    line = out.lines[0]
    assert (line.hsn_sac, line.qty, line.gross, line.taxable) == ("998599", 1, 100.0, 84.75)
    assert (line.cgst, line.sgst, line.igst, line.total) == (7.63, 7.62, 0.0, 100.0)


def test_a_recognised_layout_with_nothing_readable_says_what_is_missing():
    out = parse_invoice_text("Tax Invoice\nProduct\tTitle\tQty\tGross\tDiscounts\tTaxable\tIGST\tTotal\nGrand Total ₹ 0.00\n")
    assert out.kind == "product"
    assert out.error == "no invoice number; no order id; no invoice lines"


#: Flipkart's credit-note layout of Nov 2026 (OD339240224678012100): the
#: "Original Invoice Number:" and "Reason Of Issuance:" labels stand alone in
#: the left column, their values lines below beside the address column, and
#: the Place of Supply state broken over two lines.
CREDIT_NOTE_NOV_2026 = """Credit Note
Sold By: Health & Happiness Private Limited ,
GSTIN - 06AADCH8449J1Z8
Credit Note Number # LKAAIQX270002331
Order ID:\tBill To\tShip From\tShip To
OD339240224678012100\tAyush\tAyush\tHealth & Happiness Private
Order Date: 25-11-2026\tAARTHBODHA PRIVATE\t59, PNB road, railway ganj.\tLimited
Invoice Date: 26-11-2026
59, PNB road, railway ganj.\tPhone: xxxxxxxxxx\t//20/2, 22//21 & 25//5/1, 24//1,
Original Invoice Number:\tPhone: xxxxxxxxxx\t//10,25//7, 20//24, 20//25, 21
LIAAJYY270010016\tGSTIN: 09ABACA7232Q1Z9\t//19, 21//21, 21//22, 21//23& 21
State: Uttar Pradesh\t//24, AND OTHERS, VILLAGE
Reason Of Issuance:
State Code: IN-UP\tSANPKA, TEHSIL
sales_return
Place of Supply: UTTAR\tFARUKHNAGAR, DISTRICT
PRADESH\tGURUGRAM, HARYANA, INDIA-
Product\tTitle\tQty\tGross\tDiscounts\tTaxable\tIGST ₹\tTotal ₹
Handsets\tAi+ Nova 2 5G (Pink, 64 GB)\t1\t11412.00\t0.00\t9671.19\t1740.81\t11412.00
FSN:\tWarranty: 1 Year
MOBHKHPY6XQGZXNK
HSN/SAC: 85171300
Total\t1\t11412.00\t0.00\t9671.19\t1740.81\t11412.00
Grand Total\t₹ 11412.00
"""


def test_a_nov_2026_credit_note_reads_its_column_labels_like_the_reference():
    out = parse_invoice_text(CREDIT_NOTE_NOV_2026)

    assert out.kind == "credit_note"
    assert out.invoice_number == "LKAAIQX270002331"
    # Not "Phone" (the next cell on the label's line), nor the FSN further down.
    assert out.original_invoice == "LIAAJYY270010016"
    # Not "State Code: IN-UP …": the reference file's value, state rejoined.
    assert out.reason == "Place of Supply: UTTAR PRADESH"


def test_a_credit_note_with_the_state_two_lines_below_still_joins_it():
    text = (
        "Credit Note\nCredit Note Number # LDAAAB4270190489\nOriginal Invoice Number:\tState: Uttar Pradesh\n"
        "LBAAAJA270765491\tState Code: IN-UP\nPlace of Supply: UTTAR\nReason Of Issuance:\nPRADESH\n"
    )
    out = parse_invoice_text(text)

    assert (out.original_invoice, out.reason) == ("LBAAAJA270765491", "Place of Supply: UTTAR PRADESH")
