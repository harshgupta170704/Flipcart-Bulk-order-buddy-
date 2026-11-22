"""The invoice reader's limits (audit F-27, 26 Oct 2026): a PDF of more than
``MAX_PAGES`` pages is refused before a page is read — counting no further
than one page past the cap, however long the file — and one still being read
after ``MAX_READ_SECONDS`` stops before its next page. Either is kept the way
an unreadable PDF is kept: on file, kind "unknown", the reason in
``parse_error``; ``parse_invoice`` never raises.

The many-page PDFs are written here by hand (a page tree of N pages, each
printing "Page n" in Helvetica): no PDF-writing library is installed, and a
few dozen lines of PDF need none.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pdfminer import pdfpage

from app.api import invoices as invoices_api
from app.core import invoices
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Invoice, Order, OrderStatus
from app.io import invoice_pdf, invoice_store
from app.io.invoice_pdf import PARSER_VERSION, parse_invoice
from app.io.invoice_store import InvoiceStore

ORDER_ID = "OD9876543210"


def many_page_pdf(pages: int, *, declared: int | None = None) -> bytes:
    """A valid PDF of ``pages`` pages, page n printing "Page n". ``declared``
    is the page count its page tree states, when that should be a lie."""
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids: list[int] = []
    for n in range(1, pages + 1):
        page_id, content_id = 2 + 2 * n, 3 + 2 * n
        kids.append(page_id)
        text = f"BT /F1 12 Tf 72 720 Td (Page {n}) Tj ET".encode()
        objects[content_id] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(text), text)
        objects[page_id] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_id
        )
    count = pages if declared is None else declared
    objects[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), count)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = out.tell()
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, objects[number]))
    xref_at = out.tell()
    size = max(objects) + 1
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
    for number in range(1, size):
        out.write(b"%010d 00000 n \n" % offsets[number])
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_at))
    return out.getvalue()


@pytest.fixture
def pages_read(monkeypatch) -> list[object]:
    """Every page whose text the reader extracted."""
    seen: list[object] = []
    real = invoice_pdf._page_lines

    def spy(page):
        seen.append(page)
        return real(page)

    monkeypatch.setattr(invoice_pdf, "_page_lines", spy)
    return seen


class _Clock:
    """A clock that moves ``step`` seconds every time it is read."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def test_the_limits_are_twenty_pages_and_twenty_seconds():
    assert invoice_pdf.MAX_PAGES == 20
    assert invoice_pdf.MAX_READ_SECONDS == 20.0


# --- within the limits -------------------------------------------------------------------
def test_a_pdf_within_the_limits_is_read_page_by_page(pages_read):
    text = invoice_pdf.extract_text(many_page_pdf(3))

    assert text.splitlines() == ["Page 1", "Page 2", "Page 3"]
    assert len(pages_read) == 3
    # Read, and then simply not an invoice: the layout error, not a limit's.
    assert parse_invoice(many_page_pdf(3)).error == "no invoice, fee or credit note layout recognised"


def test_exactly_the_cap_is_still_read(monkeypatch, pages_read):
    monkeypatch.setattr(invoice_pdf, "MAX_PAGES", 3)
    assert invoice_pdf.extract_text(many_page_pdf(3)).splitlines() == ["Page 1", "Page 2", "Page 3"]


# --- the page cap --------------------------------------------------------------------------
def test_a_pdf_past_the_page_cap_is_refused_before_a_page_is_read(pages_read):
    out = parse_invoice(many_page_pdf(25))

    assert out.error == "too many pages to read: 25"
    assert (out.kind, out.invoice_type, out.lines, out.order_id) == ("unknown", None, [], None)
    assert pages_read == []


def test_the_page_cap_is_a_constant_a_test_can_lower(monkeypatch, pages_read):
    monkeypatch.setattr(invoice_pdf, "MAX_PAGES", 2)

    assert parse_invoice(many_page_pdf(3)).error == "too many pages to read: 3"
    assert pages_read == []


def test_refusing_a_huge_page_tree_walks_no_more_than_one_page_past_the_cap(monkeypatch, pages_read):
    """Three thousand pages: listing them all is the cost the cap spares —
    including pdfplumber's close(), which would list every page first."""
    made: list[int] = []
    real_init = pdfpage.PDFPage.__init__

    def counting_init(self, *args, **kwargs):
        made.append(1)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(pdfpage.PDFPage, "__init__", counting_init)

    out = parse_invoice(many_page_pdf(3000))

    assert out.error == "too many pages to read: 3000"
    assert len(made) == invoice_pdf.MAX_PAGES + 1
    assert pages_read == []


def test_a_page_count_the_file_misstates_is_caught_by_the_walk(pages_read):
    out = parse_invoice(many_page_pdf(25, declared=1))

    assert out.error == "too many pages to read: more than 20"
    assert pages_read == []


# --- the time limit -----------------------------------------------------------------------
def test_the_time_limit_stops_reading_before_the_next_page(monkeypatch, pages_read):
    """Each look at the clock moves it 15 s: the first page starts 15 s in
    (inside the 20), the second would start 30 s in — so it never does."""
    monkeypatch.setattr(invoice_pdf, "_clock", _Clock(step=15.0))

    out = parse_invoice(many_page_pdf(3))

    assert out.error == "reading took too long: over 20 seconds"
    assert (out.kind, out.invoice_type, out.lines) == ("unknown", None, [])
    assert len(pages_read) == 1


def test_a_file_slow_to_open_is_stopped_before_its_first_page(monkeypatch, pages_read):
    monkeypatch.setattr(invoice_pdf, "_clock", _Clock(step=45.0))

    assert parse_invoice(many_page_pdf(2)).error == "reading took too long: over 20 seconds"
    assert pages_read == []


def test_the_time_limit_is_a_constant_a_test_can_lower(monkeypatch, pages_read):
    monkeypatch.setattr(invoice_pdf, "MAX_READ_SECONDS", 0.5)
    monkeypatch.setattr(invoice_pdf, "_clock", _Clock(step=0.4))

    assert parse_invoice(many_page_pdf(3)).error == "reading took too long: over 0.5 seconds"
    assert len(pages_read) == 1


def test_a_clock_that_stays_within_the_limit_reads_every_page(monkeypatch, pages_read):
    monkeypatch.setattr(invoice_pdf, "_clock", _Clock(step=4.0))  # 4, 8, 12 s before pages 1–3

    assert invoice_pdf.extract_text(many_page_pdf(3)).splitlines() == ["Page 1", "Page 2", "Page 3"]


def test_the_core_belt_passes_a_limit_through_as_the_reason(monkeypatch):
    monkeypatch.setattr(invoice_pdf, "MAX_PAGES", 1)
    assert invoices.parse_pdf(many_page_pdf(2)).error == "too many pages to read: 2"


# --- kept the way an unreadable PDF is kept --------------------------------------------------
@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    return tmp_path / "invoices"


@pytest.fixture
def order(db) -> Order:
    account = Account(email="limits@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    row = Order(account_id=account.id, order_id=ORDER_ID, status=OrderStatus.placed, placed_at=datetime(2026, 10, 1, 4, 0))
    db.add(row)
    db.commit()
    return row


def test_an_uploaded_pdf_past_the_page_cap_is_kept_with_the_reason(db, order, tmp_store):
    application = FastAPI()
    application.include_router(invoices_api.router)
    api = TestClient(application, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})
    data = many_page_pdf(25)

    out = api.post("/invoices/upload", files=[("files", (f"{ORDER_ID}.pdf", data, "application/pdf"))]).json()

    assert out["stored"] == 1
    assert [(f["status"], f["order_id"]) for f in out["files"]] == [("stored", ORDER_ID)]
    # Stored, but not read — and the answer says why (NFR-009).
    assert [(f["reading"], f["reason"]) for f in out["files"]] == [("not_read", "too many pages to read: 25")]
    row = db.query(Invoice).one()
    assert row.parse_error == "too many pages to read: 25"
    assert (row.kind, row.invoice_type, row.lines, row.parser_version) == ("unknown", None, [], PARSER_VERSION)
    assert InvoiceStore.from_settings().path(row.path).read_bytes() == data


def test_a_read_back_invoice_past_the_time_limit_is_kept_with_the_reason(db, order, tmp_store, monkeypatch):
    monkeypatch.setattr(invoice_pdf, "_clock", _Clock(step=15.0))

    row = invoices.attach(db, InvoiceStore.from_settings(), order, many_page_pdf(3), source="sync")
    db.commit()

    assert row is not None
    assert (row.parse_error, row.kind, row.invoice_type) == ("reading took too long: over 20 seconds", "unknown", None)
    assert InvoiceStore.from_settings().exists(row.path)


def test_the_backfill_marks_a_pdf_past_a_limit_read_and_does_not_read_it_every_start(db, order, tmp_store):
    """A row whose PDF is too long is read once by this parser, the reason on
    the row, and left alone at the next start — like an unreadable file."""
    data = many_page_pdf(25)
    digest = hashlib.sha256(data).hexdigest()
    path = InvoiceStore.from_settings().put(ORDER_ID, data, digest)
    db.add(Invoice(order_pk=order.id, order_id=ORDER_ID, path=path, size=len(data), sha256=digest, source="upload"))
    db.commit()

    first = invoices.backfill_unparsed(limit=10)

    assert (first.parsed, first.missing, first.unsaved) == (1, 0, 0)
    db.expire_all()
    row = db.query(Invoice).one()
    assert (row.parse_error, row.parser_version) == ("too many pages to read: 25", PARSER_VERSION)
    assert invoices.backfill_unparsed(limit=10).last_id is None


def test_a_pdf_that_ran_out_of_time_is_read_once_more_at_the_next_start(db, order, tmp_store, monkeypatch):
    """Running out of time says more about a busy machine than about the file:
    the first time out leaves the row unread, so the start-up backfill reads
    it again; a second time out is final, so it is not tried at every start."""
    monkeypatch.setattr(invoice_pdf, "_clock", _Clock(step=15.0))
    row = invoices.attach(db, InvoiceStore.from_settings(), order, many_page_pdf(3), source="sync")
    db.commit()
    assert row.parse_error.startswith(invoice_pdf.TOOK_TOO_LONG)
    assert (row.parsed_at, row.parser_version) == (None, None)  # waiting for one more read

    # The next start's backfill picks it up and runs out of time again: given up on.
    done = invoices.backfill_unparsed(limit=10)
    assert done.parsed == 1
    db.expire_all()
    again = db.get(Invoice, row.id)
    assert again.parse_error.startswith(invoice_pdf.TOOK_TOO_LONG)
    assert again.parser_version == PARSER_VERSION and again.parsed_at is not None
    assert invoices.backfill_unparsed(limit=10).last_id is None  # not tried at every start


def test_a_re_read_that_runs_out_of_time_keeps_the_earlier_reading(db, order, tmp_store, monkeypatch):
    """NFR-009: a newer parser running out of time on a PDF an older one read
    must not blank the row. The earlier reading stays, the time-out is noted;
    the next start tries once more, and a second time out is final — still
    with the earlier reading."""
    data = many_page_pdf(3)
    digest = hashlib.sha256(data).hexdigest()
    path = InvoiceStore.from_settings().put(ORDER_ID, data, digest)
    reading = {
        "kind": "product", "invoice_type": "Product", "invoice_number": "FA1", "grand_total": 15162.0,
        "seller_gstin": "29AAHCH2846P2ZU", "buyer_gstin": "09BUAPA6939F1ZP",
        "lines": [{"title": "vivo T5 Lite", "qty": 1, "taxable": 12849.15, "igst": 2312.85, "total": 15162.0}],
    }
    row = Invoice(order_pk=order.id, order_id=ORDER_ID, path=path, size=len(data), sha256=digest, source="upload",
                  parsed_at=datetime(2026, 10, 1), parser_version=PARSER_VERSION - 1, **reading)
    db.add(row)
    db.commit()
    monkeypatch.setattr(invoice_pdf, "_clock", _Clock(step=15.0))

    first = invoices.backfill_unparsed(limit=10)

    assert first.parsed == 1
    db.expire_all()
    kept = db.get(Invoice, row.id)
    assert {key: getattr(kept, key) for key in reading} == reading
    assert kept.parse_error == f"reading took too long: over 20 seconds; {invoices.EARLIER_READING_KEPT}"
    assert kept.parser_version == PARSER_VERSION - 1  # read once more at the next start

    second = invoices.backfill_unparsed(limit=10)  # out of time again: final

    assert second.parsed == 1
    db.expire_all()
    kept = db.get(Invoice, row.id)
    assert {key: getattr(kept, key) for key in reading} == reading
    assert kept.parse_error.startswith(invoice_pdf.TOOK_TOO_LONG)
    assert kept.parser_version == PARSER_VERSION and kept.parsed_at > datetime(2026, 10, 1)
    assert invoices.backfill_unparsed(limit=10).last_id is None  # not tried at every start


def test_a_pdf_read_in_time_on_its_second_go_is_read_normally(db, order, tmp_store, monkeypatch):
    monkeypatch.setattr(invoice_pdf, "_clock", _Clock(step=15.0))
    row = invoices.attach(db, InvoiceStore.from_settings(), order, many_page_pdf(3), source="sync")
    db.commit()

    monkeypatch.setattr(invoice_pdf, "_clock", _Clock(step=0.1))  # a quiet machine at the next start
    invoices.backfill_unparsed(limit=10)

    db.expire_all()
    read = db.get(Invoice, row.id)
    assert read.parse_error is None or not read.parse_error.startswith(invoice_pdf.TOOK_TOO_LONG)
    assert read.parser_version == PARSER_VERSION
