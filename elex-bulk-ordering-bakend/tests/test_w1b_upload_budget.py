"""What one invoice upload may cost (audit F-27, 26 Oct 2026).

A loose PDF is read no further than its 25 MB cap plus one byte and refused
by name past it, the rest of the upload filed as before. The upload as a
whole — the loose files plus what its zips unpack to — may hand the reader at
most ``MAX_UPLOAD_BYTES``: the file that would take it past that, and every
file after it, is refused unread, each saying so. A zip is weighed on the
sizes it declares, before any of it is unpacked. The caps that were there
before keep their messages.

The caps are lowered for most tests (they are module constants, read at call
time); the read of a loose file is also checked at its real 25 MB.
"""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette import datastructures

from app.api import invoices as invoices_api
from app.core import invoices
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Invoice, Order, OrderStatus
from app.io import invoice_store

ORDER_ID = "OD9876543210"
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    """Every test writes into its own directory, never the configured one."""
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    return tmp_path / "invoices"


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(invoices_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def order(db) -> Order:
    account = Account(email="budget@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    row = Order(account_id=account.id, order_id=ORDER_ID, status=OrderStatus.placed, placed_at=datetime(2026, 10, 1, 4, 0))
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def reads(monkeypatch) -> list[tuple[str, int, int]]:
    """Every read the upload makes of a file it was sent: ``(file name, bytes
    asked for, bytes handed back)``."""
    seen: list[tuple[str, int, int]] = []
    real = datastructures.UploadFile.read

    async def spy(self, size: int = -1) -> bytes:
        data = await real(self, size)
        seen.append((self.filename, size, len(data)))
        return data

    monkeypatch.setattr(datastructures.UploadFile, "read", spy)
    return seen


def _pdf(size: int, tag: str) -> bytes:
    """A file of exactly ``size`` bytes that starts the way a PDF does (the
    reader cannot read it, which is beside the point: it is kept all the
    same). ``tag`` makes each one different, so none is a duplicate."""
    head = f"%PDF-1.4\n%{tag}\n".encode()
    return head + b"x" * (size - len(head))


def _zip(members: dict[str, bytes], *, stored: bool = False) -> bytes:
    buffer = io.BytesIO()
    method = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _upload(api: TestClient, *files: tuple[str, bytes]) -> dict:
    response = api.post("/invoices/upload", files=[("files", (name, data, "application/pdf")) for name, data in files])
    assert response.status_code == 200, response.text
    return response.json()


def _answers(out: dict) -> list[tuple[str, str]]:
    return [(entry["filename"], entry["status"]) for entry in out["files"]]


PAST_TOTAL = "the upload is over {mb} MB in all, so this file was not read; upload it again in a smaller batch"


# --- the read of one file ----------------------------------------------------------------
class _FakeUpload:
    """Stands in for an uploaded file: records what is asked of it."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.reads: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        return self._buffer.read(size)

    async def seek(self, offset: int) -> None:
        self._buffer.seek(offset)


def test_a_loose_file_is_read_no_further_than_25_mb_and_one_byte():
    """Four bytes to tell a zip from a PDF, then from the start again, never
    more than the PDF cap plus the one byte that says it is over."""
    upload = _FakeUpload(_pdf(26 * MB, "big"))

    data = asyncio.run(invoices_api._read_capped(upload))

    assert invoices.MAX_PDF_BYTES == 25 * MB
    assert upload.reads == [4, 25 * MB + 1]
    assert len(data) == 25 * MB + 1


def test_a_zip_is_read_up_to_the_zip_cap_and_one_byte(monkeypatch):
    monkeypatch.setattr(invoices_api, "MAX_ZIP_BYTES", 1000)
    upload = _FakeUpload(b"PK\x03\x04" + b"z" * 5000)

    data = asyncio.run(invoices_api._read_capped(upload))

    assert upload.reads == [4, 1001]
    assert len(data) == 1001
    assert data.startswith(b"PK\x03\x04")


def test_a_short_file_is_read_whole():
    upload = _FakeUpload(b"%PDF")
    assert asyncio.run(invoices_api._read_capped(upload)) == b"%PDF"
    assert asyncio.run(invoices_api._read_capped(_FakeUpload(b""))) == b""


# --- a loose PDF over its own cap ----------------------------------------------------------
def test_a_loose_pdf_over_25_mb_is_refused_by_name_and_the_rest_is_filed(api, db, order, reads):
    """At the real cap: the 25 MB + 10 bytes file is read to 25 MB + 1 and no
    further, refused by name, and the file after it is stored as before."""
    big = _pdf(25 * MB + 10, "big")
    small = _pdf(2000, "small")

    out = _upload(api, (f"{ORDER_ID}-big.pdf", big), (f"{ORDER_ID}.pdf", small))

    assert out["stored"] == 1
    assert _answers(out) == [(f"{ORDER_ID}-big.pdf", "too_large"), (f"{ORDER_ID}.pdf", "stored")]
    assert out["files"][0]["reason"] == "larger than 25 MB"
    big_reads = [(asked, got) for name, asked, got in reads if name == f"{ORDER_ID}-big.pdf"]
    assert big_reads == [(4, 4), (25 * MB + 1, 25 * MB + 1)]
    assert db.query(Invoice).count() == 1


def test_a_file_over_its_own_cap_takes_no_room_from_the_total(api, db, order, monkeypatch):
    monkeypatch.setattr(invoices, "MAX_PDF_BYTES", 1500)
    monkeypatch.setattr(invoices_api, "MAX_UPLOAD_BYTES", 2500)

    out = _upload(
        api,
        (f"{ORDER_ID}-huge.pdf", _pdf(5000, "huge")),  # refused by name, never kept
        (f"{ORDER_ID}-1.pdf", _pdf(1200, "one")),
        (f"{ORDER_ID}-2.pdf", _pdf(1200, "two")),  # 2400 of 2500: fits
    )

    assert [status for _name, status in _answers(out)] == ["too_large", "stored", "stored"]
    assert out["files"][0]["reason"] == "larger than 25 MB"


# --- the upload's total ------------------------------------------------------------------------
def test_past_the_total_the_file_that_does_not_fit_and_every_one_after_it_is_refused_unread(
    api, db, order, reads, monkeypatch
):
    monkeypatch.setattr(invoices_api, "MAX_UPLOAD_BYTES", 2500)
    names = [f"{ORDER_ID}-{n}.pdf" for n in range(1, 5)]

    out = _upload(
        api,
        (names[0], _pdf(1000, "1")),
        (names[1], _pdf(1000, "2")),
        (names[2], _pdf(1000, "3")),  # 3000 > 2500: past the total
        (names[3], _pdf(100, "4")),  # would fit on its own, but comes after
    )

    assert out["stored"] == 2
    assert _answers(out) == list(zip(names, ["stored", "stored", "too_large", "too_large"]))
    message = PAST_TOTAL.format(mb=0)  # 2500 bytes is "0 MB"; the real total reads 250
    assert [entry["reason"] for entry in out["files"][2:]] == [message, message]
    assert db.query(Invoice).count() == 2
    read_names = {name for name, _asked, _got in reads}
    assert names[3] not in read_names  # not a byte of it was read
    assert names[2] in read_names  # read once, to its own cap, to learn it does not fit


def test_the_total_is_inclusive_and_names_250_mb(api, db, order, monkeypatch):
    assert invoices_api.MAX_UPLOAD_BYTES == 250 * MB
    monkeypatch.setattr(invoices_api, "MAX_UPLOAD_BYTES", 2000)

    out = _upload(api, (f"{ORDER_ID}-1.pdf", _pdf(1000, "1")), (f"{ORDER_ID}-2.pdf", _pdf(1000, "2")))

    assert out["stored"] == 2  # exactly the total fits

    monkeypatch.setattr(invoices_api, "MAX_UPLOAD_BYTES", 250 * MB)
    answer = invoices_api._past_total("late.pdf")
    assert (answer.status, answer.reason) == ("too_large", PAST_TOTAL.format(mb=250))


def test_a_zip_counts_what_it_unpacks_to_not_what_it_weighs(api, db, order, monkeypatch):
    """Two 1000-byte PDFs deflate to a few hundred bytes, but the reader is
    handed 2000: that is what the total counts."""
    monkeypatch.setattr(invoices_api, "MAX_UPLOAD_BYTES", 2500)
    archive = _zip({"file_1.pdf": _pdf(1000, "z1"), "file_2.pdf": _pdf(1000, "z2")})
    assert len(archive) < 1000

    out = _upload(api, (f"{ORDER_ID}.zip", archive), (f"{ORDER_ID}-loose.pdf", _pdf(1000, "loose")))

    assert _answers(out) == [
        (f"{ORDER_ID}.zip/file_1.pdf", "stored"),
        (f"{ORDER_ID}.zip/file_2.pdf", "stored"),
        (f"{ORDER_ID}-loose.pdf", "too_large"),  # 2000 + 1000 > 2500
    ]
    assert out["files"][2]["reason"] == PAST_TOTAL.format(mb=0)


def test_a_zip_past_the_total_is_refused_before_any_of_it_is_unpacked(api, db, order, monkeypatch):
    monkeypatch.setattr(invoices_api, "MAX_UPLOAD_BYTES", 2500)
    unpacked: list[str] = []
    real = zipfile.ZipFile.read

    def spy(self, name, pwd=None):
        unpacked.append(getattr(name, "filename", name))
        return real(self, name, pwd)

    monkeypatch.setattr(zipfile.ZipFile, "read", spy)
    archive = _zip({"file_1.pdf": _pdf(1000, "z1"), "file_2.pdf": _pdf(1000, "z2")})

    out = _upload(
        api,
        (f"{ORDER_ID}-first.pdf", _pdf(1000, "first")),
        (f"{ORDER_ID}.zip", archive),  # declares 2000 with 1500 left
        (f"{ORDER_ID}-last.pdf", _pdf(10, "last")),
    )

    assert _answers(out) == [
        (f"{ORDER_ID}-first.pdf", "stored"),
        (f"{ORDER_ID}.zip", "too_large"),
        (f"{ORDER_ID}-last.pdf", "too_large"),
    ]
    assert unpacked == []
    assert db.query(Invoice).count() == 1


def test_a_zip_member_refused_as_too_large_takes_no_room(api, db, order, monkeypatch):
    monkeypatch.setattr(invoices, "MAX_PDF_BYTES", 1500)
    monkeypatch.setattr(invoices_api, "MAX_UPLOAD_BYTES", 2500)
    archive = _zip({"big.pdf": _pdf(3000, "big"), "file_1.pdf": _pdf(1000, "z1")})

    out = _upload(api, (f"{ORDER_ID}.zip", archive), (f"{ORDER_ID}-loose.pdf", _pdf(1000, "loose")))

    assert _answers(out) == [
        (f"{ORDER_ID}.zip/big.pdf", "too_large"),
        (f"{ORDER_ID}.zip/file_1.pdf", "stored"),
        (f"{ORDER_ID}-loose.pdf", "stored"),  # 1000 + 1000 of 2500
    ]


# --- the caps from before, unchanged -----------------------------------------------------------
def test_the_zip_caps_keep_their_messages(api, db, order, reads, monkeypatch):
    monkeypatch.setattr(invoices_api, "MAX_ZIP_BYTES", 1 * MB)
    too_heavy = _zip({"file_1.pdf": b"%PDF-1.4\n" + os.urandom(MB + 100)}, stored=True)  # random: does not deflate
    unpacks_big = _zip({"file_1.pdf": _pdf(MB + 100, "padding")})  # deflates to a few KB

    out = _upload(api, ("heavy.zip", too_heavy), ("big.zip", unpacks_big), (f"{ORDER_ID}.pdf", _pdf(500, "ok")))

    assert [(entry["status"], entry["reason"]) for entry in out["files"][:2]] == [
        ("too_large", "larger than 1 MB"),
        ("too_large", "the zip unpacks to more than 1 MB"),
    ]
    # The small file goes through. Its made-up bytes are no PDF the reader can
    # read, and the answer says so with the reader's reason (NFR-009).
    assert (out["files"][2]["status"], out["files"][2]["reading"]) == ("stored", "not_read")
    assert out["files"][2]["reason"].startswith("could not read the PDF")
    heavy_reads = [asked for name, asked, _got in reads if name == "heavy.zip"]
    assert heavy_reads == [4, MB + 1]


def test_too_many_files_is_still_a_413(api, order, monkeypatch):
    monkeypatch.setattr(invoices_api, "MAX_FILES", 2)
    response = api.post(
        "/invoices/upload", files=[("files", (f"{n}.pdf", _pdf(100, str(n)), "application/pdf")) for n in range(3)]
    )
    assert response.status_code == 413
    assert "at most 2 files" in response.json()["detail"]
