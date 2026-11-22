"""Which card will pay for which account, shown before a card run starts
(wave 3, package B, 26 Oct 2026).

A card run pairs the sheet's rows with the selected accounts by email. Until
now nothing showed that pairing before Start: a wrong sheet, or one whose
emails do not match, came out only as skipped accounts — or the wrong card.
``POST /jobs/order/card-preview`` reads and pairs the sheet exactly as the run
does and answers per account the card's last four digits and whether its row
carries a bank password: nothing else of any card, ever.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.api import jobs as jobs_api
from app.core.logging_setup import APP_LOGGER, AUDIT_LOGGER
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Address, Job

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: Test values only: 16 digits that no timestamp or id can collide with in a
#: substring scan, a CVV, an expiry and a password that appear nowhere else.
NUMBERS = {"a1@test.com": "4111111111114242", "a2@test.com": "5500000000007777"}
CVV = "839"
PASSWORD = "Bank-Pa55word"


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        async def _noop():
            return None

        return _noop()


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(jobs_api.router)
    return TestClient(application, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})


@pytest.fixture
def accounts(db) -> list[Account]:
    rows = [Account(email=f"a{i}@test.com", status=AccountStatus.session_valid) for i in (1, 2, 3)]
    db.add_all(rows)
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def _sheet(rows: list[list[object]], header=("Email", "Card Number", "Exp Month", "Exp Year", "CVV", "Password")) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    if header:
        sheet.append(list(header))
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def _two_cards() -> bytes:
    return _sheet(
        [
            ["a1@test.com", NUMBERS["a1@test.com"], "08", "2029", CVV, PASSWORD],
            ["a2@test.com", NUMBERS["a2@test.com"], "09", "2030", CVV, ""],
            ["broken@test.com", "4111", "01", "2029", CVV, ""],  # not a card: set aside
        ]
    )


def _preview(api, accounts, sheet: bytes, **fields):
    data = {"account_ids": ",".join(str(account.id) for account in accounts), **fields}
    return api.post("/jobs/order/card-preview", data=data, files={"cards_file": ("cards.xlsx", sheet, _XLSX_MIME)})


def test_each_account_shows_its_cards_last_four_and_whether_a_password_is_there(api, accounts) -> None:
    response = _preview(api, accounts, _two_cards())

    assert response.status_code == 200, response.text
    body = response.json()
    by_email = {row["email"]: row for row in body["accounts"]}
    assert by_email["a1@test.com"]["card_last4"] == "4242" and by_email["a1@test.com"]["has_password"] is True
    assert by_email["a2@test.com"]["card_last4"] == "7777" and by_email["a2@test.com"]["has_password"] is False
    # No card for the third account: the run will skip it.
    assert by_email["a3@test.com"]["card_last4"] is None and by_email["a3@test.com"]["has_password"] is None
    assert (body["paired"], body["without_card"], body["paired_by"]) == (2, 1, "email")
    assert [row["row"] for row in body["rows_set_aside"]] == [4]
    assert body["rows_set_aside"][0]["reason"]


def test_nothing_of_a_card_but_its_last_four_digits_is_in_the_answer(api, accounts) -> None:
    text = _preview(api, accounts, _two_cards()).text

    for number in NUMBERS.values():
        assert number not in text
        assert number[:12] not in text  # not even the part before the last four
    assert CVV not in text
    assert PASSWORD not in text and "Pa55word" not in text
    for expiry in ("2029", "2030"):
        assert expiry not in text
    allowed = {"account_id", "email", "card_last4", "has_password"}
    assert all(set(row) == allowed for row in json.loads(text)["accounts"])


def test_the_preview_pairs_exactly_as_the_run_does(api, accounts, monkeypatch) -> None:
    sheet = _two_cards()
    preview = _preview(api, accounts, sheet).json()
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    started = api.post(
        "/jobs/order",
        data={
            "product_link": "https://www.flipkart.com/some-item/p/itm123",
            "target": "1000",
            "account_ids": ",".join(str(account.id) for account in accounts),
        },
        files={"cards_file": ("cards.xlsx", sheet, _XLSX_MIME)},
    )

    assert started.status_code == 201, started.text
    run_cards = recorder.calls[0]["kwargs"]["cards"]
    shown = {row["account_id"]: row["card_last4"] for row in preview["accounts"] if row["card_last4"]}
    assert {account_id: card.number[-4:] for account_id, card in run_cards.items()} == shown


def test_the_preview_stores_nothing(api, accounts, db) -> None:
    _preview(api, accounts, _two_cards())
    assert db.query(Job).count() == 0


@pytest.mark.parametrize(
    ("sheet", "status", "says"),
    [
        (b"PK\x03\x04 not really a workbook", 422, "Could not read the card file"),
        (_sheet([["x@test.com", "4111", "01", "2029", CVV, ""]]), 422, "No usable card rows in the card file"),
        (_sheet([["nobody@test.com", "4111111111111111", "01", "2029", CVV, ""]]), 422, "emails must match"),
        (
            _sheet([["4111111111111111", "01", "2029", CVV]], header=("Card Number", "Exp Month", "Exp Year", "CVV")),
            422,
            "no email column",
        ),
        (b"x" * (jobs_api.MAX_CARD_FILE_BYTES + 1), 413, "too large"),
    ],
)
def test_it_refuses_what_the_run_refuses(api, accounts, sheet, status, says) -> None:
    response = _preview(api, accounts, sheet)
    assert response.status_code == status
    assert says in response.json()["detail"]


def test_one_account_and_a_sheet_without_emails_pairs_by_position(api, accounts) -> None:
    sheet = _sheet([["4111111111119999", "01", "2029", CVV]], header=("Card Number", "Exp Month", "Exp Year", "CVV"))
    body = _preview(api, accounts[:1], sheet).json()
    assert body["paired_by"] == "position"
    assert body["accounts"] == [{"account_id": accounts[0].id, "email": "a1@test.com", "card_last4": "9999", "has_password": False}]


def test_a_file_is_required_and_a_sign_in_too(api, accounts) -> None:
    response = api.post("/jobs/order/card-preview", data={"account_ids": str(accounts[0].id)})
    assert response.status_code == 422
    assert "corporate-card file is required" in response.json()["detail"]
    anonymous = TestClient(api.app)
    assert anonymous.post("/jobs/order/card-preview", data={"account_ids": "1"}).status_code == 401


def test_an_empty_selection_is_refused_before_the_file_is_read(api, accounts, monkeypatch) -> None:
    """As the run does it. A file that cannot be read, and a spy that only
    counts the parser's calls: read first, the sheet would be the 422 here,
    and the spy would have seen it."""
    calls: list[object] = []
    parse = jobs_api.card_sheets.parse_card_sheet

    def counted(source):
        calls.append(source)
        return parse(source)

    monkeypatch.setattr(jobs_api.card_sheets, "parse_card_sheet", counted)
    response = api.post(
        "/jobs/order/card-preview",
        data={"account_filter": json.dumps({"statuses": ["session_invalid"]})},
        files={"cards_file": ("cards.xlsx", b"PK\x03\x04 not a workbook", _XLSX_MIME)},
    )
    assert (response.status_code, len(calls)) == (400, 0)


def _start(api, accounts, sheet: bytes):
    """A card run over ``accounts`` paid from ``sheet``."""
    return api.post(
        "/jobs/order",
        data={
            "product_link": "https://www.flipkart.com/some-item/p/itm123",
            "target": "1000",
            "account_ids": ",".join(str(account.id) for account in accounts),
        },
        files={"cards_file": ("cards.xlsx", sheet, _XLSX_MIME)},
    )


def test_nothing_of_a_card_is_logged_by_the_preview_or_the_run(api, accounts, caplog, capsys, monkeypatch) -> None:
    """Counts and row numbers only, at every level. Each record's own message
    is scanned, never the formatted lines: they carry a file's line number,
    which a three-digit CVV can equal, and stderr's carry a date, which holds
    an expiry year in that year. So stderr is searched for the long values."""
    caplog.set_level(logging.DEBUG)
    # The app's loggers carry levels of their own (app.core.logging_setup).
    caplog.set_level(logging.DEBUG, logger=APP_LOGGER)
    caplog.set_level(logging.DEBUG, logger=AUDIT_LOGGER)
    monkeypatch.setattr(jobs_api, "run_order_job", _Recorder())

    assert _preview(api, accounts, _two_cards()).status_code == 200
    started = _start(api, accounts, _two_cards())
    assert started.status_code == 201, started.text

    # The card parser's own lines were caught, so the scan below reads them.
    assert any(record.name == "ordering.cards" for record in caplog.records)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    out, err = capsys.readouterr()
    long_values = [PASSWORD, "Pa55word", *NUMBERS.values(), *(number[:12] for number in NUMBERS.values())]
    for value in long_values:
        assert value not in messages and value not in out + err
    for value in (CVV, "2029", "2030"):
        assert value not in messages


def _recell(sheet: bytes, ref: str, cell: str) -> bytes:
    """The workbook with cell ``ref`` written as ``cell``, raw XML: what a tool
    other than Excel can write, a value whose text does not fit its type."""
    source = zipfile.ZipFile(io.BytesIO(sheet))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data, found = re.subn(rf'<c r="{ref}"[^>]*>.*?</c>'.encode(), cell.encode(), data, count=1)
                assert found == 1, f"no cell {ref} in the sheet"
            target.writestr(item, data)
    return out.getvalue()


def _bare(text: str) -> str:
    """Letters and digits only: a value with its spaces and dashes taken out."""
    return re.sub(r"[\W_]+", "", text)


@pytest.mark.parametrize("route", ["preview", "run"])
@pytest.mark.parametrize(
    ("ref", "kind", "value"),
    [
        ("B2", "n", "4111 1111 1111 4242"),  # a card number with spaces, typed as a number
        ("B2", "d", "4111-1111-1111-4242"),  # the same, typed as a date
        ("F2", "n", PASSWORD),  # the bank password, typed as a number
    ],
    ids=["spaced-number", "number-as-date", "password"],
)
def test_a_sheet_that_cannot_be_read_is_refused_without_quoting_a_cell(
    api, accounts, db, caplog, monkeypatch, route, ref, kind, value
) -> None:
    """openpyxl's error for such a cell quotes its text ("invalid literal for
    int() with base 10: '4111 1111 …'"). The answer is one fixed sentence and
    the log names the error's type, so neither holds the value, even run
    together; and no run starts."""
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    sheet = _recell(_two_cards(), ref, f'<c r="{ref}" t="{kind}"><v>{value}</v></c>')

    response = _preview(api, accounts, sheet) if route == "preview" else _start(api, accounts, sheet)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("Could not read the card file")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for text in (detail, logged):
        assert value not in text and _bare(value) not in _bare(text)
    assert recorder.calls == [] and db.query(Job).count() == 0
