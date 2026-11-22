"""A card upload too big to be a card sheet is refused before a byte of it is
read (card-preview review, 26 Oct 2026).

Starlette writes a multipart file part over 1 MiB to a temp file on disk while
it reads the form, before any dependency or handler runs, so the handler's own
413 came after the card rows were already on disk. ``CardUploadLimit`` answers
first, from the size the request declares: 413 over the sheet's cap plus room
for the other fields, 411 when it declares none. It looks at a POST to the two
card routes, and to the two sheet routes with a cap of their own
(tests/test_sheet_uploads_in_memory.py), only.
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest
from fastapi.testclient import TestClient
from starlette.formparsers import MultiPartParser

from app.api import jobs as jobs_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Address
from app.main import app
from tests.test_w3b_card_preview import _XLSX_MIME, _Recorder, _two_cards

#: The most a card upload may declare: the sheet at its cap, and the rest.
LIMIT = jobs_api.MAX_CARD_FILE_BYTES + jobs_api.FORM_FIELDS_ALLOWANCE
CARD_ROUTES = sorted(jobs_api.CARD_UPLOAD_PATHS)


@pytest.fixture
def api() -> TestClient:
    """The whole app, its middleware included, signed in."""
    return TestClient(app, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})


@pytest.fixture
def accounts(db) -> list[Account]:
    rows = [Account(email=f"a{i}@test.com", status=AccountStatus.session_valid) for i in (1, 2)]
    db.add_all(rows)
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


@pytest.fixture
def reads(monkeypatch) -> dict[str, int]:
    """How often a form was read, a file part went to a temp file on disk, and
    a card sheet was parsed."""
    seen = {"form": 0, "disk": 0, "sheet": 0}
    parse_form = MultiPartParser.parse
    rollover = tempfile.SpooledTemporaryFile.rollover
    parse_sheet = jobs_api.card_sheets.parse_card_sheet

    async def counted_form(self):
        seen["form"] += 1
        return await parse_form(self)

    def counted_rollover(self):
        seen["disk"] += 1
        return rollover(self)

    def counted_sheet(source):
        seen["sheet"] += 1
        return parse_sheet(source)

    monkeypatch.setattr(MultiPartParser, "parse", counted_form)
    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", counted_rollover)
    monkeypatch.setattr(jobs_api.card_sheets, "parse_card_sheet", counted_sheet)
    return seen


def _fields(route: str, accounts) -> dict[str, str]:
    """The form fields that go with the sheet on ``route``."""
    fields = {"account_ids": ",".join(str(account.id) for account in accounts)}
    if route == "/jobs/order":
        fields |= {"product_link": "https://www.flipkart.com/some-item/p/itm123", "target": "1000"}
    return fields


# --- through the app ------------------------------------------------------------


@pytest.mark.parametrize("route", CARD_ROUTES)
def test_an_upload_over_the_limit_is_refused_before_its_body_is_read(api, accounts, reads, route) -> None:
    """Without the middleware this was a 413 too, from the handler, but only
    after the form had been read and the file part written to disk."""
    sheet = b"x" * LIMIT  # the fields and the framing take the body over

    response = api.post(route, data=_fields(route, accounts), files={"cards_file": ("cards.xlsx", sheet, _XLSX_MIME)})

    assert response.status_code == 413
    assert "too large" in response.json()["detail"]
    assert reads == {"form": 0, "disk": 0, "sheet": 0}


@pytest.mark.parametrize("route", CARD_ROUTES)
def test_an_upload_that_declares_no_size_is_refused_unread(api, accounts, reads, route) -> None:
    """A chunked body's size is known only once it has been read; a browser
    always declares a form's."""

    def chunked():
        yield b'--x\r\nContent-Disposition: form-data; name="cards_file"; filename="cards.xlsx"\r\n\r\n'
        yield _two_cards()
        yield b"\r\n--x--\r\n"

    response = api.post(route, content=chunked(), headers={"Content-Type": "multipart/form-data; boundary=x"})

    assert response.status_code == 411
    assert reads == {"form": 0, "disk": 0, "sheet": 0}


def test_a_card_sheet_of_the_usual_size_is_read_as_before(api, accounts, reads, monkeypatch) -> None:
    monkeypatch.setattr(jobs_api, "run_order_job", _Recorder())
    route = "/jobs/order/card-preview"
    preview = api.post(route, data=_fields(route, accounts), files={"cards_file": ("cards.xlsx", _two_cards(), _XLSX_MIME)})
    route = "/jobs/order"
    started = api.post(route, data=_fields(route, accounts), files={"cards_file": ("cards.xlsx", _two_cards(), _XLSX_MIME)})

    assert preview.status_code == 200, preview.text
    assert [row["card_last4"] for row in preview.json()["accounts"]] == ["4242", "7777"]
    assert started.status_code == 201, started.text
    assert reads == {"form": 2, "disk": 0, "sheet": 2}


def test_another_route_is_not_looked_at(api) -> None:
    """The same chunked body, with no size declared, on a start route that is
    not a card route: the handler answers it, as it always has."""

    def chunked():
        yield b"{}"

    response = api.post("/jobs/login", content=chunked(), headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    assert "No accounts to run" in response.json()["detail"]


# --- the middleware on its own ----------------------------------------------------


def _through(method: str, path: str, length: bytes | None) -> tuple[list, list, int]:
    """Send one request through CardUploadLimit in front of an app that only
    notes it was reached. Returns what reached it, what was sent back, and how
    often the body was asked for."""
    reached: list = []
    sent: list = []
    asked = 0

    async def inner(scope, receive, send):
        reached.append((scope, receive, send))

    async def receive():
        nonlocal asked
        asked += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    headers = [(b"content-type", b"multipart/form-data; boundary=x")]
    if length is not None:
        headers.append((b"content-length", length))
    scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": headers}
    asyncio.run(jobs_api.CardUploadLimit(inner)(scope, receive, send))
    if reached:
        # Passed on as it came: the very same scope, receive and send.
        assert reached == [(scope, receive, send)]
    return reached, sent, asked


@pytest.mark.parametrize("route", CARD_ROUTES)
@pytest.mark.parametrize(
    ("length", "status"),
    [(str(LIMIT + 1).encode(), 413), (b"999999999999", 413), (None, 411), (b"12a", 411)],
    ids=["one-byte-over", "huge", "none", "not-a-number"],
)
def test_the_refusal_is_sent_without_asking_for_the_body(route, length, status) -> None:
    reached, sent, asked = _through("POST", route, length)

    assert reached == [] and asked == 0
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == status


@pytest.mark.parametrize("route", CARD_ROUTES)
@pytest.mark.parametrize("length", [str(LIMIT).encode(), b"4096", b"0"], ids=["at-the-limit", "a-sheet", "empty"])
def test_a_card_upload_within_the_limit_is_passed_on(route, length) -> None:
    reached, sent, asked = _through("POST", route, length)

    assert len(reached) == 1 and sent == [] and asked == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/accounts"),
        ("POST", "/invoices/upload"),
        ("POST", "/jobs/login"),
        ("POST", "/jobs/order/card-preview/other"),
        ("GET", "/jobs/order"),
        ("PUT", "/jobs/order/card-preview"),
    ],
)
@pytest.mark.parametrize("length", [b"999999999999", None], ids=["huge", "none"])
def test_every_other_request_is_passed_on_untouched(method, path, length) -> None:
    reached, sent, asked = _through(method, path, length)

    assert len(reached) == 1 and sent == [] and asked == 0
