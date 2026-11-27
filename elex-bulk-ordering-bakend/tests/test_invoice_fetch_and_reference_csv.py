"""27 Nov 2026: invoices fetched for a filter, and the CSV in the owner's layout.

* No invoice had ever been fetched: the read-back looked for an ``<a href>``
  invoice link the order page does not have ("Download Invoice", under the
  collapsed Price details, is a div calling Flipkart's invoice API, which
  answers 403 without its web client's ``X-User-Agent``).
* "Fetch invoices from Flipkart" downloads the invoices of every order the
  ledger filter matches — ignoring the invoice filters, which match only
  invoices already on file and so matched nothing before the first fetch.
* The invoice-data CSV is written in the owner's reference layout
  (``invoice_data_2026-10-28_1532.csv``): no BOM, CRLF, ISO dates, Qty with
  two decimals, IMEI after a tab, "Reason Of Issuance" / "Invoice Product
  Name" / "BoB Product Name", the reference's type names, the billed business
  in capitals as buyer, and the phone's title on every row.

Every value below is made up.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invoices as invoices_api
from app.core import invoices
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Job, JobType, Order, OrderStatus, utcnow
from app.io import invoice_data_csv
from app.io.invoice_pdf import parse_invoice_text, reference_name

OD = "OD100000000000000001"


# --- the parser: the reference's names ---------------------------------------------
TRANSPORT = "\n".join(
    [
        "Tax invoice",
        "Billed From",
        "Flipkart India Private Limited",
        "Plot 1, Some Village, Gurugram, Haryana,",
        "GSTIN : 06AAAAA0000A1Z5",
        "Invoice Details\tNature of transaction : INTER",
        "Tax Invoice Number : NBAA000000000001\tNature Of Supply : Service",
        "Invoice Date : 26-11-2026",
        f"Order Number : {OD}",
        "Billed To\tShipped From\tShipped To",
        "Some Person\tSome Warehouse, Haryana -\tSome Person",
        "Particulars\tSAC\tQty\tGross\tTaxable\tIGST\tTotal",
        "Amount\tValue",
        "Delivery Charges\t996819\t1.0\t₹587.00\t₹559.05\t₹27.95\t₹587.00",
        "IGST 5.0 %",
        "Total\t1.0\t₹587.00\t₹559.05\t₹27.95\t₹587.00",
    ]
)


def test_flipkart_indias_delivery_invoice_is_gt_charges_with_its_seller():
    out = parse_invoice_text(TRANSPORT)

    assert out.invoice_type == "GT Charges"
    assert out.seller_name == "Flipkart India Private Limited"
    assert (out.invoice_number, out.order_id, out.grand_total) == ("NBAA000000000001", OD, 587.0)


@pytest.mark.parametrize(
    "title, name",
    [
        ("Protect Promise Fee", "Protect Promise"),
        ("Protect Promise", "Protect Promise"),
        ("Delivery Charges", "GT Charges"),
        ("Corporate Card Fee", "Corporate Card Fee"),
        ("Offer Handling Fee", "Offer Handling Fee"),
    ],
)
def test_fee_titles_take_the_reference_names(title, name):
    assert reference_name(title) == name


# --- the CSV: the reference layout ------------------------------------------------------
def _order(**over):
    base = dict(
        order_id=OD,
        model="Some Phone 5G (Blue, 64 GB)",
        placed_at=None,
        invoice_buyer_name="Some Business Private Limited",
        invoice_buyer_gstin="09AAAAA0000A1Z9",
        gstin=None,
        invoice_product_title="Some Phone 5G (Blue, 64 GB)",
    )
    return NS(**{**base, **over})


def _invoice(kind, invoice_type, number, lines, **over):
    base = dict(
        kind=kind,
        invoice_type=invoice_type,
        invoice_number=number,
        order_id=OD,
        invoice_date=date(2026, 10, 26),
        order_date=date(2026, 10, 15),
        original_invoice=None,
        reason=None,
        buyer_name="Some Person",
        buyer_gstin="09AAAAA0000A1Z9",
        seller_name="some retail private limited",
        seller_gstin="09BBBBB0000B1Z1",
        grand_total=None,
        lines=lines,
    )
    return NS(**{**base, **over})


PRODUCT_LINE = {
    "title": "Some Phone 5G (Blue, 64 GB)", "fsn": "MOBAAAAAAAAAAAAA", "hsn_sac": "85171300",
    "imei": "860000000000001", "qty": 1, "taxable": 12849.16, "igst": 2312.86, "sgst": None, "cgst": None, "total": 15162.0,
}


def _csv(pairs) -> str:
    return "".join(invoice_data_csv.stream(pairs))


def test_the_file_is_the_reference_layout_byte_for_byte():
    text = _csv([(_invoice("product", "Product", "LIAA000000000001", [PRODUCT_LINE]), _order())])

    assert text == (
        "Invoice Type,Invoice Number,Order ID,Invoice Date,Order Date,Original Invoice Number,Reason Of Issuance,"
        "IMEI,Invoice Product Name,BoB Product Name,Qty,FSN,HSN,Taxable Amount,IGST,SGST,CGST,Total Amount,"
        "Buyer Name,Buyer GST,Seller Name,Seller GST\r\n"
        "Product,LIAA000000000001,OD100000000000000001,2026-10-26,2026-10-15,,,\t860000000000001,"
        '"Some Phone 5G (Blue, 64 GB)","Some Phone 5G (Blue, 64 GB)",1.00,MOBAAAAAAAAAAAAA,85171300,'
        "12849.16,2312.86,0.00,0.00,15162.00,SOME BUSINESS PRIVATE LIMITED,09AAAAA0000A1Z9,"
        "some retail private limited,09BBBBB0000B1Z1\r\n"
    )


def test_igst_is_the_whole_gst_and_the_split_is_zero_when_there_is_none():
    """As the reference writes it: an intra-state line's IGST column is SGST +
    CGST; an inter-state line's SGST and CGST are 0.00, not blank."""
    intra = {**PRODUCT_LINE, "igst": None, "sgst": 9.84, "cgst": 9.84, "taxable": 109.32, "total": 129.0}
    rows = list(csv.reader(io.StringIO(_csv([
        (_invoice("product", "Product", "LIAA1", [intra]), _order()),
        (_invoice("product", "Product", "LIAA2", [PRODUCT_LINE]), _order()),
    ]))))
    got = [dict(zip(rows[0], row)) for row in rows[1:]]

    assert [(row["IGST"], row["SGST"], row["CGST"]) for row in got] == [
        ("19.68", "9.84", "9.84"),
        ("2312.86", "0.00", "0.00"),
    ]


def test_a_fee_invoice_of_two_fees_is_a_row_per_fee_naming_the_phone():
    fees = [
        {"title": "Offer Handling Fee", "hsn_sac": "998599", "qty": 1, "taxable": 109.32, "igst": 19.68, "total": 129.0},
        {"title": "Corporate Card Fee", "hsn_sac": "998599", "qty": 1, "taxable": 137.29, "igst": 24.71, "total": 162.0},
    ]
    rows = list(csv.reader(io.StringIO(_csv([(_invoice("fee", "Corporate Card Fee + Offer Handling Fee", "MXAA1", fees), _order())]))))
    got = [dict(zip(rows[0], row)) for row in rows[1:]]

    assert [row["Invoice Type"] for row in got] == ["Offer Handling Fee", "Corporate Card Fee"]
    assert {row["Invoice Product Name"] for row in got} == {"Some Phone 5G (Blue, 64 GB)"}
    assert {row["IMEI"] for row in got} == {""}


def test_a_transport_invoice_takes_the_orders_billed_business_and_gstin():
    line = {"title": "Delivery Charges", "hsn_sac": "996819", "qty": 1, "taxable": 559.05, "igst": 27.95, "total": 587.0}
    invoice = _invoice("fee", "GT Charges", "NBAA1", [line], buyer_name=None, buyer_gstin=None)
    rows = list(csv.reader(io.StringIO(_csv([(invoice, _order())]))))
    got = dict(zip(rows[0], rows[1]))

    assert (got["Invoice Type"], got["Buyer Name"], got["Buyer GST"]) == (
        "GT Charges", "SOME BUSINESS PRIVATE LIMITED", "09AAAAA0000A1Z9",
    )


def test_a_long_all_digit_number_keeps_its_digits_and_a_formula_stays_text():
    invoice = _invoice("product", "Product", "123456789012345", [PRODUCT_LINE], seller_name="=cmd()")
    rows = list(csv.reader(io.StringIO(_csv([(invoice, _order())]))))
    got = dict(zip(rows[0], rows[1]))

    assert got["Invoice Number"] == "\t123456789012345"
    assert got["Seller Name"] == " =cmd()"


# --- the fetch ------------------------------------------------------------------------
class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self.ok, self._body = status, 200 <= status < 300, body

    async def body(self) -> bytes:
        return self._body


class _Requests:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    async def get(self, url, timeout=None, headers=None):  # noqa: ARG002
        self.asked.append((url, headers))
        return _Response(*self.answers[url])


class _Page:
    async def evaluate(self, _script):
        return "Mozilla/5.0 Test"


class _Context:
    def __init__(self, answers):
        self.request = _Requests(answers)
        self.pages = [_Page()]


@pytest.fixture(autouse=True)
def _fresh_notes():
    invoices._last_asked.clear()
    yield
    invoices._last_asked.clear()


def test_the_api_is_asked_with_flipkarts_client_header_and_a_refusal_is_reported():
    url = invoices.invoice_api_url(OD)
    assert url == f"https://2.rome.api.flipkart.com/api/1/self-serve/invoice/download/{OD}"
    context = _Context({url: (403, b"{}")})
    problems: dict[str, str] = {}

    got = asyncio.run(invoices.fetch_missing(context, 1, [NS(order_id=OD, invoice_url=url)], problems=problems))

    assert got == {}
    assert problems == {OD: "Flipkart answered 403"}
    (_url, headers), = context.request.asked
    assert headers["X-User-Agent"] == f"Mozilla/5.0 Test {invoices.FK_CLIENT_SUFFIX}"


def test_force_asks_again_an_order_asked_within_the_day():
    url = invoices.invoice_api_url(OD)
    context = _Context({url: (200, b"%PDF-1.4 made up")})
    seen = [NS(order_id=OD, invoice_url=url)]

    assert asyncio.run(invoices.fetch_missing(context, 1, seen)) == {OD: [b"%PDF-1.4 made up"]}
    assert asyncio.run(invoices.fetch_missing(context, 1, seen)) == {}  # the timer: once a day
    assert asyncio.run(invoices.fetch_missing(context, 1, seen, force=True)) == {OD: [b"%PDF-1.4 made up"]}


def test_an_order_is_asked_on_the_api_host_that_serves_it():
    """Seen live on 27 Nov 2026: Flipkart's own page asks host 1 for an OD3…
    order and host 2 for an OD4… one; the other host answers 406."""
    assert invoices.invoice_api_url("OD339240673109268100").startswith("https://1.rome.api.flipkart.com/")
    assert invoices.invoice_api_url("OD439237155916543100").startswith("https://2.rome.api.flipkart.com/")


def test_a_406_is_asked_again_on_the_other_api_host():
    od = "OD539240673109268100"  # a digit the rule does not know: host 2 first
    first = invoices.invoice_api_url(od)
    other = invoices.invoice_api_url(od, host="1")
    context = _Context({first: (406, b""), other: (200, b"%PDF-1.4 made up")})
    problems: dict[str, str] = {}

    got = asyncio.run(invoices.fetch_missing(context, 1, [NS(order_id=od, invoice_url=first)], problems=problems))

    assert got == {od: [b"%PDF-1.4 made up"]}
    assert problems == {}
    assert [url for url, _headers in context.request.asked] == [first, other]


# --- POST /invoices/fetch ---------------------------------------------------------------
@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(invoices_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_fetch_starts_an_invoices_only_read_back_over_the_matching_orders_ignoring_invoice_filters(db, api, monkeypatch):
    spawned: list[int] = []
    monkeypatch.setattr(invoices_api, "_spawn", lambda factory, job_id: spawned.append(job_id))
    account = Account(email="fetch@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    placed = (utcnow() - timedelta(days=2)).replace(tzinfo=None)
    for order_id in ("OD100000000000000002", "OD100000000000000001", None):
        db.add(Order(account_id=account.id, status=OrderStatus.placed, model="Some Phone", qty=1,
                     order_id=order_id, placed_at=placed))
    db.commit()

    # No invoice is on file, so an invoice-month filter alone would match nothing.
    response = api.post("/invoices/fetch", params={"invoice_month": "2026-10"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["orders"], body["accounts"]) == (2, 1)
    job = db.get(Job, body["job"]["id"])
    assert job.type == JobType.sync
    assert job.payload["invoices_only"] is True
    assert job.payload["order_ids"] == ["OD100000000000000001", "OD100000000000000002"]
    assert spawned == [job.id]


def test_a_timers_read_back_does_not_block_a_fetch_but_a_running_fetch_does(db, api, monkeypatch):
    """The timer starts a read-back every few minutes (seen live: #1292–#1294
    five minutes apart), so refusing a fetch beside one would refuse nearly always."""
    from app.db.models import JobStatus

    monkeypatch.setattr(invoices_api, "_spawn", lambda factory, job_id: None)
    account = Account(email="busy@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(Order(account_id=account.id, status=OrderStatus.placed, model="Some Phone", qty=1, order_id=OD,
                 placed_at=(utcnow() - timedelta(days=1)).replace(tzinfo=None)))
    db.add(Job(type=JobType.sync, status=JobStatus.running, payload={"started_by": "schedule"}))
    db.commit()

    first = api.post("/invoices/fetch")
    assert first.status_code == 201, first.text
    second = api.post("/invoices/fetch")
    assert second.status_code == 409
    assert f"#{first.json()['job']['id']}" in second.json()["detail"]


def test_fetch_with_no_matching_order_is_a_422(db, api, monkeypatch):
    monkeypatch.setattr(invoices_api, "_spawn", lambda factory, job_id: None)

    assert api.post("/invoices/fetch").status_code == 422
