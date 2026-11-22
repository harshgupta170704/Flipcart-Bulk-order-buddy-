"""Extract Invoices Data (spec FR-026–FR-030, SC-002): the three real invoice
PDFs of one order attached to a ledger order, then ``GET /invoices/types`` and
``GET /invoices/data.csv`` through the invoices router — the type counts
(invoices told from units), the header row, one row per line of the ticked
types, the left-out count (units with no readable invoice of the ticked types:
none on file, an unreadable one, or only other types), the Order Date
fallback's time zone, and the 422 with no type ticked. Plus what the parse
leaves on the invoice row, and the start-up backfill: a missing file left for
later, an earlier reading kept when a newer parser cannot read the PDF, the
whole table walked in chunks in the background."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invoices as invoices_api
from app.core import invoices
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Invoice, Order, OrderStatus, utcnow
from app.io import invoice_data_csv, invoice_store, orders_csv
from app.io.invoice_pdf import PARSER_VERSION, ParsedInvoice
from app.io.invoice_store import InvoiceStore

FIXTURES = Path(__file__).parent / "fixtures" / "invoices" / "OD332627468086718100"
ORDER_ID = "OD332627468086718100"
BOM = "﻿"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    return tmp_path / "invoices"


def _client(role: str) -> TestClient:
    application = FastAPI()
    application.include_router(invoices_api.router)
    token = create_access_token("tester", role=role)
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def api() -> TestClient:
    return _client("admin")


def _naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def _order(db, account: Account, order_id: str, *, model: str = "T5x", days_ago: int = 3, qty: int = 1,
           status: OrderStatus = OrderStatus.placed, pdfs: tuple[str, ...] = ()) -> Order:
    order = Order(account_id=account.id, status=status, model=model, qty=qty, order_id=order_id,
                  placed_at=_naive(utcnow()) - timedelta(days=days_ago))
    db.add(order)
    db.commit()
    store = InvoiceStore.from_settings()
    for name in pdfs:
        data = (FIXTURES / name).read_bytes() if name.endswith(".pdf") else b"%PDF-1.4\n%" + name.encode() + b"\n"
        invoices.attach(db, store, order, data, source="upload")
    db.commit()
    return order


def _invoice_row(db, order: Order, *, invoice_type: str, number: str) -> Invoice:
    """A readable invoice row written straight to the ledger, for a shape the
    three sample PDFs cannot give (they all print one order's id): a Product
    invoice on another order, a fee invoice alone. The counts and the CSV
    read rows, not files."""
    row = Invoice(
        order_pk=order.id, order_id=order.order_id, kind="product" if invoice_type == "Product" else "fee",
        invoice_type=invoice_type, invoice_number=number, invoice_date=datetime(2026, 10, 3),
        order_date=datetime(2026, 10, 2), grand_total=11.8,
        lines=[{"title": invoice_type, "qty": 1, "taxable": 10.0, "cgst": 0.9, "sgst": 0.9, "total": 11.8}],
        path=f"{number}.pdf", size=1, sha256=hashlib.sha256(number.encode()).hexdigest(),
        parsed_at=_naive(utcnow()), parser_version=PARSER_VERSION,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def ledger(db):
    """The real order with its three invoices, and a second order with none."""
    account = Account(email="inv@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    real = _order(db, account, ORDER_ID, model="Apple iPhone 15 (Blue, 128 GB)", days_ago=2,
                  pdfs=("file_1.pdf", "file_2.pdf", "file_3.pdf"))
    bare = _order(db, account, "OD000000000000000001", days_ago=1)
    return {"account": account, "real": real, "bare": bare}


def _rows(response) -> list[list[str]]:
    assert response.text.startswith(BOM)
    return list(csv.reader(io.StringIO(response.text[len(BOM):])))


# --- the parse on the row ------------------------------------------------------------------
def test_attach_reads_the_pdf_onto_the_row_and_keeps_an_unreadable_one(db, ledger):
    rows = {row.invoice_number: row for row in db.query(Invoice).filter(Invoice.order_pk == ledger["real"].id).all()}
    assert set(rows) == {"FASUUT2500113513", "BFF8825001967376", "BFF8825001967375"}
    product = rows["FASUUT2500113513"]
    assert (product.kind, product.invoice_type) == ("product", "Product")
    assert product.invoice_date == datetime(2024, 10, 21)
    assert product.order_date == datetime(2024, 10, 20)
    assert (product.buyer_name, product.buyer_gstin) == ("Transparent Deals", "09AAUFT2204P1ZI")
    assert (product.seller_name, product.seller_gstin) == ("Konde Products & Services Private Limited", "09AAGCK4304E1ZT")
    assert product.grand_total == 50999.0
    assert [line["title"] for line in product.lines] == ["Apple iPhone 15 (Blue, 128 GB)", "Shipping And Packaging Charges"]
    assert product.lines[0]["imei"] == "358107262645105"
    assert (product.parser_version, product.parse_error) == (PARSER_VERSION, None)
    assert product.parsed_at is not None
    assert rows["BFF8825001967375"].kind == "fee"
    assert rows["BFF8825001967375"].invoice_type == "Offer Handling Fee"
    assert rows["BFF8825001967375"].lines[0]["igst"] == 7.47

    # A PDF the parser cannot read stays on file, with the reason.
    _order(db, ledger["account"], "OD000000000000000002", pdfs=("not really a pdf",))
    odd = db.query(Invoice).filter(Invoice.order_id == "OD000000000000000002").one()
    assert (odd.kind, odd.invoice_type, odd.lines) == ("unknown", None, [])
    assert "could not read the PDF" in odd.parse_error
    assert odd.parser_version == PARSER_VERSION
    assert InvoiceStore.from_settings().exists(odd.path)


def test_the_backfill_reads_unparsed_rows_and_leaves_a_missing_file_for_later(db, ledger):
    """A row never parsed is read; a row whose PDF has gone from the store keeps
    everything an earlier read found and its parser version — only the error
    says the file is missing — and is read the moment the file is back."""
    product = db.query(Invoice).filter(Invoice.invoice_number == "FASUUT2500113513").one()
    fee = db.query(Invoice).filter(Invoice.invoice_number == "BFF8825001967376").one()
    product.parsed_at = None
    product.parser_version = None
    product.invoice_type = None
    product.lines = None
    fee.parser_version = PARSER_VERSION - 1  # read by an older parser...
    db.commit()
    fee_id = fee.id
    fee_path = InvoiceStore.from_settings().path(fee.path)
    fee_bytes = fee_path.read_bytes()
    fee_path.unlink()  # ...and its file has since gone from the store

    first = invoices.backfill_unparsed(limit=10)
    assert (first.parsed, first.missing, first.looked_at, first.last_id) == (1, 1, 2, fee_id)

    db.expire_all()
    assert (product.invoice_type, len(product.lines), product.parser_version) == ("Product", 2, PARSER_VERSION)
    assert fee.parse_error == invoices.MISSING_FILE_ERROR
    assert (fee.invoice_type, fee.invoice_number, fee.grand_total) == ("Secure Packaging Fee", "BFF8825001967376", 99.0)
    assert fee.lines[0]["hsn_sac"] == "998599"
    assert (fee.kind, fee.seller_gstin, fee.buyer_name) == ("fee", "09AAGCK4304E1ZT", "Transparent Deals")
    assert fee.parser_version == PARSER_VERSION - 1  # not advanced: the file was never read by this parser

    # Looked at again at the next start — a cheap stat, nothing rewritten...
    again = invoices.backfill_unparsed(limit=10)
    assert (again.parsed, again.missing) == (0, 1)
    # ...and read as soon as the file is back.
    fee_path.write_bytes(fee_bytes)
    back = invoices.backfill_unparsed(limit=10)
    assert (back.parsed, back.missing) == (1, 0)
    db.expire_all()
    assert (fee.parse_error, fee.parser_version) == (None, PARSER_VERSION)
    assert invoices.backfill_unparsed(limit=10).last_id is None  # nothing left


def test_a_newer_parser_that_cannot_read_a_pdf_keeps_the_earlier_reading(api, db, ledger, monkeypatch, caplog):
    """NFR-009: a failed re-read leaves stored data unchanged. A newer parser
    that fails on a PDF an older one read must not blank the row — the
    invoice would drop out of Extract Invoices Data with only a log line to
    show for it. The earlier reading stays, the reason is noted, and the row
    is not read again at every start; a later parser that reads the PDF
    writes its reading as usual."""
    product = db.query(Invoice).filter(Invoice.invoice_number == "FASUUT2500113513").one()
    product.parser_version = PARSER_VERSION - 1  # read by an older parser
    db.commit()
    before = {column: getattr(product, column) for column in (
        "kind", "invoice_type", "invoice_number", "invoice_date", "order_date", "seller_name", "seller_gstin",
        "buyer_name", "buyer_gstin", "grand_total", "lines",
    )}
    real_parse = invoices.parse_pdf
    monkeypatch.setattr(invoices, "parse_pdf", lambda _data: ParsedInvoice(error="could not read the PDF: broken xref"))

    with caplog.at_level(logging.WARNING, logger="ordering.invoices"):
        done = invoices.backfill_unparsed(limit=10)

    assert (done.parsed, done.missing, done.unsaved) == (1, 0, 0)
    db.expire_all()
    assert {column: getattr(product, column) for column in before} == before
    assert product.parse_error == f"could not read the PDF: broken xref; {invoices.EARLIER_READING_KEPT}"
    assert product.parser_version == PARSER_VERSION  # this parser had its go: not read at every start
    assert f"invoice {product.id}: its PDF could not be read again" in caplog.text
    assert invoices.backfill_unparsed(limit=10).last_id is None
    # Still in the invoice-data CSV, both of its lines.
    response = api.get("/invoices/data.csv", params={"type": "Product", "order_id": ORDER_ID})
    assert (response.headers["x-invoice-rows"], response.headers["x-units-left-out"]) == ("2", "0")

    # A later parser that can read it writes its reading, and the note goes.
    monkeypatch.setattr(invoices, "parse_pdf", real_parse)
    monkeypatch.setattr(invoices, "PARSER_VERSION", PARSER_VERSION + 1)
    assert invoices.backfill_unparsed(limit=10).parsed == 3
    db.expire_all()
    assert (product.invoice_type, product.parse_error, product.parser_version) == ("Product", None, PARSER_VERSION + 1)


def test_backfill_all_walks_the_table_in_chunks_and_stops_when_a_pass_finds_nothing(db, ledger, monkeypatch):
    """Three unparsed rows, two per pass: 2, then 1, then an empty pass that
    ends the run — every row read once, however many there are, with no cap
    per start."""
    for row in db.query(Invoice).all():
        row.parsed_at = None
    db.commit()
    passes: list[int] = []
    real = invoices.backfill_unparsed

    def spy(limit, **kwargs):
        out = real(limit, **kwargs)
        passes.append(out.looked_at)
        return out

    monkeypatch.setattr(invoices, "backfill_unparsed", spy)

    assert asyncio.run(invoices.backfill_all(chunk=2, pause=0)) == 3

    assert passes == [2, 1, 0]
    db.expire_all()
    assert all(row.parsed_at is not None for row in db.query(Invoice).all())


def test_backfill_all_logs_a_failure_and_never_raises(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(invoices, "backfill_unparsed", boom)
    with caplog.at_level(logging.ERROR, logger="ordering.invoices"):
        assert asyncio.run(invoices.backfill_all(pause=0)) == 0
    assert "invoice backfill failed" in caplog.text


def test_the_background_backfill_stops_cleanly_when_cancelled_mid_way(monkeypatch):
    """Shutdown in the middle of a pass: the pass in hand is told to stop, and
    the task ends cancelled — nothing raised out of it."""
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    asked: list[bool] = []

    def slow_pass(limit, *, store=None, after_id=0, should_stop=None):
        started.set()
        release.wait(5)
        asked.append(bool(should_stop and should_stop()))
        finished.set()
        return invoices.BackfillChunk(parsed=0, missing=0, last_id=None)

    monkeypatch.setattr(invoices, "backfill_unparsed", slow_pass)

    async def scenario() -> asyncio.Task:
        task = invoices.start_backfill_in_background(pause=0)
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.to_thread(finished.wait, 5)
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert asked == [True]


def test_the_app_starts_the_backfill_in_the_background_and_stops_it_at_shutdown(monkeypatch):
    """The start-up backfill must not hold the first request: main.py starts
    it as a task and cancels the handle at shutdown."""
    from app import main as main_module
    from app.main import app

    class Handle:
        cancelled = False

        def cancel(self) -> bool:
            self.cancelled = True
            return True

    handle = Handle()
    calls: list[dict] = []
    monkeypatch.setattr(main_module, "start_backfill_in_background", lambda **options: calls.append(options) or handle)
    with TestClient(app):
        assert calls == [{}]
        assert not handle.cancelled
    assert handle.cancelled


# --- the types ---------------------------------------------------------------------------
def test_the_types_are_built_from_the_invoices_found_with_counts(api, ledger):
    response = api.get("/invoices/types")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["types"] == [
        {"type": "Product", "invoices": 1, "units": 1},
        {"type": "Offer Handling Fee", "invoices": 1, "units": 1},
        {"type": "Secure Packaging Fee", "invoices": 1, "units": 1},
    ]
    assert (body["units"], body["units_without_invoice"]) == (2, 1)


def test_the_type_counts_tell_invoices_from_units(api, db, ledger):
    """An order of two units with two Product invoices is 2 invoices on 2
    units — not the 4 and 4 a join without DISTINCT would count, since each
    invoice of an order meets each of its units once."""
    pair = _order(db, ledger["account"], "OD000000000000000003", qty=2)
    assert len(pair.units) == 2
    for number in ("FA-PAIR-1", "FA-PAIR-2"):
        _invoice_row(db, pair, invoice_type="Product", number=number)
    db.commit()

    body = api.get("/invoices/types").json()
    by_type = {item["type"]: item for item in body["types"]}
    assert by_type["Product"] == {"type": "Product", "invoices": 3, "units": 3}
    assert by_type["Offer Handling Fee"] == {"type": "Offer Handling Fee", "invoices": 1, "units": 1}
    assert (body["units"], body["units_without_invoice"]) == (4, 1)

    pair_only = api.get("/invoices/types", params={"order_id": "OD000000000000000003"}).json()
    assert pair_only["types"] == [{"type": "Product", "invoices": 2, "units": 2}]
    assert (pair_only["units"], pair_only["units_without_invoice"]) == (2, 0)


def test_the_types_follow_the_ledger_filter(api, ledger):
    narrowed = api.get("/invoices/types", params={"order_id": "OD000000000000000001"}).json()
    assert narrowed == {"types": [], "units": 1, "units_without_invoice": 1}
    none = api.get("/invoices/types", params={"status": "failed"}).json()
    assert none == {"types": [], "units": 0, "units_without_invoice": 0}
    assert api.get("/invoices/types", params={"date_field": "nope"}).status_code == 422


def test_an_operator_can_read_the_types_and_the_csv(ledger):
    operator = _client("operator")
    assert operator.get("/invoices/types").status_code == 200
    assert operator.get("/invoices/data.csv", params={"type": "Product"}).status_code == 200


# --- the CSV -----------------------------------------------------------------------------
def test_the_csv_has_the_spec_columns_and_one_row_per_line_of_the_ticked_types(api, ledger):
    response = api.get("/invoices/data.csv", params=[("type", "Product"), ("type", "Secure Packaging Fee")])

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert re.fullmatch(
        r'attachment; filename="invoice_data_\d{4}-\d{2}-\d{2}_\d{4}\.csv"', response.headers["content-disposition"]
    )
    assert (response.headers["x-invoice-rows"], response.headers["x-units-left-out"]) == ("3", "1")
    rows = _rows(response)
    assert rows[0] == invoice_data_csv.HEADERS == [
        "Invoice Type", "Invoice Number", "Order ID", "Invoice Date", "Order Date", "Original Invoice Number", "Reason",
        "IMEI", "Invoice Product", "Ledger Product", "Qty", "FSN", "HSN", "Taxable Amount",
        "IGST", "SGST", "CGST", "Total Amount", "Buyer Name", "Buyer GST", "Seller Name", "Seller GST",
    ]
    assert len(rows) == 4
    assert [row[0] for row in rows[1:]] == ["Product", "Product", "Secure Packaging Fee"]

    phone = dict(zip(rows[0], rows[1]))
    assert phone == {
        "Invoice Type": "Product",
        "Invoice Number": "FASUUT2500113513",
        "Order ID": ORDER_ID,
        "Invoice Date": "21-10-2024",
        "Order Date": "20-10-2024",
        "Original Invoice Number": "",
        "Reason": "",
        "IMEI": '="358107262645105"',
        "Invoice Product": "Apple iPhone 15 (Blue, 128 GB)",
        "Ledger Product": "Apple iPhone 15 (Blue, 128 GB)",
        "Qty": "1",
        "FSN": "MOBGTAGPAQNVFZZY",
        "HSN": '="85171300"',
        "Taxable Amount": "43219.50",
        "IGST": "",
        "SGST": "3889.75",
        "CGST": "3889.75",
        "Total Amount": "50999.00",
        "Buyer Name": "Transparent Deals",
        "Buyer GST": "09AAUFT2204P1ZI",
        "Seller Name": "Konde Products & Services Private Limited",
        "Seller GST": "09AAGCK4304E1ZT",
    }
    shipping = dict(zip(rows[0], rows[2]))
    assert (shipping["Invoice Product"], shipping["IMEI"], shipping["HSN"]) == ("Shipping And Packaging Charges", "", "")
    assert (shipping["Taxable Amount"], shipping["Total Amount"], shipping["Qty"]) == ("0.00", "0.00", "1")
    fee = dict(zip(rows[0], rows[3]))
    assert (fee["Invoice Number"], fee["HSN"], fee["Buyer GST"]) == ("BFF8825001967376", '="998599"', "")
    assert (fee["Taxable Amount"], fee["CGST"], fee["SGST"], fee["IGST"], fee["Total Amount"]) == ("83.90", "7.55", "7.55", "", "99.00")


def test_only_the_ticked_types_are_written(api, ledger):
    one = api.get("/invoices/data.csv", params={"type": "Offer Handling Fee"})
    rows = _rows(one)
    assert one.headers["x-invoice-rows"] == "1"
    assert len(rows) == 2
    row = dict(zip(rows[0], rows[1]))
    assert (row["Invoice Type"], row["Seller Name"], row["Seller GST"]) == ("Offer Handling Fee", "Flipkart Internet Private Limited", "29AACCF0683K1ZD")
    assert (row["IGST"], row["SGST"], row["CGST"], row["Total Amount"]) == ("7.47", "", "", "49.00")

    everything = api.get(
        "/invoices/data.csv", params=[("type", "Product"), ("type", "Offer Handling Fee"), ("type", "Secure Packaging Fee")]
    )
    assert everything.headers["x-invoice-rows"] == "4"
    assert len(_rows(everything)) == 5

    unknown = api.get("/invoices/data.csv", params={"type": "No Such Type"})
    assert unknown.status_code == 200
    assert unknown.headers["x-invoice-rows"] == "0"
    assert _rows(unknown) == [invoice_data_csv.HEADERS]


def test_the_csv_follows_the_ledger_filter_and_counts_the_left_out_units(api, ledger):
    only_real = api.get("/invoices/data.csv", params={"type": "Product", "order_id": ORDER_ID})
    assert (only_real.headers["x-invoice-rows"], only_real.headers["x-units-left-out"]) == ("2", "0")

    only_bare = api.get("/invoices/data.csv", params={"type": "Product", "order_id": "OD000000000000000001"})
    assert (only_bare.headers["x-invoice-rows"], only_bare.headers["x-units-left-out"]) == ("0", "1")
    assert _rows(only_bare) == [invoice_data_csv.HEADERS]


def test_a_unit_whose_only_invoice_could_not_be_read_counts_as_left_out(api, db, ledger):
    """FR-030 by what the CSV writes: an unreadable PDF is on file but in no
    type's count and in no CSV row, so its unit is left out — and said to be,
    rather than dropped in silence."""
    _order(db, ledger["account"], "OD000000000000000002", pdfs=("not really a pdf",))

    body = api.get("/invoices/types").json()
    assert [item["type"] for item in body["types"]] == ["Product", "Offer Handling Fee", "Secure Packaging Fee"]
    assert (body["units"], body["units_without_invoice"]) == (3, 2)

    response = api.get("/invoices/data.csv", params={"type": "Product"})
    assert (response.headers["x-invoice-rows"], response.headers["x-units-left-out"]) == ("2", "2")
    only_odd = api.get("/invoices/data.csv", params={"type": "Product", "order_id": "OD000000000000000002"})
    assert (only_odd.headers["x-invoice-rows"], only_odd.headers["x-units-left-out"]) == ("0", "1")
    assert _rows(only_odd) == [invoice_data_csv.HEADERS]


def test_a_unit_with_no_invoice_of_the_ticked_types_is_left_out_of_that_csv(api, db, ledger):
    """Only a fee invoice on file and Product ticked: nothing of the unit is in
    the file, so it is counted as left out of this CSV; tick its type and it
    is in. The types list has no ticks yet, so there any readable invoice counts."""
    fee_only = _order(db, ledger["account"], "OD000000000000000004")
    _invoice_row(db, fee_only, invoice_type="Secure Packaging Fee", number="BFF-ALONE")
    db.commit()

    product = api.get("/invoices/data.csv", params={"type": "Product", "order_id": "OD000000000000000004"})
    assert (product.headers["x-invoice-rows"], product.headers["x-units-left-out"]) == ("0", "1")
    fee = api.get("/invoices/data.csv", params={"type": "Secure Packaging Fee", "order_id": "OD000000000000000004"})
    assert (fee.headers["x-invoice-rows"], fee.headers["x-units-left-out"]) == ("1", "0")
    both = api.get("/invoices/data.csv", params=[("type", "Product"), ("type", "Secure Packaging Fee")])
    assert (both.headers["x-invoice-rows"], both.headers["x-units-left-out"]) == ("4", "1")  # the bare order only

    types = api.get("/invoices/types", params={"order_id": "OD000000000000000004"}).json()
    assert types["types"] == [{"type": "Secure Packaging Fee", "invoices": 1, "units": 1}]
    assert (types["units"], types["units_without_invoice"]) == (1, 0)


def test_no_type_ticked_is_a_422(api, ledger):
    assert api.get("/invoices/data.csv").status_code == 422
    assert api.get("/invoices/data.csv", params={"type": " "}).status_code == 422
    assert api.get("/invoices/data.csv", params={"type": ""}).status_code == 422


def test_an_invoice_with_no_readable_line_still_gets_one_row(db, ledger):
    """A credit note parsed on its labels, or a product invoice whose table
    could not be read: seen in the file, not silently dropped."""
    order = ledger["real"]
    row = Invoice(
        order_pk=order.id, order_id=order.order_id, kind="credit_note", invoice_type="Credit Note",
        invoice_number="CN1", invoice_date=datetime(2024, 10, 25), order_date=datetime(2024, 10, 20),
        original_invoice="FASUUT2500113513", reason="Return", grand_total=50999.0, lines=[],
        buyer_name="Transparent Deals", seller_name="Konde", path="x.pdf", size=1, sha256="z" * 64,
    )
    rows = invoice_data_csv.rows_for(row, order)
    assert len(rows) == 1
    got = dict(zip(invoice_data_csv.HEADERS, rows[0]))
    assert (got["Invoice Type"], got["Invoice Number"], got["Original Invoice Number"], got["Reason"]) == ("Credit Note", "CN1", "FASUUT2500113513", "Return")
    assert (got["Invoice Date"], got["Order Date"], got["Total Amount"]) == ("25-10-2024", "20-10-2024", "50999.00")
    assert (got["IMEI"], got["Invoice Product"], got["Ledger Product"], got["Qty"]) == ("", "", order.model, "")
    assert invoice_data_csv.row_count([(row, order)]) == 1


def test_the_order_date_fallback_is_indias_day_like_the_ledger_export(db, ledger):
    """No readable order date on the invoice: the ledger's placed_at stands in.
    It is an instant in UTC — 23:30 on the 1st is 05:00 on the 2nd in India —
    so it is turned into the same day the order-units export writes."""
    order = ledger["bare"]
    order.placed_at = datetime(2026, 10, 1, 23, 30)
    db.commit()
    row = Invoice(
        order_pk=order.id, order_id=order.order_id, kind="product", invoice_type="Product", invoice_number="FA1",
        invoice_date=datetime(2026, 10, 2), order_date=None, lines=[], path="x.pdf", size=1, sha256="y" * 64,
    )

    def order_date() -> str:
        return dict(zip(invoice_data_csv.HEADERS, invoice_data_csv.rows_for(row, order)[0]))["Order Date"]

    assert order_date() == "02-10-2026" == orders_csv.fmt_date(order.placed_at)
    # A day printed on the invoice is a calendar day: no clock to convert.
    row.order_date = datetime(2026, 10, 1)
    assert order_date() == "01-10-2026"
