"""An account sheet, or a sheet of order ids, never reaches disk on its way in
(final review, 27 Oct 2026).

Either can be the owner's workbook in the legacy layout, whose columns T to W
hold every account's full card number, expiry and CVV; the workbook of every
account (~37,000) is about 2 MB. Both routes took it as ``UploadFile =
File(...)``, so Starlette's form reader, which FastAPI runs before the
handler, wrote it to a temp file once it passed 1 MiB. They now read the form
themselves, in memory (app.api.uploads), and CardUploadLimit refuses, unread,
a sheet upload over 25 MiB plus the form's allowance, or one that declares no
size. Every card value here is made up.
"""

from __future__ import annotations

import io
import random
import tempfile

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from starlette import formparsers

from app.api import jobs as jobs_api
from app.api import uploads
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order
from app.io import excel, order_ids
from app.main import app
from tests.test_w3b_card_upload_limit import _through

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SHEET_ROUTES = sorted(jobs_api.SHEET_UPLOAD_PATHS)
#: The most a sheet upload may declare: the sheet at its cap, and the framing.
LIMIT = jobs_api.MAX_SHEET_FILE_BYTES + jobs_api.FORM_FIELDS_ALLOWANCE
#: Where Starlette's usual form reader moves a file part to a temp file on disk.
USUAL_SPOOL = formparsers.MultiPartParser.spool_max_size
#: The owner's accounts, about.
ACCOUNTS = 37_000


@pytest.fixture
def api() -> TestClient:
    """The whole app, its middleware included, signed in."""
    return TestClient(app, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})


@pytest.fixture
def spools(monkeypatch) -> dict[str, int]:
    """Each file part the form reader held in a spooled file ("made"), and
    each of those it moved to a temp file on disk ("disk")."""
    seen = {"made": 0, "disk": 0}

    class Watched(tempfile.SpooledTemporaryFile):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen["made"] += 1

        def rollover(self):
            if not getattr(self, "_rolled", False):
                seen["disk"] += 1
            super().rollover()

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", Watched)
    return seen


@pytest.fixture
def reads(monkeypatch, spools) -> dict[str, dict[str, int]]:
    """How often a sheet route's handler began reading its form and a
    multipart form was parsed at all ("seen"), and the spooled files ("spools")."""
    seen = {"handler": 0, "form": 0}
    read_sheet = uploads.read_sheet
    parse_form = formparsers.MultiPartParser.parse

    async def counted_sheet(request, *args, **kwargs):
        seen["handler"] += 1
        return await read_sheet(request, *args, **kwargs)

    async def counted_form(self):
        seen["form"] += 1
        return await parse_form(self)

    monkeypatch.setattr(uploads, "read_sheet", counted_sheet)
    monkeypatch.setattr(formparsers.MultiPartParser, "parse", counted_form)
    return {"seen": seen, "spools": spools}


def _legacy_workbook(rows: int) -> bytes:
    """An account workbook in the legacy layout: C email, D phone, E status,
    and in T to W each account's card number, expiry month and year, and CVV,
    all made up (the numbers start 9999)."""
    rnd = random.Random(rows)
    wb = Workbook(write_only=True)
    sheet = wb.create_sheet()
    header = [None] * 23
    header[excel.COL_EMAIL - 1], header[excel.COL_PHONE - 1], header[excel.COL_STATUS - 1] = "Email", "Phone", "Status"
    header[19:23] = ["Card Number", "Month", "Year", "CVV"]
    sheet.append(header)
    for i in range(rows):
        row = [None] * 23
        row[excel.COL_EMAIL - 1] = f"acct{i:05d}@example.invalid"
        row[excel.COL_PHONE - 1] = f"9{rnd.randrange(10**9):09d}"
        row[excel.COL_STATUS - 1] = "Done"
        row[19:23] = [
            f"9999{rnd.randrange(10**12):012d}",
            f"{rnd.randint(1, 12):02d}",
            str(rnd.randint(2027, 2099)),
            f"{rnd.randrange(1000):03d}",
        ]
        sheet.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def every_account() -> bytes:
    """The workbook of every account, built once for the module."""
    return _legacy_workbook(ACCOUNTS)


def _multipart(name: str, data: bytes, field: str = "file") -> bytes:
    """A form upload's body, as a browser frames it, with boundary "x"."""
    head = f'--x\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\nContent-Type: {_XLSX_MIME}\r\n\r\n'
    return head.encode() + data + b"\r\n--x--\r\n"


_FORM = {"Content-Type": "multipart/form-data; boundary=x"}


# --- kept in memory -----------------------------------------------------------------


def test_the_workbook_of_every_account_is_imported_from_memory(api, spools, every_account) -> None:
    """The legacy workbook of every account, card columns and all, is over the
    1 MiB at which Starlette's usual reader moves a file part to disk. It is
    imported as before, and none of it went to disk."""
    assert len(every_account) > USUAL_SPOOL

    response = api.post("/accounts/import", files={"file": ("accounts.xlsx", every_account, _XLSX_MIME)})

    assert response.status_code == 200, response.text
    assert response.json() == {"created": ACCOUNTS, "updated": 0, "skipped": 0, "rejected": []}
    assert spools == {"made": 1, "disk": 0}


def test_the_same_workbook_handed_in_as_order_ids_is_refused_from_memory(api, spools, every_account) -> None:
    """The sheet of order ids is read by its headers and leaves the card
    columns unread, so the same workbook is a natural upload there too. It is
    refused for its length in the reader's own words, as before, and none of
    it went to disk."""
    response = api.post("/orders/import-ids", files={"file": ("accounts.xlsx", every_account, _XLSX_MIME)})

    assert response.status_code == 422
    assert response.json() == {
        "detail": f"the sheet has {ACCOUNTS:,} rows; at most {order_ids.MAX_ROWS:,} are accepted"
    }
    assert spools == {"made": 1, "disk": 0}


def test_a_large_sheet_of_order_ids_is_imported_from_memory(api, db, spools) -> None:
    """A sheet of order ids over 1 MiB: the legacy layout saved as CSV, long
    remarks and made-up card columns included. Each row becomes an order to
    track, as before, and none of it went to disk."""
    owners = [Account(email=f"ids{i}@test.com", status=AccountStatus.session_valid) for i in range(3)]
    db.add_all(owners)
    db.commit()
    rnd = random.Random(2200)
    lines = ["Email,Order ID,Qty,Remark,Card Number,Month,Year,CVV"]
    ids = [f"OD{4_000_000_000_000_000 + n:016d}" for n in range(2_200)]
    for n, order_id in enumerate(ids):
        remark = " ".join(rnd.choice(["checked", "by", "the", "team", "on", "call"]) for _ in range(100))
        lines.append(f"ids{n % 3}@test.com,{order_id},1,{remark},9999{rnd.randrange(10**12):012d},07,2099,{rnd.randrange(1000):03d}")
    sheet = ("\n".join(lines) + "\n").encode()
    assert len(sheet) > USUAL_SPOOL

    response = api.post("/orders/import-ids", files={"file": ("ids.csv", sheet, "text/csv")})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["created"], body["existing"], body["rejected"]) == (len(ids), 0, [])
    assert body["order_ids"] == ids and body["account_ids"] == sorted(owner.id for owner in owners)
    assert spools == {"made": 1, "disk": 0}


def test_a_body_of_exactly_the_limit_stays_in_memory(api, spools) -> None:
    """The largest body the guard lets through, almost all of it one file
    part: still held in memory, and refused as no workbook, as before."""
    framing = len(_multipart("big.xlsx", b""))
    body = _multipart("big.xlsx", b"\0" * (LIMIT - framing))
    assert len(body) == LIMIT

    response = api.post("/accounts/import", content=body, headers=_FORM)

    assert response.status_code == 422
    assert response.json()["detail"].startswith("Could not read the file")
    assert spools == {"made": 1, "disk": 0}


def test_the_reader_holds_in_memory_all_the_guard_lets_through() -> None:
    """The guard's limit for each sheet route is at most what the reader
    keeps in memory, so no file part of a body it passes on can go to disk;
    and that is past the usual reader's 1 MiB, which the workbook of every
    account outgrows."""
    for route in SHEET_ROUTES:
        assert jobs_api.UPLOAD_LIMITS[route].most <= uploads.InMemoryForm.spool_max_size
    assert uploads.InMemoryForm.spool_max_size > USUAL_SPOOL


def test_an_account_sheet_of_the_usual_size_is_imported_as_before(api, db, spools) -> None:
    response = api.post("/accounts/import", files={"file": ("inputs.xlsx", _legacy_workbook(2), _XLSX_MIME)})

    assert response.status_code == 200, response.text
    assert response.json() == {"created": 2, "updated": 0, "skipped": 0, "rejected": []}
    assert db.query(Account.email).order_by(Account.email).all() == [
        ("acct00000@example.invalid",),
        ("acct00001@example.invalid",),
    ]
    assert spools == {"made": 1, "disk": 0}


def test_a_sheet_of_order_ids_of_the_usual_size_is_imported_as_before(api, db, spools) -> None:
    db.add(Account(email="a1@test.com", status=AccountStatus.session_valid))
    db.commit()
    sheet = b"Email,Order ID\na1@test.com,OD1111111111\nnobody@test.com,OD2222222222\n"

    response = api.post("/orders/import-ids", files={"file": ("ids.csv", sheet, "text/csv")})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["created"], body["order_ids"]) == (1, ["OD1111111111"])
    assert body["rejected"] == [{"row": 3, "reason": "no account with the email nobody@test.com"}]
    db.expire_all()
    assert [order.order_id for order in db.query(Order).all()] == ["OD1111111111"]
    assert spools == {"made": 1, "disk": 0}


# --- refused before a byte is read ------------------------------------------------


@pytest.mark.parametrize("route", SHEET_ROUTES)
def test_a_sheet_upload_over_the_limit_is_refused_before_its_body_is_read(api, reads, route) -> None:
    response = api.post(route, content=b"\0" * (LIMIT + 1), headers=_FORM)

    assert response.status_code == 413
    assert response.json() == {"detail": jobs_api.SHEET_FILE_TOO_LARGE}
    assert "sheet" in jobs_api.SHEET_FILE_TOO_LARGE and "card" not in jobs_api.SHEET_FILE_TOO_LARGE.lower()
    assert reads == {"seen": {"handler": 0, "form": 0}, "spools": {"made": 0, "disk": 0}}


@pytest.mark.parametrize("route", SHEET_ROUTES)
def test_a_sheet_upload_that_declares_no_size_is_refused_unread(api, reads, route) -> None:
    """A chunked body's size is known only once it has been read; a browser
    always declares a form's."""

    def chunked():
        yield _multipart("accounts.xlsx", _legacy_workbook(2))

    response = api.post(route, content=chunked(), headers=_FORM)

    assert response.status_code == 411
    assert response.json() == {"detail": "A sheet upload must state its size (Content-Length)."}
    assert reads == {"seen": {"handler": 0, "form": 0}, "spools": {"made": 0, "disk": 0}}


@pytest.mark.parametrize("route", SHEET_ROUTES)
@pytest.mark.parametrize(
    ("length", "status"),
    [(str(LIMIT + 1).encode(), 413), (b"999999999999", 413), (None, 411), (b"12a", 411)],
    ids=["one-byte-over", "huge", "none", "not-a-number"],
)
def test_the_refusal_is_sent_without_asking_for_the_body(route, length, status) -> None:
    reached, sent, asked = _through("POST", route, length)

    assert reached == [] and asked == 0
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == status


@pytest.mark.parametrize("route", SHEET_ROUTES)
@pytest.mark.parametrize(
    "length",
    [str(LIMIT).encode(), str(2 * jobs_api.MAX_CARD_FILE_BYTES).encode(), b"4096", b"0"],
    ids=["at-the-limit", "over-a-card-sheets-cap", "a-sheet", "empty"],
)
def test_a_sheet_upload_within_the_limit_is_passed_on(route, length) -> None:
    """Up to the sheet's own limit, past the card routes' far smaller one:
    the workbook of every account is about 2 MB."""
    reached, sent, asked = _through("POST", route, length)

    assert len(reached) == 1 and sent == [] and asked == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/accounts/import"), ("PUT", "/orders/import-ids"), ("POST", "/accounts/import/x"), ("POST", "/orders/import-ids/x")],
)
@pytest.mark.parametrize("length", [b"999999999999", None], ids=["huge", "none"])
def test_another_method_or_path_beside_a_sheet_route_is_passed_on(method, path, length) -> None:
    reached, sent, asked = _through(method, path, length)

    assert len(reached) == 1 and sent == [] and asked == 0


def test_the_card_routes_keep_their_own_limit() -> None:
    for route in jobs_api.CARD_UPLOAD_PATHS:
        assert jobs_api.UPLOAD_LIMITS[route].most == jobs_api.MAX_CARD_FILE_BYTES + jobs_api.FORM_FIELDS_ALLOWANCE
        assert jobs_api.UPLOAD_LIMITS[route].too_large == jobs_api.CARD_FILE_TOO_LARGE


# --- what the reader answers for a form it cannot use ------------------------------


@pytest.mark.parametrize("route", SHEET_ROUTES)
@pytest.mark.parametrize(
    ("headers", "body", "status", "words"),
    [
        ({"Content-Type": "application/json"}, b'{"file": "accounts.xlsx"}', 415, "form upload"),
        ({"Content-Type": "multipart/form-data"}, b"--x--\r\n", 400, "boundary"),
        (_FORM, b"not a form at all", 400, "multipart"),
        (_FORM, b'--x\r\nContent-Disposition: form-data; name="file"\r\n\r\njust text\r\n--x--\r\n', 422, '"file"'),
        (_FORM, _multipart("accounts.xlsx", b"PK\3\4", field="sheet"), 422, '"file"'),
    ],
    ids=["not-a-form", "no-boundary", "broken-form", "text-not-a-file", "another-field"],
)
def test_a_form_without_a_sheet_is_answered_plainly(api, reads, route, headers, body, status, words) -> None:
    response = api.post(route, content=body, headers=headers)

    assert response.status_code == status, response.text
    assert words in response.json()["detail"]
    assert reads["seen"]["handler"] == 1 and reads["spools"]["disk"] == 0
