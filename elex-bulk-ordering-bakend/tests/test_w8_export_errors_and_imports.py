"""Review of 27 Oct 2026, the part the agent limit cut off.

A streamed export that fails part-way must reach the browser broken off, not
as a whole, shorter file: Starlette's BaseHTTPMiddleware, which the timing
middleware was, ended such an answer cleanly, so the page said "CSV
downloaded" over a file missing its last rows. And the account import names
the rows it refused, and reads the workbook off the event loop.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.api import accounts as accounts_api
from app.core.security import create_access_token
from app.db.models import Account
from app.io import excel, orders_csv


async def _drive(app, path: str, token: str) -> tuple[list[dict], BaseException | None]:
    """One GET through the app at the ASGI level, as uvicorn calls it: every
    message the app sends, and what it raised once it was done sending."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"authorization", f"Bearer {token}".encode())],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    sent: list[dict] = []
    asked = False

    async def receive() -> dict:
        # The request once; then, as a connected browser, nothing more: a
        # receive that answered at once would spin the response's disconnect
        # listener forever.
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    raised: BaseException | None = None
    try:
        await app(scope, receive, send)
    except Exception as exc:  # noqa: BLE001 — what the server would see
        raised = exc
    return sent, raised


def test_an_export_that_fails_part_way_is_left_unfinished(db, admin_token, monkeypatch):
    from app.main import create_app

    def failing(rows, *, sheet=orders_csv.FULL_LEDGER):
        yield "﻿Unit ID,Status\r\n"
        yield "439123456789012345601,Shipped\r\n"
        raise RuntimeError("the database went away")

    monkeypatch.setattr(orders_csv, "stream", failing)
    sent, raised = asyncio.run(_drive(create_app(), "/orders/export.csv", admin_token))

    start = sent[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    assert any(name == b"server-timing" for name, _value in start["headers"])  # still timed
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert b"".join(m.get("body", b"") for m in bodies).endswith(b"Shipped\r\n")
    # Never finished: the server cuts the connection and the browser sees the
    # download broken off, which the page says in plain words.
    assert all(m.get("more_body", False) for m in bodies)
    assert isinstance(raised, RuntimeError)


def test_a_whole_export_still_finishes_and_is_timed(db, admin_token):
    from app.main import create_app

    sent, raised = asyncio.run(_drive(create_app(), "/orders/export.csv", admin_token))
    assert raised is None
    assert sent[0]["status"] == 200
    assert any(name == b"server-timing" for name, _value in sent[0]["headers"])
    assert sent[-1]["type"] == "http.response.body" and sent[-1].get("more_body", False) is False


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(accounts_api.router)
    token = create_access_token("ops@test.com")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def _workbook(emails: list[str]) -> bytes:
    book = Workbook()
    sheet = book.active
    for offset, email in enumerate(emails):
        sheet.cell(row=excel.FIRST_DATA_ROW + offset, column=excel.COL_EMAIL, value=email)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _post(api, data: bytes):
    return api.post(
        "/accounts/import",
        files={"file": ("accounts.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def test_the_account_import_names_the_rows_it_refused(api, db):
    response = _post(api, _workbook(["good@example.com", "Ramesh (no email yet)", "also-good@example.com"]))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "created": 2,
        "updated": 0,
        "skipped": 0,
        "rejected": [{"row": excel.FIRST_DATA_ROW + 1, "reason": "not an email address"}],
    }
    assert sorted(a.email for a in db.query(Account).all()) == ["also-good@example.com", "good@example.com"]


def test_the_account_import_reads_the_workbook_off_the_event_loop(api, db, monkeypatch):
    ran_on_the_loop: list[bool] = []

    def fake_import(source, session):
        try:
            asyncio.get_running_loop()
            ran_on_the_loop.append(True)
        except RuntimeError:
            ran_on_the_loop.append(False)
        return {"created": 0, "updated": 0, "skipped": 0, "rejected": []}

    monkeypatch.setattr(excel, "import_accounts", fake_import)
    assert _post(api, _workbook(["good@example.com"])).status_code == 200
    assert ran_on_the_loop == [False]


def test_a_sheet_with_no_email_in_it_is_answered_in_words(api, db):
    """Not "0 created", and not "the file could not be read": it was read, and
    there is nothing in it to import (11 Nov 2026)."""
    book = Workbook()
    book.active.append(["Name", "City"])
    book.active.append(["Ramesh", "Lucknow"])
    buffer = io.BytesIO()
    book.save(buffer)

    response = _post(api, buffer.getvalue())

    assert response.status_code == 422
    assert response.json()["detail"].startswith("No email addresses were found in the sheet.")


def test_adding_an_account_by_hand_does_not_make_a_second_row_in_other_capitals(api, db):
    first = api.post("/accounts", json={"email": "Buyer.One@gmail.com"})
    assert first.status_code in (200, 201), first.text
    assert first.json()["email"] == "buyer.one@gmail.com"

    again = api.post("/accounts", json={"email": "buyer.one@GMAIL.com"})

    assert again.status_code == 409
    assert again.json()["detail"] == "buyer.one@gmail.com is already in Accounts"
