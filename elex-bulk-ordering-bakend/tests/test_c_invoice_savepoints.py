"""Invoice rows that must not stall a pass (audit F-28, second half).

The start-up backfill and the upload write each invoice row in a savepoint of
its own. A row the database refuses is rolled back alone, logged by its id and
the error's type, and the pass carries on. A refused reading is set aside —
marked read by this parser, the reason on the row — so the backfill does not
try it again at every start; a failure of the database's own state (locked,
disconnected) leaves the row to be tried again instead. A row that cannot be
saved at all takes its PDF out of the store with it, and an upload whose
commit fails takes back every file it wrote: no PDF is left without its row
(NFR-009).

The database's refusal is made from inside the flush, the way PostgreSQL
refuses a value too long for its column; SQLite itself refuses nothing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect
from sqlalchemy.exc import DataError, IntegrityError, OperationalError

from app.api import invoices as invoices_api
from app.core import invoices
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Invoice, Order, OrderStatus
from app.io import invoice_store
from app.io.invoice_pdf import PARSER_VERSION
from app.io.invoice_store import InvoiceStore, sha256

FIXTURES = Path(__file__).parent / "fixtures" / "invoices" / "OD332627468086718100"
ORDER_ID = "OD332627468086718100"
FILES = ("file_1.pdf", "file_2.pdf", "file_3.pdf")
#: The Secure Packaging Fee invoice among the three: the one refused below.
FEE_FILE = "file_2.pdf"
FEE_NUMBER = "BFF8825001967376"


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    return tmp_path / "invoices"


class Refusal:
    """The database refusing the invoice row writes a predicate picks, raised
    from inside the flush. The predicate reads only what the write itself
    carries (``__dict__``) or the row's identity, so it never loads anything
    mid-flush. ``attempts`` counts the refused writes; ``lift()`` ends it."""

    def __init__(self) -> None:
        self.attempts = 0
        self._listeners: list = []

    def __call__(self, pick, error=DataError) -> "Refusal":
        def listener(_mapper, _connection, target) -> None:
            if pick(target):
                self.attempts += 1
                raise error("INSERT/UPDATE invoices", {}, Exception("refused by the test"))

        for name in ("before_insert", "before_update"):
            event.listen(Invoice, name, listener)
            self._listeners.append((name, listener))
        return self

    def lift(self) -> None:
        while self._listeners:
            event.remove(Invoice, *self._listeners.pop())


@pytest.fixture
def refuse():
    refusal = Refusal()
    yield refusal
    refusal.lift()


def _identity(target) -> int | None:
    key = inspect(target).identity
    return key[0] if key else None


def _with_reading(target) -> bool:
    """The write carries what the reader took off the PDF."""
    return target.__dict__.get("invoice_number") is not None


@pytest.fixture
def order(db) -> Order:
    account = Account(email="inv@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    row = Order(account_id=account.id, status=OrderStatus.placed, model="Apple iPhone 15", qty=1, order_id=ORDER_ID)
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def on_file(db, order) -> dict[str, int]:
    """The order's three real invoices on file, then made unparsed again — the
    backfill's work. File name → row id."""
    ids = {}
    for name in FILES:
        row = invoices.attach(db, InvoiceStore.from_settings(), order, (FIXTURES / name).read_bytes(), source="upload")
        ids[name] = row.id
    db.commit()
    for row in db.query(Invoice).all():
        row.parsed_at = row.parser_version = row.invoice_type = row.invoice_number = row.lines = None
    db.commit()
    return ids


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(invoices_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def _upload(api: TestClient):
    files = [("files", (name, (FIXTURES / name).read_bytes(), "application/pdf")) for name in FILES]
    return api.post("/invoices/upload", files=files)


# --- the start-up backfill ----------------------------------------------------------------
def test_the_fee_fixture_is_the_one_these_tests_refuse():
    assert invoices.parse_pdf((FIXTURES / FEE_FILE).read_bytes()).invoice_number == FEE_NUMBER


@pytest.mark.parametrize("how", ["database", "driver"])
def test_a_row_whose_reading_is_refused_is_rolled_back_alone_and_set_aside(db, on_file, refuse, monkeypatch, caplog, how):
    """Refused by the database (a DataError: PostgreSQL's "value too long"),
    or by the driver before the statement is sent (a value JSON cannot hold):
    either way the rows on both sides are saved and the refused one is set
    aside for this parser."""
    fee = on_file[FEE_FILE]
    if how == "database":
        refuse(lambda target: _identity(target) == fee and _with_reading(target))
        expected = "DataError"
    else:
        real = invoices.apply_parse

        def unstorable(row, parsed):
            real(row, parsed)
            if row.id == fee:
                row.lines = [object()]

        monkeypatch.setattr(invoices, "apply_parse", unstorable)
        # SQLite's path wraps the JSON adapter's refusal in a StatementError;
        # PostgreSQL's driver serialises the column itself and raises it bare.
        from app.db.base import engine

        expected = "TypeError" if engine.dialect.name == "postgresql" else "StatementError"

    with caplog.at_level(logging.WARNING, logger="ordering.invoices"):
        done = invoices.backfill_unparsed(limit=10)

    assert (done.parsed, done.missing, done.unsaved, done.last_id) == (2, 0, 1, max(on_file.values()))
    db.expire_all()
    for name in set(FILES) - {FEE_FILE}:
        saved = db.get(Invoice, on_file[name])
        assert saved.invoice_number is not None
        assert (saved.parser_version, saved.parse_error) == (PARSER_VERSION, None)
    refused = db.get(Invoice, fee)
    assert (refused.invoice_number, refused.invoice_type, refused.lines) == (None, None, None)  # rolled back
    assert refused.parse_error == f"{invoices.UNSAVED_READING_ERROR} ({expected})"
    assert refused.parser_version == PARSER_VERSION and refused.parsed_at is not None
    # The log names the row and the error's type — never what the row held.
    named = [r.getMessage() for r in caplog.records if f"invoice {fee}" in r.getMessage()]
    assert len(named) == 1 and expected in named[0] and "set aside" in named[0]
    assert "refused by the test" not in caplog.text

    # Set aside: the next start does not try it again.
    assert invoices.backfill_unparsed(limit=10).last_id is None


def test_the_backfill_walks_on_past_a_refused_row_to_the_end(db, on_file, refuse, caplog):
    """One row per pass, the refused one in the middle: before, the pass
    raised at it, the run ended there, and every start began again at the
    first row; now the run reads the rows after it."""
    middle = sorted(on_file.values())[1]
    refuse(lambda target: _identity(target) == middle and _with_reading(target))

    with caplog.at_level(logging.INFO, logger="ordering.invoices"):
        assert asyncio.run(invoices.backfill_all(chunk=1, pause=0)) == 2

    assert "invoice backfill failed" not in caplog.text
    assert "2 PDF(s) read, 0 missing from the store, 1 could not be saved" in caplog.text
    db.expire_all()
    assert all(row.parser_version == PARSER_VERSION for row in db.query(Invoice).all())


def _write_lock_held() -> bool:
    """Whether another connection is holding SQLite's write lock right now."""
    import sqlite3

    from app.db.base import engine

    probe = sqlite3.connect(engine.url.database, timeout=0)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
        return False
    except sqlite3.OperationalError as exc:
        return "locked" in str(exc)
    finally:
        probe.close()


def test_no_pdf_is_read_with_the_write_lock_held_after_a_refused_row(db, on_file, refuse, monkeypatch):
    """On SQLite a rolled-back savepoint leaves its transaction open. Reading
    the next PDFs inside it held the write lock for the rest of the pass, and
    an order run's record would wait behind the PDF reader. Each row now ends
    its transaction before the next PDF is read."""
    from app.db.base import engine

    if engine.url.get_backend_name() != "sqlite":
        pytest.skip("the write lock is SQLite's")
    first = min(on_file.values())
    refuse(lambda target: _identity(target) == first and _with_reading(target))
    real = invoices.parse_pdf
    locked_while_reading: list[bool] = []

    def reading(data):
        locked_while_reading.append(_write_lock_held())
        return real(data)

    monkeypatch.setattr(invoices, "parse_pdf", reading)

    done = invoices.backfill_unparsed(limit=10)

    assert (done.parsed, done.unsaved) == (2, 1)
    assert locked_while_reading == [False, False, False]


def test_text_a_driver_cannot_encode_is_stored_with_question_marks(db, order):
    """A font mapped as Identity can give the reader lone surrogates, which a
    UTF-8 driver refuses outright; they are stored as '?' instead."""
    from app.io.invoice_pdf import ParsedInvoice

    row = Invoice(order_pk=order.id, path="x.pdf", size=1, sha256="0" * 64, source="upload")
    invoices.apply_parse(row, ParsedInvoice(seller_name="Seller \ud800 Name", error="bad \udfff glyph"))
    db.add(row)
    db.commit()

    db.expire_all()
    saved = db.get(Invoice, row.id)
    assert (saved.seller_name, saved.parse_error) == ("Seller ? Name", "bad ? glyph")


def test_a_nul_character_is_dropped_so_postgresql_can_store_the_text(db, order):
    """No PostgreSQL text column holds a NUL, and a PDF's text can carry one."""
    from app.io.invoice_pdf import ParsedInvoice

    row = Invoice(order_pk=order.id, path="y.pdf", size=1, sha256="1" * 64, source="upload")
    invoices.apply_parse(row, ParsedInvoice(seller_name="Sell\x00er", reason="re\x00ason"))
    db.add(row)
    db.commit()

    db.expire_all()
    saved = db.get(Invoice, row.id)
    assert (saved.seller_name, saved.reason) == ("Seller", "reason")


def test_a_row_failed_by_the_databases_own_state_is_tried_again_at_the_next_start(db, on_file, refuse, caplog):
    """A locked or vanished database says nothing about the row: it is not set
    aside, the pass carries on, and the next start reads it."""
    fee = on_file[FEE_FILE]
    refuse(lambda target: _identity(target) == fee, error=OperationalError)

    with caplog.at_level(logging.WARNING, logger="ordering.invoices"):
        done = invoices.backfill_unparsed(limit=10)

    assert (done.parsed, done.unsaved) == (2, 1)
    db.expire_all()
    left = db.get(Invoice, fee)
    assert (left.parser_version, left.parsed_at, left.parse_error) == (None, None, None)
    assert f"invoice {fee} could not be saved (OperationalError); tried again at the next start" in caplog.text

    refuse.lift()  # the database is back
    again = invoices.backfill_unparsed(limit=10)
    assert (again.parsed, again.unsaved, again.last_id) == (1, 0, fee)
    db.expire_all()
    assert db.get(Invoice, fee).invoice_number == FEE_NUMBER


def test_a_missing_file_note_that_cannot_be_saved_is_not_set_aside(db, on_file, refuse, store):
    """The missing-file note is tried again at every start by design — the
    file may come back — so a refused note never marks the row read."""
    fee = on_file[FEE_FILE]
    InvoiceStore.from_settings().path(db.get(Invoice, fee).path).unlink()
    refuse(lambda target: _identity(target) == fee)

    done = invoices.backfill_unparsed(limit=10)

    assert (done.parsed, done.missing, done.unsaved) == (2, 0, 1)
    db.expire_all()
    assert db.get(Invoice, fee).parser_version is None


# --- the upload ----------------------------------------------------------------------------
def test_an_upload_keeps_a_pdf_whose_reading_is_refused_and_files_the_rest(api, db, order, refuse):
    refuse(lambda target: target.__dict__.get("invoice_number") == FEE_NUMBER)

    response = _upload(api)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stored"] == 3
    assert [f["status"] for f in body["files"]] == ["stored", "stored", "stored"]
    # The refused reading is said in the answer: on file, but not read.
    answers = {f["filename"]: f for f in body["files"]}
    assert (answers[FEE_FILE]["reading"], answers[FEE_FILE]["reason"]) == (
        "not_read", f"{invoices.UNSAVED_READING_ERROR} (DataError)"
    )
    assert all(answers[name]["reading"] == "read" for name in set(FILES) - {FEE_FILE})
    rows = {row.sha256: row for row in db.query(Invoice).all()}
    assert len(rows) == 3
    kept = rows[sha256((FIXTURES / FEE_FILE).read_bytes())]
    # On file like an unreadable PDF: no reading, the reason on the row, and
    # read by this parser, so the backfill leaves it be.
    assert (kept.invoice_number, kept.lines, kept.kind) == (None, None, "unknown")
    assert kept.parse_error == f"{invoices.UNSAVED_READING_ERROR} (DataError)"
    assert kept.parser_version == PARSER_VERSION
    assert InvoiceStore.from_settings().exists(kept.path)
    others = [row for row in rows.values() if row is not kept]
    assert all(row.invoice_number and row.parse_error is None for row in others)
    assert invoices.backfill_unparsed(limit=10).last_id is None


def _pdfs_in(store: Path) -> set[str]:
    """Every PDF in the store, by its path relative to the store's root."""
    return {path.relative_to(store).as_posix() for path in store.rglob("*.pdf")}


@pytest.mark.parametrize(("error", "attempts"), [(IntegrityError, 2), (OperationalError, 1)])
def test_an_upload_answers_a_row_that_cannot_be_saved_at_all_and_files_the_rest(api, db, order, refuse, store, error, attempts):
    """Refused with its reading and without it (IntegrityError: tried twice),
    or refused for the database's own state (OperationalError: not tried
    again without the reading) — answered "not_saved", never a 500, and the
    upload's other files are on file. The refused one's PDF is taken back out
    of the store: no file is left there without its row (NFR-009)."""
    fee_digest = sha256((FIXTURES / FEE_FILE).read_bytes())
    refusal = refuse(lambda target: target.__dict__.get("sha256") == fee_digest, error=error)

    response = _upload(api)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stored"] == 2
    answers = {f["filename"]: f for f in body["files"]}
    assert answers[FEE_FILE]["status"] == "not_saved"
    assert answers[FEE_FILE]["order_id"] == ORDER_ID
    assert f"could not be saved ({error.__name__})" in answers[FEE_FILE]["reason"]
    assert all(answers[name]["status"] == "stored" for name in set(FILES) - {FEE_FILE})
    assert refusal.attempts == attempts
    db.expire_all()
    rows = db.query(Invoice).all()
    assert {row.sha256 for row in rows} == {
        sha256((FIXTURES / name).read_bytes()) for name in set(FILES) - {FEE_FILE}
    }
    assert _pdfs_in(store) == {row.path for row in rows}


def test_an_upload_whose_commit_fails_leaves_no_pdf_in_the_store(db, order, store):
    """NFR-009: every row of the upload is in its one transaction. When the
    final commit fails, the rows go — and so do the files the upload wrote,
    or the store keeps PDFs no row knows. The client hears of the failure."""
    from sqlalchemy import event

    from app.db.base import SessionLocal

    def refuse_to_commit(session) -> None:
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    application = FastAPI()
    application.include_router(invoices_api.router)
    token = create_access_token("tester", role="admin")
    api = TestClient(application, headers={"Authorization": f"Bearer {token}"}, raise_server_exceptions=False)
    event.listen(SessionLocal, "before_commit", refuse_to_commit)
    try:
        response = _upload(api)
    finally:
        event.remove(SessionLocal, "before_commit", refuse_to_commit)

    assert response.status_code == 500
    db.expire_all()
    assert db.query(Invoice).count() == 0
    assert _pdfs_in(store) == set()

    # The same files go through once the database is back, each on file once.
    api_back = TestClient(application, headers={"Authorization": f"Bearer {token}"})
    assert _upload(api_back).json()["stored"] == 3
    assert _pdfs_in(store) == {row.path for row in db.query(Invoice).all()}


def test_a_file_already_in_the_store_is_never_taken_back(db, order, refuse, store):
    """Only a file the failed write itself put there is removed: one that
    was there before — the same bytes, the same order, the same day — may be
    another row's, and a row must never point at a missing file."""
    data = (FIXTURES / FEE_FILE).read_bytes()
    there_before = InvoiceStore.from_settings().put(ORDER_ID, data, sha256(data))
    refuse(lambda target: target.__dict__.get("sha256") == sha256(data), error=OperationalError)

    with pytest.raises(invoices.NotSaved):
        invoices.attach(db, InvoiceStore.from_settings(), order, data, source="upload")

    assert _pdfs_in(store) == {there_before}


def test_taking_back_keeps_a_file_a_saved_row_points_at_and_keeps_all_when_it_cannot_ask(db, on_file, store, caplog):
    """A commit can fail on its way back from a database that did commit:
    a file a saved row points at stays. And when the database cannot be
    asked at all, every file stays — a PDF left over is harmless, a row
    whose PDF is gone is not."""
    saved = db.get(Invoice, on_file[FEE_FILE]).path
    loose = InvoiceStore.from_settings().put("OD9999999999", b"%PDF-1.4\n%loose\n")
    assert {saved, loose} <= _pdfs_in(store)

    def cannot_ask():
        raise OperationalError("SELECT", {}, Exception("the database is gone"))

    with caplog.at_level(logging.WARNING, logger="ordering.invoices"):
        assert invoices.remove_unsaved_files(InvoiceStore.from_settings(), [saved, loose], check=cannot_ask) == []
    assert {saved, loose} <= _pdfs_in(store)
    assert "could not be checked (OperationalError)" in caplog.text

    assert invoices.remove_unsaved_files(InvoiceStore.from_settings(), [saved, loose]) == [loose]
    assert saved in _pdfs_in(store) and loose not in _pdfs_in(store)


# --- the read-back ---------------------------------------------------------------------------
def test_a_read_back_skips_an_invoice_it_cannot_save_and_records_the_rest(db, order, refuse, store):
    other = Order(account_id=order.account_id, status=OrderStatus.placed, model="Case", qty=1, order_id="OD2222222222")
    db.add(other)
    db.commit()
    refused_pdf = b"%PDF-1.4\n%refused\n"
    refuse(lambda target: target.__dict__.get("sha256") == sha256(refused_pdf), error=IntegrityError)

    saved = invoices.record(order.account_id, {ORDER_ID: refused_pdf, "OD2222222222": b"%PDF-1.4\n%kept\n"})

    assert saved == 1
    db.expire_all()
    rows = db.query(Invoice).all()
    assert [row.order_id for row in rows] == ["OD2222222222"]
    # The refused invoice's PDF went with its row; the read-back fetches it again next time.
    assert _pdfs_in(store) == {row.path for row in rows}
