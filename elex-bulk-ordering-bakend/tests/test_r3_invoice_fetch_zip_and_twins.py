"""11 Nov 2026: why no invoice had ever been put on file by itself.

* Flipkart answers an order's invoice link with a zip of all its invoices.
  The read-back refused it as "not a PDF", so the automatic download saved
  nothing.
* An order with any invoice on file was never asked again, so a fee invoice
  filed first kept the product's tax invoice out for good; and only the first
  of an order's links was ever asked.
* The same invoice downloaded twice differs by a few bytes, was filed twice,
  and doubled its lines in the invoice-data CSV.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

from app.core import invoices
from app.core.order_filter import OrderFilter
from app.db.models import Account, AccountStatus, Invoice, Order, OrderStatus, utcnow
from app.io import invoice_data_csv
from app.io.invoice_pdf import parse_invoice_text
from app.io.invoice_store import InvoiceStore

FIXTURES = Path(__file__).parent / "fixtures" / "invoices" / "OD338918803190451100"
OD = "OD338918803190451100"
LINK = f"https://www.flipkart.com/x/invoice?order={OD}"


def _pdf(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self.ok, self._body = status, 200 <= status < 300, body

    async def body(self) -> bytes:
        return self._body


class _Requests:
    def __init__(self, answers: dict[str, tuple[int, bytes]]) -> None:
        self.answers, self.asked = answers, []

    async def get(self, url: str, timeout=None):  # noqa: ARG002 — the client's signature
        self.asked.append(url)
        return _Response(*self.answers[url])


class _Context:
    def __init__(self, answers: dict[str, tuple[int, bytes]]) -> None:
        self.request = _Requests(answers)


class _Seen:
    def __init__(self, order_id: str, invoice_url: str) -> None:
        self.order_id, self.invoice_url = order_id, invoice_url


@pytest.fixture(autouse=True)
def _stores(monkeypatch, tmp_path):
    monkeypatch.setenv("ORDERING_INVOICE_DIR", str(tmp_path / "invoices"))
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "invoice_dir", str(tmp_path / "invoices"))
    invoices._last_asked.clear()
    yield
    invoices._last_asked.clear()


@pytest.fixture
def order(db) -> Order:
    account = Account(email="buyer@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    row = Order(account_id=account.id, order_id=OD, status=OrderStatus.placed, amount=15339.0, placed_at=utcnow())
    db.add(row)
    db.commit()
    return row


def _fetch(context, order: Order, *links: str):
    return asyncio.run(invoices.fetch_missing(context, order.account_id, [_Seen(OD, link) for link in links]))


def test_a_zip_of_the_orders_invoices_is_unpacked_and_every_pdf_filed(db, order):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in ("file_1.pdf", "file_2.pdf", "file_3.pdf"):
            archive.writestr(name, _pdf(name))
        archive.writestr("__MACOSX/._file_1.pdf", b"resource fork")
        archive.writestr("readme.txt", b"not an invoice")

    fetched = _fetch(_Context({LINK: (200, buffer.getvalue())}), order, LINK)

    assert [len(pdfs) for pdfs in fetched.values()] == [3]
    assert invoices.record(order.account_id, fetched) == 3
    db.expire_all()
    kinds = sorted(row.kind for row in db.query(Invoice).all())
    assert kinds == ["fee", "fee", "product"]


def test_a_zip_that_is_not_one_costs_nothing_but_itself(db, order):
    fetched = _fetch(_Context({LINK: (200, b"PK\x03\x04 not really a zip")}), order, LINK)
    assert fetched == {}


def test_a_sign_in_page_is_never_filed_as_an_invoice(db, order, caplog):
    fetched = _fetch(_Context({LINK: (200, b"<html>Login</html>")}), order, LINK)
    assert fetched == {}
    assert "the account may be signed out" in caplog.text


def test_every_link_of_an_order_is_asked(db, order):
    first, second = LINK + "&unit=1", LINK + "&unit=2"
    context = _Context({first: (200, _pdf("file_1.pdf")), second: (200, _pdf("file_3.pdf"))})

    fetched = _fetch(context, order, first, second, first)

    assert context.request.asked == [first, second]
    assert invoices.record(order.account_id, fetched) == 2


def test_a_fee_invoice_on_file_does_not_keep_the_product_invoice_out(db, order):
    invoices.record(order.account_id, {OD: _pdf("file_1.pdf")})  # the fee invoice, a day ago
    for row in db.query(Invoice).all():
        row.fetched_at = utcnow() - timedelta(days=2)
    db.commit()

    context = _Context({LINK: (200, _pdf("file_3.pdf"))})
    fetched = _fetch(context, order, LINK)

    assert context.request.asked == [LINK]
    assert invoices.record(order.account_id, fetched) == 1
    db.expire_all()
    assert sorted(row.kind for row in db.query(Invoice).all()) == ["fee", "product"]


def test_a_link_is_not_asked_twice_in_a_day(db, order):
    context = _Context({LINK: (200, _pdf("file_1.pdf"))})
    assert _fetch(context, order, LINK)
    assert _fetch(context, order, LINK) == {}  # the next read-back, an hour later
    assert context.request.asked == [LINK]


def test_an_order_with_its_product_invoice_is_left_alone_after_ten_days(db, order):
    invoices.record(order.account_id, {OD: [_pdf("file_1.pdf"), _pdf("file_3.pdf")]})
    for row in db.query(Invoice).all():
        row.fetched_at = utcnow() - timedelta(days=invoices.REFETCH_FOR_DAYS + 1)
    db.commit()

    context = _Context({LINK: (200, _pdf("file_2.pdf"))})
    assert _fetch(context, order, LINK) == {}
    assert context.request.asked == []


def test_within_ten_days_a_later_invoice_is_still_picked_up(db, order):
    """The transport invoice of the 26 Oct samples is dated four days after the product's."""
    invoices.record(order.account_id, {OD: [_pdf("file_1.pdf"), _pdf("file_3.pdf")]})
    for row in db.query(Invoice).all():
        row.fetched_at = utcnow() - timedelta(days=3)
    db.commit()

    fetched = _fetch(_Context({LINK: (200, _pdf("file_2.pdf"))}), order, LINK)

    assert invoices.record(order.account_id, fetched) == 1
    db.expire_all()
    assert db.query(Invoice).count() == 3


def test_the_same_invoice_in_other_bytes_is_one_invoice(db, order):
    store = InvoiceStore.from_settings()
    first = invoices.attach(db, store, order, _pdf("file_3.pdf"), source="sync")
    again = invoices.attach(db, store, order, _pdf("file_3.pdf") + b"\n%downloaded again\n", source="upload")
    db.commit()

    assert first is not None and again is None
    assert db.query(Invoice).count() == 1
    selection = invoices.data_selection(db, OrderFilter(), ["Product"])
    text = "".join(invoice_data_csv.stream(invoices.data_pairs(db, selection)))
    assert len(text.strip().splitlines()) == 2  # the header and the one line, not two


def test_two_imeis_in_one_bracket_are_both_kept():
    from app.io.invoice_pdf import extract_text

    text = extract_text(_pdf("file_3.pdf"))
    assert "864414083556330 ]" in text
    parsed = parse_invoice_text(text.replace("864414083556330 ]", "864414083556330, 864414083556348 ]"))
    assert parsed.lines[0].imei == "864414083556330, 864414083556348"
    # …and the invoice as printed still reads its one.
    assert parse_invoice_text(text).lines[0].imei == "864414083556330"
