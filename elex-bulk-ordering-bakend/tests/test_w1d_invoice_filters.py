"""Finding orders by GST and by invoice (26 Oct 2026): the ledger's billed
GST, invoice number, invoice GSTIN and invoice month filters.

The filter is the one query behind the page, its count, both exports, Extract
Invoices Data and Compile Invoices, so the new fields are checked in the core
filter first and then through each of those doors — including the compile
run, which rebuilds the filter from the request's raw parameters and so must
be handed every one of them, and the saved view, which must keep them.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import inspect
import io
import zipfile
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invoices as invoices_api
from app.api import jobs as jobs_api
from app.api import orders as orders_api
from app.api.orders import build_filter
from app.core import archives, invoices, order_filter
from app.core.order_filter import (
    GSTIN_MAX_LENGTH,
    INVOICE_NUMBER_MAX_LENGTH,
    OrderFilter,
    month_range,
    prefix_pattern,
)
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Invoice, Order, OrderStatus, utcnow
from app.io import invoice_store
from app.io.invoice_store import InvoiceStore
from app.jobs.compile_tasks import run_compile_job

#: Made-up GSTINs in the real shape (state code, PAN, entity, Z, check): two
#: of a business's own registrations, a seller, and a fee desk.
OURS = "09AAAAA1111A1Z1"
OTHER_ENTITY = "27BBBBB2222B1Z2"
SELLER = "09CCCCC3333C1Z3"
FLIPKART = "29DDDDD4444D1Z4"


@pytest.fixture(autouse=True)
def stores(tmp_path, monkeypatch):
    """Every test writes into its own directories, never the configured ones."""
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    monkeypatch.setattr(archives, "archive_root", lambda: tmp_path / "archives")
    return tmp_path


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    application.include_router(invoices_api.router)
    application.include_router(invoices_api.public_router)
    application.include_router(jobs_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def spawned(monkeypatch) -> list[int]:
    """The compile job ids handed to the engine, not run: the test runs the body."""
    ids: list[int] = []
    monkeypatch.setattr(invoices_api, "_spawn", lambda factory, job_id: ids.append(job_id))
    return ids


@pytest.fixture
def account(db) -> Account:
    row = Account(email="gst@test.com", status=AccountStatus.new)
    db.add(row)
    db.commit()
    return row


def _order(db, account: Account, order_id: str, **fields) -> Order:
    values = {
        "account_id": account.id,
        "status": OrderStatus.placed,
        "model": "vivo T5x 5G",
        "qty": 1,
        "order_id": order_id,
        "placed_at": utcnow().replace(tzinfo=None) - timedelta(days=3),
        **fields,
    }
    order = Order(**values)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _invoice(
    db,
    order: Order,
    number: str,
    *,
    kind: str = "product",
    seller: str | None = None,
    buyer: str | None = None,
    dated: datetime | None = None,
) -> Invoice:
    """An invoice row as the reader leaves it: the filters read rows, not files."""
    row = Invoice(
        order_pk=order.id,
        order_id=order.order_id,
        kind=kind,
        invoice_type="Product" if kind == "product" else "Secure Packaging Fee",
        invoice_number=number,
        seller_gstin=seller,
        buyer_gstin=buyer,
        invoice_date=dated,
        grand_total=100.0,
        path=f"{order.order_id}-{number}.pdf",
        size=1,
        sha256=hashlib.sha256(f"{order.id}-{number}".encode()).hexdigest(),
        source="upload",
    )
    db.add(row)
    db.commit()
    return row


def _bill(db, order: Order, gstin: str | None, *, position: int = 1) -> None:
    """The GST the order page showed on one unit, as the read-back stores it."""
    db.refresh(order)
    unit = next(u for u in order.units if u.position == position)
    unit.billed_gstin = gstin
    db.commit()


def _ids(db, flt: OrderFilter) -> list[str]:
    """The Flipkart ids of the matching rows, one per unit, in ledger order."""
    return [order.order_id for _unit, order, _email, _n in order_filter.rows(db, flt)]


# --- billed GST ---------------------------------------------------------------------
def test_billed_gst_matches_the_runs_gstin_or_the_pages_whole_or_by_its_start(db, account):
    chosen = _order(db, account, "OD1", gstin=OURS)  # the GST the run billed under
    seen = _order(db, account, "OD2")  # imported: only the order page says
    _bill(db, seen, OURS.lower() + " ")  # stored as read
    elsewhere = _order(db, account, "OD3", gstin=OTHER_ENTITY)
    _bill(db, elsewhere, OTHER_ENTITY)
    _order(db, account, "OD4")  # no GST anywhere

    assert sorted(_ids(db, OrderFilter(billed_gst=OURS))) == ["OD1", "OD2"]
    assert sorted(_ids(db, OrderFilter(billed_gst="09"))) == ["OD1", "OD2"]  # the state code
    assert _ids(db, OrderFilter(billed_gst="27BBBBB")) == ["OD3"]
    assert order_filter.count(db, OrderFilter(billed_gst=OURS)) == 2
    # Exact or by its start, never from the middle: the PAN alone is not a GSTIN's start.
    assert _ids(db, OrderFilter(billed_gst=OURS[2:])) == []
    assert _ids(db, OrderFilter(billed_gst=OURS + "X")) == []
    # Typed any old way, it is read as the GSTIN it looks like.
    assert build_filter(billed_gst=" 09aaaaa 1111a1z1 ").billed_gst == OURS
    assert build_filter(billed_gst="   ").billed_gst is None
    assert chosen.id and elsewhere.id


def test_billed_gst_takes_every_unit_of_an_order_one_unit_shows_it_on(db, account):
    """A unit the read-back has not reached yet is not left out of its order."""
    order = _order(db, account, "OD10", qty=2)
    _bill(db, order, OURS, position=1)  # unit 2 is still a placeholder

    flt = OrderFilter(billed_gst=OURS)
    assert _ids(db, flt) == ["OD10", "OD10"]
    assert order_filter.count(db, flt) == 2
    assert order_filter.order_pks(db, flt) == [order.id]


# --- invoice number, invoice GSTIN, invoice month -------------------------------------
@pytest.fixture
def invoiced(db, account) -> dict[str, Order]:
    """Three invoiced orders and one with nothing on file."""
    one = _order(db, account, "OD21")
    _invoice(db, one, "FAAAEU2600040612", seller=SELLER, buyer=OURS, dated=datetime(2026, 10, 1))
    _invoice(db, one, "BFF8825001967375", kind="fee", seller=FLIPKART, dated=datetime(2026, 10, 1))
    two = _order(db, account, "OD22")
    _invoice(db, two, "FAXYZ_77", seller="07EEEEE5555E1Z5", buyer=OTHER_ENTITY, dated=datetime(2026, 10, 31))
    three = _order(db, account, "OD23")
    _invoice(db, three, "FA100%", seller=SELLER, buyer=OURS, dated=datetime(2026, 11, 1))
    bare = _order(db, account, "OD24")
    return {"one": one, "two": two, "three": three, "bare": bare}


def test_invoice_number_matches_any_part_of_a_number_on_file(db, invoiced):
    assert _ids(db, OrderFilter(invoice_number="FAAAEU2600040612")) == ["OD21"]
    assert _ids(db, OrderFilter(invoice_number="2600040612")) == ["OD21"]  # the digits alone
    assert _ids(db, OrderFilter(invoice_number="bff88")) == ["OD21"]  # a fee invoice's, any case
    assert sorted(_ids(db, OrderFilter(invoice_number="FA"))) == ["OD21", "OD22", "OD23"]
    assert _ids(db, OrderFilter(invoice_number="NOPE")) == []


def test_the_invoice_text_filters_escape_like_wildcards(db, invoiced):
    """A search for "_" or "%" finds those characters, never every invoice
    (audit F-25's rule, kept for the new fields)."""
    assert _ids(db, OrderFilter(invoice_number="_")) == ["OD22"]
    assert _ids(db, OrderFilter(invoice_number="%")) == ["OD23"]
    assert _ids(db, OrderFilter(invoice_gstin="_")) == []
    assert _ids(db, OrderFilter(invoice_gstin="%")) == []
    # An order billed under a GST, so an unescaped "%" or "_" would find it.
    _bill(db, invoiced["one"], OURS)
    assert _ids(db, OrderFilter(billed_gst=OURS)) == ["OD21"]
    assert _ids(db, OrderFilter(billed_gst="%")) == []
    assert _ids(db, OrderFilter(billed_gst="_")) == []
    assert prefix_pattern("09_A%") == "09\\_A\\%%"


def test_invoice_gstin_matches_the_seller_or_the_buyer_on_any_invoice(db, invoiced):
    assert sorted(_ids(db, OrderFilter(invoice_gstin=OURS))) == ["OD21", "OD23"]  # bought by us
    assert sorted(_ids(db, OrderFilter(invoice_gstin=SELLER))) == ["OD21", "OD23"]  # sold by them
    assert _ids(db, OrderFilter(invoice_gstin=FLIPKART)) == ["OD21"]  # the fee invoice's seller
    assert _ids(db, OrderFilter(invoice_gstin="27")) == ["OD22"]  # by its start
    assert _ids(db, OrderFilter(invoice_gstin=OURS[2:])) == []  # not from the middle
    assert build_filter(invoice_gstin=" 29ddddd 4444d1z4").invoice_gstin == FLIPKART


def test_invoice_month_takes_the_invoices_dated_in_that_month(db, invoiced):
    # The first and the last day are in; the next month's first day is not.
    assert sorted(_ids(db, OrderFilter(invoice_month="2026-10"))) == ["OD21", "OD22"]
    assert _ids(db, OrderFilter(invoice_month="2026-11")) == ["OD23"]
    assert _ids(db, OrderFilter(invoice_month="2026-09")) == []
    assert month_range("2026-12") == (datetime(2026, 12, 1), datetime(2027, 1, 1))
    assert month_range(" 2026-02 ") == (datetime(2026, 2, 1), datetime(2026, 3, 1))
    for bad in ("2026-13", "2026-1", "2026/10", "Oct 2026", "0000-01", "", "2026-10-01"):
        with pytest.raises(ValueError):
            month_range(bad)
    # One built by hand with a bad month matches nothing, never everything.
    assert _ids(db, OrderFilter(invoice_month="garbage")) == []


def test_the_invoice_filters_hold_on_one_invoice_together(db, account):
    """"An invoice from this seller dated October": the seller's invoice is
    September's and October's is Flipkart's fee, so this order is not one."""
    order = _order(db, account, "OD30")
    _invoice(db, order, "FA30", seller=SELLER, buyer=OURS, dated=datetime(2026, 9, 28))
    _invoice(db, order, "BF30", kind="fee", seller=FLIPKART, dated=datetime(2026, 10, 2))

    assert _ids(db, OrderFilter(invoice_gstin=SELLER, invoice_month="2026-10")) == []
    assert _ids(db, OrderFilter(invoice_gstin=SELLER, invoice_month="2026-09")) == ["OD30"]
    assert _ids(db, OrderFilter(invoice_number="FA30", invoice_month="2026-10")) == []
    assert _ids(db, OrderFilter(invoice_number="BF30", invoice_gstin=FLIPKART, invoice_month="2026-10")) == ["OD30"]


def test_an_invoice_filter_takes_every_unit_of_the_order(db, account):
    """An invoice is per order, so each unit of a matching order is a row."""
    order = _order(db, account, "OD40", qty=3)
    _invoice(db, order, "FA40", seller=SELLER, buyer=OURS, dated=datetime(2026, 10, 5))
    _order(db, account, "OD41", qty=2)

    flt = OrderFilter(invoice_number="FA40")
    assert _ids(db, flt) == ["OD40"] * 3
    assert order_filter.count(db, flt) == 3
    assert order_filter.order_pks(db, flt) == [order.id]
    assert flt.is_empty() is False
    assert OrderFilter(billed_gst=OURS).is_empty() is False
    assert OrderFilter(invoice_month="2026-10").is_empty() is False
    assert OrderFilter(invoice_gstin=OURS).is_empty() is False


# --- through the API --------------------------------------------------------------------
def test_the_list_reads_the_new_parameters_and_refuses_what_it_cannot_apply(api, db, invoiced):
    _bill(db, invoiced["bare"], OURS)

    def total(**params) -> int:
        response = api.get("/orders", params=params)
        assert response.status_code == 200, response.text
        return response.json()["total"]

    assert total() == 4
    assert total(billed_gst="09aaaaa") == 1  # the bare order's page showed our GST
    assert total(invoice_number="2600040612") == 1
    assert total(invoice_gstin=OURS) == 2
    assert total(invoice_month="2026-10") == 2
    assert total(invoice_month="2026-10", invoice_gstin=OTHER_ENTITY) == 1
    # Blank is no filter, as the page sends an empty box.
    assert total(billed_gst="", invoice_number="", invoice_gstin="", invoice_month="") == 4

    for params in (
        {"invoice_month": "2026-13"},
        {"invoice_month": "October"},
        {"billed_gst": "9" * (GSTIN_MAX_LENGTH + 1)},
        {"invoice_gstin": "9" * (GSTIN_MAX_LENGTH + 1)},
        {"invoice_number": "F" * (INVOICE_NUMBER_MAX_LENGTH + 1)},
    ):
        assert api.get("/orders", params=params).status_code == 422, params
    assert api.get("/orders", params={"billed_gst": "9" * GSTIN_MAX_LENGTH}).status_code == 200


def test_a_saved_view_keeps_the_new_filters_and_refuses_what_a_load_would(api, invoiced):
    filters = {
        "billed_gst": "09AAAAA",
        "invoice_number": "FAAAEU",
        "invoice_gstin": SELLER,
        "invoice_month": "2026-10",
    }
    saved = api.put("/orders/view", json={"columns": ["order_id"], "filters": filters})
    assert saved.status_code == 200, saved.text

    restored = api.get("/orders/view").json()["filters"]
    assert restored == filters
    # What a view restores is what the list accepts, every load.
    assert api.get("/orders", params=restored).status_code == 200

    for bad in (
        {"invoice_month": "2026-1"},
        {"invoice_month": ["2026-10"]},
        {"billed_gst": ["09AAAAA"]},
        {"invoice_gstin": "9" * (GSTIN_MAX_LENGTH + 1)},
        {"invoice_number": "F" * (INVOICE_NUMBER_MAX_LENGTH + 1)},
    ):
        assert api.put("/orders/view", json={"columns": [], "filters": bad}).status_code == 422, bad


def test_the_exports_hold_the_rows_the_new_filters_count(api, invoiced):
    params = {"invoice_month": "2026-10"}
    listed = api.get("/orders", params=params).json()
    text = api.get("/orders/export.csv", params=params).text.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert sorted(row["Order ID"] for row in rows) == sorted(item["order_id"] for item in listed["items"])
    assert len(rows) == listed["total"] == 2
    assert api.get("/orders/export", params=params).status_code == 200


def test_extract_invoices_data_follows_the_new_filters(api, invoiced):
    types = api.get("/invoices/types", params={"invoice_gstin": OTHER_ENTITY})
    assert types.status_code == 200, types.text
    body = types.json()
    assert (body["units"], body["units_without_invoice"]) == (1, 0)
    assert [(t["type"], t["invoices"]) for t in body["types"]] == [("Product", 1)]

    data = api.get("/invoices/data.csv", params={"type": "Product", "invoice_month": "2026-10"})
    assert data.status_code == 200, data.text
    assert data.headers["X-Invoice-Rows"] == "2"  # OD21's and OD22's product invoices
    rows = list(csv.reader(io.StringIO(data.text.lstrip("﻿"))))[1:]
    assert sorted(row[1] for row in rows) == ["FAAAEU2600040612", "FAXYZ_77"]


def _pdf(tag: str) -> bytes:
    return b"%PDF-1.4\n%" + tag.encode("utf-8") + b"\n"


def test_compile_invoices_follows_the_new_filters_into_the_zip(api, db, account, spawned, stores):
    """The run rebuilds the filter from the request's raw parameters: a new
    field left out of that list would zip every order's PDFs."""
    store = InvoiceStore.from_settings()
    wanted = _order(db, account, "OD338918803190451100")
    other = _order(db, account, "OD338918803190451101")
    for order, number in ((wanted, "FAJAN1"), (other, "FAFEB2")):
        row = invoices.attach(db, store, order, _pdf(number), source="upload")
        row.invoice_number = number
        row.seller_gstin = SELLER
        row.invoice_date = datetime(2026, 10, 9)
    db.commit()

    response = api.post(
        "/invoices/compile", params={"invoice_number": "FAJAN", "invoice_month": "2026-10"}, json={"prefix": "jan"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["orders"], body["units"], body["units_without_invoice"]) == (1, 1, 0)
    raw = body["job"]["payload"]["filters"]
    assert raw == {"invoice_number": "FAJAN", "invoice_month": "2026-10"}
    assert build_filter(**raw).invoice_number == "FAJAN"

    asyncio.run(run_compile_job(body["job"]["id"], filter_params=raw, prefix="jan"))

    with zipfile.ZipFile(stores / "archives" / "jan.zip") as zf:
        assert zf.namelist() == ["OD338918803190451100.pdf"]
        assert zf.read("OD338918803190451100.pdf") == _pdf("FAJAN1")


def test_an_invoice_filter_narrows_the_extract_and_the_zip_to_its_invoices(api, db, account, spawned, stores):
    """One order, a September product invoice and an October fee invoice: an
    October filter picks the order, and its CSV, its type counts and its zip
    hold October's invoice only — a month's filing must not carry the month
    before."""
    store = InvoiceStore.from_settings()
    order = _order(db, account, "OD338918803190451177")
    september = invoices.attach(db, store, order, _pdf("FASEP1"), source="upload")
    september.invoice_number, september.kind, september.invoice_type = "FASEP1", "product", "Product"
    september.invoice_date = datetime(2026, 9, 28)
    october = invoices.attach(db, store, order, _pdf("BFOCT2"), source="upload")
    october.invoice_number, october.kind, october.invoice_type = "BFOCT2", "fee", "Secure Packaging Fee"
    october.invoice_date = datetime(2026, 10, 2)
    db.commit()
    month = {"invoice_month": "2026-10"}

    types = api.get("/invoices/types", params=month).json()
    assert [(t["type"], t["invoices"]) for t in types["types"]] == [("Secure Packaging Fee", 1)]

    data = api.get("/invoices/data.csv", params={**month, "type": ["Product", "Secure Packaging Fee"]})
    assert data.status_code == 200, data.text
    assert [row[1] for row in list(csv.reader(io.StringIO(data.text.lstrip("﻿"))))[1:]] == ["BFOCT2"]
    # Its October invoices are not of the Product type: the unit is counted as left out, not filled
    # with September's product invoice.
    product_only = api.get("/invoices/data.csv", params={**month, "type": "Product"})
    assert product_only.headers["X-Invoice-Rows"] == "0"
    assert product_only.headers["X-Units-Left-Out"] == "1"

    response = api.post("/invoices/compile", params=month, json={"prefix": "oct"})
    assert response.status_code == 201, response.text
    job = response.json()["job"]
    asyncio.run(run_compile_job(job["id"], filter_params=job["payload"]["filters"], prefix="oct"))
    with zipfile.ZipFile(stores / "archives" / "oct.zip") as zf:
        assert zf.namelist() == ["OD338918803190451177.pdf"]
        assert zf.read("OD338918803190451177.pdf") == _pdf("BFOCT2")


# --- the lists that must name every filter ----------------------------------------------
def test_the_compile_run_is_handed_every_ledger_filter():
    """``_FILTER_KEYS`` is what a compilation keeps of the request; it must be
    exactly the parameters ``parse_filter`` reads, or a filter the screen
    applied is dropped on the way to the zip."""
    assert set(invoices_api._FILTER_KEYS) == set(inspect.signature(orders_api.parse_filter).parameters)


def test_a_saved_view_may_hold_every_filter_but_the_deep_links():
    """A run's rows (``job_id``) and one order (``row``, an alert's link) are
    where a link landed, not a view worth keeping."""
    view_arguments = set(orders_api.VIEW_FILTER_KEYS.values())
    assert view_arguments | {"job_id", "row"} == set(inspect.signature(build_filter).parameters)
