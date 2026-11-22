"""Invoices on file: fetched during a read-back with the account's session,
or uploaded by hand — matched to the order by the id in the file name or the
one printed on the PDF, and refused when the two disagree; served from the
ledger; reported by the endpoints."""

import asyncio
import contextlib
import io
import logging
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import invoices as invoices_api
from app.api import order_details
from app.api import orders as orders_api
from app.core import invoices
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Invoice, Job, JobStatus, JobType, Order, OrderStatus
from app.db.models import Session as SessionModel
from app.io import invoice_store
from app.io.invoice_store import InvoiceStore
from app.jobs import sync_tasks
from tests.fakes import FakeContext, FakePage

PDF = b"%PDF-1.4\n%fake invoice\n"
OTHER_PDF = b"%PDF-1.4\n%another invoice\n"

FIXTURES = Path(__file__).parent / "fixtures" / "invoices" / "OD332627468086718100"
#: The order every one of the three sample PDFs prints.
SAMPLE_ORDER_ID = "OD332627468086718100"


def _sample(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    """Every test writes into its own directory, never the configured one."""
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    return tmp_path / "invoices"


# --- the store -------------------------------------------------------------------
def test_the_store_files_by_day_and_order_id_and_is_idempotent(tmp_store):
    store = InvoiceStore(tmp_store)
    when = datetime(2025, 4, 17, 9, 12, tzinfo=timezone.utc)
    first = store.put("OD334935538466132100", PDF, when=when)
    again = store.put("OD334935538466132100", PDF, when=when)
    assert first == again
    assert first.startswith("2025/04/17/OD334935538466132100-")
    assert first.endswith(".pdf")
    assert store.path(first).read_bytes() == PDF
    assert not list(tmp_store.rglob("*.part"))  # no half-written file left behind


def test_the_store_refuses_a_path_outside_itself(tmp_store):
    store = InvoiceStore(tmp_store)
    with pytest.raises(ValueError):
        store.path("../../etc/passwd")
    assert store.exists("../../etc/passwd") is False


# --- fetched during a read-back ---------------------------------------------------
def _account(db, email: str) -> Account:
    row = Account(email=email, status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    db.add(SessionModel(account_id=row.id, cookies=[{"name": "SN", "value": email}], valid=True))
    db.commit()
    return row


def _run(db, monkeypatch, account: Account, scraped, *, responses: dict[str, tuple[int, bytes]]):
    """One read-back over a fake browser whose HTTP client answers ``responses``."""
    contexts: list[FakeContext] = []

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        page.context.request.responses = dict(responses)
        contexts.append(page.context)
        yield page.context, page

    async def fake_scrape_orders(page, *, limit=20, open_details=True, should_stop=None):
        return list(scraped)

    monkeypatch.setattr(sync_tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(sync_tasks, "scrape_orders", fake_scrape_orders)
    job = Job(type=JobType.sync, status=JobStatus.queued)
    db.add(job)
    db.commit()
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))
    return contexts


def test_a_read_back_puts_the_invoice_on_file_once(db, monkeypatch, tmp_store):
    from app.automation.sync import ScrapedOrder

    account = _account(db, "inv@test.com")
    db.add(Order(account_id=account.id, order_id="OD1234567890", status=OrderStatus.placed))
    db.commit()
    url = "https://www.flipkart.com/invoice/OD1234567890.pdf"
    seen = [ScrapedOrder(order_id="OD1234567890", platform_status="Delivered", invoice_url=url)]

    first = _run(db, monkeypatch, account, seen, responses={url: (200, PDF)})
    assert first[0].request.calls == [url]
    db.expire_all()
    row = db.query(Invoice).one()
    assert (row.order_id, row.source, row.size) == ("OD1234567890", "sync", len(PDF))
    assert (tmp_store / row.path).read_bytes() == PDF

    # The next read-back sees it on file and does not fetch it again.
    second = _run(db, monkeypatch, account, seen, responses={url: (200, PDF)})
    assert second[0].request.calls == []
    assert db.query(Invoice).count() == 1


def test_a_link_that_does_not_answer_with_a_pdf_is_skipped_not_fatal(db, monkeypatch):
    from app.automation.sync import ScrapedOrder

    account = _account(db, "nopdf@test.com")
    db.add(Order(account_id=account.id, order_id="OD1234567890", status=OrderStatus.placed))
    db.commit()
    url = "https://www.flipkart.com/invoice/OD1234567890.pdf"
    seen = [ScrapedOrder(order_id="OD1234567890", platform_status="Delivered", invoice_url=url)]

    _run(db, monkeypatch, account, seen, responses={url: (200, b"<html>login</html>")})

    db.expire_all()
    assert db.query(Invoice).count() == 0
    assert db.query(Order).one().platform_status == "Delivered"  # the read-back itself landed


# --- served and reported -----------------------------------------------------------
@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    application.include_router(invoices_api.router)
    application.include_router(order_details.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def ledger(db):
    account = Account(email="serve@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    order = Order(
        account_id=account.id,
        order_id="OD9876543210",
        status=OrderStatus.placed,
        model="Thing",
        placed_at=datetime(2026, 10, 1, 4, 0),
    )
    bare = Order(account_id=account.id, order_id="OD1111111111", status=OrderStatus.placed, placed_at=datetime(2026, 10, 2, 4, 0))
    db.add_all([order, bare])
    db.commit()
    return {"order": order, "bare": bare}


def test_an_uploaded_pdf_is_matched_by_the_order_id_in_its_name(api, db, ledger):
    files = [
        ("files", ("Invoice_OD9876543210.pdf", PDF, "application/pdf")),
        ("files", ("Invoice_OD9876543210.pdf", PDF, "application/pdf")),  # the same again
        ("files", ("OD9876543210-fee.pdf", OTHER_PDF, "application/pdf")),  # a second invoice
        ("files", ("OD0000000000.pdf", PDF, "application/pdf")),
        ("files", ("notes.pdf", PDF, "application/pdf")),
        ("files", ("OD9876543210.txt", b"hello", "text/plain")),
    ]
    out = api.post("/invoices/upload", files=files).json()
    assert out["stored"] == 2
    assert [f["status"] for f in out["files"]] == ["stored", "duplicate", "stored", "no_match", "no_match", "not_pdf"]
    assert db.query(Invoice).count() == 2


def test_the_ledger_serves_the_invoice_and_says_when_there_is_none(api, db, ledger):
    api.post("/invoices/upload", files=[("files", ("OD9876543210.pdf", PDF, "application/pdf"))])

    rows = {r["order_id"]: r for r in api.get("/orders").json()["items"]}
    assert rows["OD9876543210"]["invoice_on_file"] is True
    assert rows["OD1111111111"]["invoice_on_file"] is False

    response = api.get(f"/orders/{ledger['order'].id}/invoice.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content == PDF
    assert "OD9876543210.pdf" in response.headers["content-disposition"]
    assert api.get(f"/orders/{ledger['bare'].id}/invoice.pdf").status_code == 404

    csv_rows = api.get("/orders/export.csv").text.splitlines()
    header = csv_rows[0].lstrip("﻿").split(",")
    column = header.index("Invoice on file")
    by_id = {line.split(",")[header.index("Order ID")]: line.split(",")[column] for line in csv_rows[1:]}
    assert by_id == {"OD9876543210": "Yes", "OD1111111111": "No"}


def test_the_order_details_endpoints_report_the_file(api, db, ledger):
    api.post("/invoices/upload", files=[("files", ("OD9876543210.pdf", PDF, "application/pdf"))])

    listed = api.post("/orders/orderDetails", json={"emailid": "serve@test.com"}).json()["data"]
    by_id = {entry["orderId"]: entry for entry in listed}
    assert by_id["OD9876543210"]["downloadStatus"] == "DOWNLOADED"
    assert by_id["OD9876543210"]["invoiceFilePath"].startswith("/invoices/")
    assert by_id["OD9876543210"]["invoiceDownloadUrl"] == f"/orders/{ledger['order'].id}/invoice.pdf"
    assert (by_id["OD1111111111"]["downloadStatus"], by_id["OD1111111111"]["invoiceFilePath"]) == ("PENDING", None)

    one = api.post("/orders/orderDetails", json={"orderId": "OD9876543210"}).json()["data"]
    assert one["invoice"]["downloadStatus"] == "DOWNLOADED"
    assert one["invoice"]["filePath"].startswith("/invoices/2")


# --- matched by the order id the PDF prints ------------------------------------------
@pytest.fixture
def sample_ledger(db):
    """The sample PDFs' own order, and another order beside it."""
    account = Account(email="sample@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    sample = Order(
        account_id=account.id,
        order_id=SAMPLE_ORDER_ID,
        status=OrderStatus.placed,
        model="Apple iPhone 15 (Blue, 128 GB)",
        placed_at=datetime(2024, 10, 20, 4, 0),
    )
    other = Order(
        account_id=account.id, order_id="OD000000000000000001", status=OrderStatus.placed, placed_at=datetime(2026, 10, 2, 4, 0)
    )
    db.add_all([sample, other])
    db.commit()
    return {"account": account, "sample": sample, "other": other}


def test_a_file_with_no_order_id_in_its_name_is_matched_by_the_one_printed_on_the_pdf(api, db, sample_ledger):
    """The owner's own files are file_1.pdf, file_2.pdf, file_3.pdf — no OD in
    the name, but every one prints its order id."""
    files = [("files", (name, _sample(name), "application/pdf")) for name in ("file_1.pdf", "file_2.pdf", "file_3.pdf")]
    out = api.post("/invoices/upload", files=files).json()
    assert out["stored"] == 3
    assert [(f["status"], f["order_id"]) for f in out["files"]] == [("stored", SAMPLE_ORDER_ID)] * 3
    rows = db.query(Invoice).all()
    assert {row.order_pk for row in rows} == {sample_ledger["sample"].id}
    assert {row.invoice_number for row in rows} == {"FASUUT2500113513", "BFF8825001967376", "BFF8825001967375"}


def test_an_upload_says_which_pdfs_the_reader_could_not_read(api, db, sample_ledger):
    """NFR-009: a PDF the reader cannot read is put on file all the same
    (FR-023), and the answer says so with the reason — stored, but not read —
    rather than a plain "stored" that hides it until the row dialog is
    opened, while nothing of it can be in an invoice-data CSV."""
    files = [
        ("files", ("file_1.pdf", _sample("file_1.pdf"), "application/pdf")),
        ("files", (f"{SAMPLE_ORDER_ID}-scan.pdf", PDF, "application/pdf")),
    ]

    out = api.post("/invoices/upload", files=files).json()

    assert out["stored"] == 2
    read, unread = out["files"]
    assert (read["status"], read["reading"], read["reason"]) == ("stored", "read", None)
    assert (unread["status"], unread["order_id"], unread["reading"]) == ("stored", SAMPLE_ORDER_ID, "not_read")
    row = db.query(Invoice).filter(Invoice.invoice_type.is_(None)).one()
    assert unread["reason"] == row.parse_error
    assert unread["reason"].startswith("could not read the PDF")
    # A file not put on file by this upload has no reading to report.
    again = api.post("/invoices/upload", files=files[:1]).json()
    assert [(f["status"], f["reading"]) for f in again["files"]] == [("duplicate", None)]


def test_a_file_named_for_one_order_but_printing_another_is_refused_and_filed_under_neither(api, db, sample_ledger, tmp_store):
    out = api.post(
        "/invoices/upload", files=[("files", ("OD000000000000000001.pdf", _sample("file_1.pdf"), "application/pdf"))]
    ).json()
    assert out["stored"] == 0
    (entry,) = out["files"]
    assert (entry["status"], entry["order_id"]) == ("mismatch", "OD000000000000000001")
    assert "OD000000000000000001" in entry["reason"] and SAMPLE_ORDER_ID in entry["reason"]
    assert db.query(Invoice).count() == 0
    assert not list(tmp_store.rglob("*.pdf"))  # nothing in the store either

    # The same file under a name that agrees with it — or under no id at all — is fine.
    fine = api.post(
        "/invoices/upload",
        files=[
            ("files", (f"Invoice_{SAMPLE_ORDER_ID} (1).pdf", _sample("file_1.pdf"), "application/pdf")),
            ("files", ("scan.pdf", _sample("file_2.pdf"), "application/pdf")),
        ],
    ).json()
    assert [(f["status"], f["order_id"]) for f in fine["files"]] == [("stored", SAMPLE_ORDER_ID)] * 2


def test_a_pdf_printing_an_order_the_ledger_does_not_have_is_a_no_match_naming_it(api, db, ledger):
    out = api.post(
        "/invoices/upload",
        files=[
            ("files", ("scan.pdf", _sample("file_1.pdf"), "application/pdf")),
            ("files", ("notes.pdf", PDF, "application/pdf")),  # no id in the name, none on the PDF
        ],
    ).json()
    assert out["stored"] == 0
    first, second = out["files"]
    assert (first["status"], first["order_id"], first["reason"]) == (
        "no_match", SAMPLE_ORDER_ID, f"{SAMPLE_ORDER_ID} is not in the ledger"
    )
    assert (second["status"], second["order_id"]) == ("no_match", None)
    assert "file name or on the PDF" in second["reason"]


# --- zips, the way Flipkart hands out an order's invoices ---------------------------
def _zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_a_zip_named_for_the_order_is_unpacked_and_each_pdf_filed(api, db, sample_ledger):
    """Flipkart's OD….zip holds file_1.pdf, file_2.pdf…: the order id in the
    zip's name stands for every PDF inside; folders, other files and macOS
    resource forks are passed over."""
    archive = _zip(
        {
            "file_1.pdf": _sample("file_1.pdf"),
            "file_2.pdf": _sample("file_2.pdf"),
            "readme.txt": b"hi",
            "__MACOSX/._file_1.pdf": b"junk",
        }
    )
    out = api.post("/invoices/upload", files=[("files", (f"{SAMPLE_ORDER_ID}.zip", archive, "application/zip"))]).json()
    assert out["stored"] == 2
    assert [(f["filename"], f["status"], f["order_id"]) for f in out["files"]] == [
        (f"{SAMPLE_ORDER_ID}.zip/file_1.pdf", "stored", SAMPLE_ORDER_ID),
        (f"{SAMPLE_ORDER_ID}.zip/file_2.pdf", "stored", SAMPLE_ORDER_ID),
    ]
    assert db.query(Invoice).count() == 2
    assert {row.invoice_number for row in db.query(Invoice)} == {"FASUUT2500113513", "BFF8825001967376"}


def test_a_zip_named_for_another_order_is_refused_pdf_by_pdf(api, db, sample_ledger, tmp_store):
    archive = _zip({"file_1.pdf": _sample("file_1.pdf")})
    out = api.post("/invoices/upload", files=[("files", ("OD000000000000000001.zip", archive, "application/zip"))]).json()
    assert out["stored"] == 0
    (entry,) = out["files"]
    assert (entry["filename"], entry["status"]) == ("OD000000000000000001.zip/file_1.pdf", "mismatch")
    assert SAMPLE_ORDER_ID in entry["reason"] and "OD000000000000000001" in entry["reason"]
    assert not list(tmp_store.rglob("*.pdf"))


def test_a_zip_without_an_order_id_in_its_name_matches_by_the_print(api, db, sample_ledger):
    archive = _zip({"invoices/file_3.pdf": _sample("file_3.pdf")})
    out = api.post("/invoices/upload", files=[("files", ("download.zip", archive, "application/zip"))]).json()
    assert [(f["filename"], f["status"], f["order_id"]) for f in out["files"]] == [
        ("download.zip/file_3.pdf", "stored", SAMPLE_ORDER_ID)
    ]


def test_a_zip_that_is_not_one_or_holds_no_pdf_is_answered_not_raised(api, db, ledger):
    out = api.post(
        "/invoices/upload",
        files=[
            ("files", ("OD9876543210.zip", b"PK\x03\x04not really a zip", "application/zip")),
            ("files", ("empty.zip", _zip({"notes.txt": b"hello"}), "application/zip")),
            ("files", ("OD9876543210.pdf", PDF, "application/pdf")),  # the rest of the upload still goes through
        ],
    ).json()
    assert out["stored"] == 1
    assert [(f["status"], f["reason"]) for f in out["files"][:2]] == [
        ("not_pdf", "not a PDF, and not a zip that can be opened"),
        ("not_pdf", "the zip holds no PDF"),
    ]
    assert out["files"][2]["status"] == "stored"


def test_a_zip_member_that_is_no_pdf_is_told_apart_and_the_caps_hold(api, db, ledger, monkeypatch):
    archive = _zip({"file_1.pdf": b"not a pdf at all", "file_2.pdf": PDF})
    out = api.post("/invoices/upload", files=[("files", ("OD9876543210.zip", archive, "application/zip"))]).json()
    assert [f["status"] for f in out["files"]] == ["not_pdf", "stored"]

    monkeypatch.setattr(invoices_api, "MAX_ZIP_MEMBERS", 1)
    out = api.post("/invoices/upload", files=[("files", ("OD9876543210.zip", archive, "application/zip"))]).json()
    assert [(f["status"], f["reason"]) for f in out["files"]] == [("too_large", "the zip holds more than 1 PDFs")]

    monkeypatch.setattr(invoices_api, "MAX_ZIP_MEMBERS", 100)
    monkeypatch.setattr(invoices, "MAX_PDF_BYTES", 10)
    out = api.post(
        "/invoices/upload", files=[("files", ("OD9876543210.zip", _zip({"big.pdf": PDF}), "application/zip"))]
    ).json()
    assert [(f["filename"], f["status"]) for f in out["files"]] == [("OD9876543210.zip/big.pdf", "too_large")]

    monkeypatch.setattr(invoices, "MAX_PDF_BYTES", 25 * 1024 * 1024)
    monkeypatch.setattr(invoices_api, "MAX_FILES", 2)
    three = _zip({"a.pdf": PDF, "b.pdf": PDF, "c.pdf": OTHER_PDF})
    response = api.post("/invoices/upload", files=[("files", ("OD9876543210.zip", three, "application/zip"))])
    assert response.status_code == 413
    assert "zip counts" in response.json()["detail"]


# --- the invoices of an order, listed and served one by one (the owner, 26 Oct 2026) -------
def test_an_orders_invoices_are_listed_with_what_the_reader_took_off_them(api, db, sample_ledger):
    files = [("files", (name, _sample(name), "application/pdf")) for name in ("file_1.pdf", "file_2.pdf", "file_3.pdf")]
    assert api.post("/invoices/upload", files=files).json()["stored"] == 3
    order_pk = sample_ledger["sample"].id

    listed = api.get(f"/orders/{order_pk}/invoices").json()
    assert [row["invoice_type"] for row in listed] == ["Product", "Secure Packaging Fee", "Offer Handling Fee"]
    product = listed[0]
    assert product["invoice_number"] == "FASUUT2500113513"
    assert product["grand_total"] == 50999.0
    assert (product["seller_gstin"], product["buyer_gstin"]) == ("09AAGCK4304E1ZT", "09AAUFT2204P1ZI")
    assert product["lines"][0]["imei"] == "358107262645105"
    assert product["download_path"] == f"/orders/{order_pk}/invoices/{product['id']}.pdf"

    pdf = api.get(product["download_path"])
    assert pdf.status_code == 200 and pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.content == _sample("file_1.pdf")
    assert "FASUUT2500113513" in pdf.headers["content-disposition"]

    rows = {r["order_id"]: r for r in api.get("/orders").json()["items"]}
    assert (rows[SAMPLE_ORDER_ID]["invoice_on_file"], rows[SAMPLE_ORDER_ID]["invoices_on_file"]) == (True, 3)
    assert (rows["OD000000000000000001"]["invoice_on_file"], rows["OD000000000000000001"]["invoices_on_file"]) == (
        False,
        0,
    )

    # Another order's invoice is not served under this order; unknown ids are 404.
    other = sample_ledger["other"].id
    assert api.get(f"/orders/{other}/invoices").json() == []
    assert api.get(f"/orders/{other}/invoices/{product['id']}.pdf").status_code == 404
    assert api.get(f"/orders/{order_pk}/invoices/999999.pdf").status_code == 404
    assert api.get("/orders/999999/invoices").status_code == 404


def test_attach_refuses_a_pdf_printing_another_order_with_both_ids_in_the_message(db, sample_ledger, tmp_store):
    store = InvoiceStore(tmp_store)
    with pytest.raises(invoices.OrderMismatch) as caught:
        invoices.attach(db, store, sample_ledger["other"], _sample("file_1.pdf"), source="upload")
    assert (caught.value.expected, caught.value.found) == ("OD000000000000000001", SAMPLE_ORDER_ID)
    assert "OD000000000000000001" in str(caught.value) and SAMPLE_ORDER_ID in str(caught.value)
    assert db.query(Invoice).count() == 0
    assert not list(tmp_store.rglob("*.pdf"))


def test_a_fetched_invoice_printing_another_order_is_not_recorded_but_the_right_one_is(db, sample_ledger, caplog):
    """The read-back path: a link that answered with another order's PDF is
    logged and dropped; the order's own PDF goes on file as before."""
    account_id = sample_ledger["account"].id
    with caplog.at_level(logging.WARNING, logger="ordering.invoices"):
        assert invoices.record(account_id, {"OD000000000000000001": _sample("file_2.pdf")}, source="sync") == 0
    assert db.query(Invoice).count() == 0
    assert "OD000000000000000001" in caplog.text and SAMPLE_ORDER_ID in caplog.text

    assert invoices.record(account_id, {SAMPLE_ORDER_ID: _sample("file_2.pdf")}, source="sync") == 1
    row = db.query(Invoice).one()
    assert (row.order_id, row.source, row.invoice_type) == (SAMPLE_ORDER_ID, "sync", "Secure Packaging Fee")
