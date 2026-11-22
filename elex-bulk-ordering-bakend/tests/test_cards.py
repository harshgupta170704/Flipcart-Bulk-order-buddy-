"""Tests for corporate-card parsing (app.io.cards) and the orders export.

Every workbook is built in memory — card fixtures must never touch disk
(PLAN.md §5).
"""

import io
import json
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from app.db.models import Account, Order, OrderStatus
from app.io import cards, excel


def _book(rows) -> io.BytesIO:
    """Build a workbook from a list of row lists (row 1 first)."""
    wb = Workbook()
    sheet = wb.active
    for row in rows:
        sheet.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _legacy_book(rows) -> io.BytesIO:
    """Build a header-less legacy sheet: email at C, card at T/U/V/W, data row 2."""
    wb = Workbook()
    sheet = wb.active
    for i, (email, number, month, year, cvv) in enumerate(rows):
        r = cards.FIRST_DATA_ROW + i
        sheet.cell(row=r, column=cards.LEGACY_COL_EMAIL, value=email)
        sheet.cell(row=r, column=cards.LEGACY_COL_NUMBER, value=number)
        sheet.cell(row=r, column=cards.LEGACY_COL_MONTH, value=month)
        sheet.cell(row=r, column=cards.LEGACY_COL_YEAR, value=year)
        sheet.cell(row=r, column=cards.LEGACY_COL_CVV, value=cvv)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# --- parse_cards: header-driven ----------------------------------------------
def test_header_driven_separate_month_year():
    src = _book(
        [
            ["Email ID", "Card Number", "Exp Month", "Expiry Year", "CVV"],
            ["a@test.com", "4111111111111111", "12", "2028", "123"],
            ["b@test.com", "5555444433332222", "07", "29", "456"],
        ]
    )
    parsed = cards.parse_cards(src)

    assert len(parsed) == 2
    assert parsed[0].number == "4111111111111111"
    assert (parsed[0].month, parsed[0].year) == ("12", "2028")
    assert parsed[0].cvv == "123"
    assert parsed[0].email == "a@test.com"
    # A 2-digit year is kept as-is; no century is invented.
    assert (parsed[1].month, parsed[1].year) == ("07", "29")


def _csv(text: str, *, bom: bool = False) -> io.BytesIO:
    raw = text.encode("utf-8")
    return io.BytesIO((b"\xef\xbb\xbf" if bom else b"") + raw)


def test_a_letter_in_front_of_the_card_number_is_dropped():
    """HDFC's virtual corporate cards are listed as "C4111…" on the bank's
    sheet (the owner, 26 Oct 2026); the letter is not part of the number."""
    parsed = cards.parse_cards(
        _csv(
            "Email,Card Number,Expiry,CVV\n"
            "a@test.com,C4111111111111111,09/27,123\n"
            "b@test.com,c 5555-4444-3333-2222,10/28,456\n"
            "d@test.com,CC4111111111111112,11/29,789\n"
        )
    )
    assert [card.number for card in parsed] == ["4111111111111111", "5555444433332222", "4111111111111112"]
    # Letters that are not a prefix of a whole number set the row aside, never a guess.
    sheet = cards.parse_card_sheet(_csv("Email,Card Number,Expiry,CVV\na@test.com,4111C111111111111,09/27,123\n"))
    assert sheet.records == [] and "card number" in sheet.rejected[0]["reason"]


def test_the_banks_card_password_is_read_from_the_sheet_and_never_shown():
    """HDFC's corporate cards ask for a password on the bank's page after Pay
    (the owner, 26 Oct 2026); the sheet carries it under its own heading, and
    it is masked wherever a record is printed."""
    parsed = cards.parse_cards(
        _csv(
            "Email,Card Number,Expiry,CVV,HDFC NetSafe Password\n"
            "a@test.com,4111111111111111,09/27,123,s3cret-pw\n"
            "b@test.com,5555444433332222,10/28,456,\n"
        )
    )
    assert [card.password for card in parsed] == ["s3cret-pw", ""]
    assert "s3cret-pw" not in repr(parsed[0]) and "password='***'" in repr(parsed[0])

    # An exact heading too; a numeric PIN keeps its digits and its zeros.
    parsed = cards.parse_cards(
        _csv("Email,Card Number,Expiry,CVV,Card Password\na@test.com,4111111111111111,09/27,123,004512\n")
    )
    assert parsed[0].password == "004512"
    # A sheet without the column: no password, nothing refused.
    parsed = cards.parse_cards(_csv("Email,Card Number,Expiry,CVV\na@test.com,4111111111111111,09/27,123\n"))
    assert parsed[0].password == ""


# --- the email column must never be dropped silently (review of 26 Oct 2026) ------
def test_an_email_column_under_another_heading_is_still_the_email_column():
    """"Email Address", "Cardholder Email", "Login Email"…: dropping such a
    column would pair cards to accounts by position and charge the wrong card."""
    for heading in ("Email Address", "Cardholder Email", "Login Email", "Account Email ID", "Mail ID", "E-mail"):
        parsed = cards.parse_cards(_csv(f"{heading},Card Number,Expiry,CVV\nb@test.com,4111111111111111,09/27,123\n"))
        assert [card.email for card in parsed] == ["b@test.com"], heading


def test_an_email_column_with_no_telling_heading_is_found_by_its_values():
    parsed = cards.parse_cards(
        _csv("User,Card Number,Expiry,CVV\na@test.com,4111111111111111,09/27,123\nb@test.com,5555444433332222,10/28,456\n")
    )
    assert [card.email for card in parsed] == ["a@test.com", "b@test.com"]
    # No email anywhere: the records carry none, and the run start refuses
    # pairing by position for more than one account.
    parsed = cards.parse_cards(_csv("Card Number,Expiry,CVV\n4111111111111111,09/27,123\n"))
    assert [card.email for card in parsed] == [None]


def test_float_like_csv_cells_are_narrowed_like_a_workbooks_floats():
    """A sheet written by pandas or polars from a numeric column with one blank
    cell prints every number as a float: 4111111111111111.0, 9.0, 2027.0, 123.0."""
    parsed = cards.parse_cards(
        _csv("Email,Card Number,Exp Month,Exp Year,CVV\na@test.com,4111111111111111.0,9.0,2027.0,123.0\n")
    )
    (card,) = parsed
    assert (card.number, card.month, card.year, card.cvv) == ("4111111111111111", "09", "2027", "123")


def test_a_csv_with_bare_carriage_returns_is_read_row_by_row():
    """Excel for Mac's "CSV (Macintosh)" ends every row with a lone CR."""
    parsed = cards.parse_cards(
        _csv("Email,Card Number,Expiry,CVV\ra@test.com,4111111111111111,09/27,123\rb@test.com,5555444433332222,10/28,456\r")
    )
    assert [card.email for card in parsed] == ["a@test.com", "b@test.com"]
    parsed = cards.parse_cards(_csv("Email,Card Number,Expiry,CVV\r\na@test.com,4111111111111111,09/27,123\r\n"))
    assert [card.email for card in parsed] == ["a@test.com"]


@pytest.mark.parametrize(
    ("row", "field"),
    [
        ("a@test.com,41111111,09/27,123", "card number"),
        ("a@test.com,4111111111111111X,09/27,123", "card number"),
        ("a@test.com,4111111111111111,13/27,123", "expiry month"),
        ("a@test.com,4111111111111111,09/202,123", "expiry year"),
        ("a@test.com,4111111111111111,09/27,12", "CVV"),
    ],
)
def test_a_malformed_card_row_is_set_aside_naming_the_row_and_the_field_never_the_value(row, field):
    sheet = cards.parse_card_sheet(_csv(f"Email,Card Number,Expiry,CVV\n{row}\n"))
    assert sheet.records == []
    (rejected,) = sheet.rejected
    assert rejected["row"] == 2 and field in rejected["reason"]
    for value in row.split(",")[1:]:
        assert value not in rejected["reason"]
    assert sheet.skipped_note().startswith("row 2: ")


def test_one_bad_row_in_a_long_sheet_does_not_stop_the_other_cards():
    """The owner's corporate list, 26 Oct 2026: one line with a two-digit CVV
    refused the whole run. Now that line is set aside and named; the others run."""
    sheet = cards.parse_card_sheet(
        _csv(
            "Email,Card Number,Expiry,CVV\n"
            "a@test.com,4111111111111111,09/27,123\n"
            "b@test.com,5555444433332222,10/28,12\n"
            "c@test.com,4111111111111112,11/29,456\n"
        )
    )
    assert [card.email for card in sheet.records] == ["a@test.com", "c@test.com"]
    assert [item["row"] for item in sheet.rejected] == [3]
    assert "CVV" in sheet.rejected[0]["reason"] and "leading zeros" in sheet.rejected[0]["reason"]
    assert cards.parse_cards(_csv("Email,Card Number,Expiry,CVV\na@test.com,4111111111111111,09/27,123\n"))[0].email == "a@test.com"


def test_a_csv_sheet_is_parsed_like_the_workbook():
    """The owner keeps the corporate-card list as a CSV (26 Oct 2026); it is
    told from a workbook by content, not by name, and read with the same
    header rules — a BOM from Excel included."""
    src = _csv(
        "Email ID,Card Number,Exp Month,Expiry Year,CVV\n"
        "a@test.com,4111 1111 1111 1111,12,2028,123\n"
        "b@test.com,5555444433332222,07,29,045\n"
        "\n"
        "ignored@test.com,4000000000000002,01,30,999\n",
        bom=True,
    )
    parsed = cards.parse_cards(src)

    assert [(c.email, c.number, c.month, c.year, c.cvv) for c in parsed] == [
        ("a@test.com", "4111111111111111", "12", "2028", "123"),
        ("b@test.com", "5555444433332222", "07", "29", "045"),  # the blank row ends the sheet
    ]


def test_a_csv_with_a_combined_expiry_and_a_title_row():
    src = _csv("Corporate cards\n\nemail,card,expiry,cvv\nc@test.com,4111111111111111,09/27,321\n")
    parsed = cards.parse_cards(src)
    assert len(parsed) == 1
    assert (parsed[0].month, parsed[0].year, parsed[0].cvv, parsed[0].email) == ("09", "27", "321", "c@test.com")


def test_a_workbook_is_still_told_apart_by_its_signature_not_its_name(tmp_path):
    book = _book([["Email", "Card Number", "Expiry", "CVV"], ["a@test.com", "4111111111111111", "12/28", "123"]])
    assert len(cards.parse_cards(book)) == 1  # a file-like with no name at all
    path = tmp_path / "cards.csv"
    path.write_text("Email,Card Number,Expiry,CVV\na@test.com,4111111111111111,12/28,123\n")
    assert len(cards.parse_cards(str(path))) == 1  # a CSV by path


def test_header_driven_columns_in_any_order_and_casing():
    src = _book(
        [
            ["security code", "CARD NO", "mm", "yyyy", "account email"],
            ["999", "4111111111111111", "3", "2030", "c@test.com"],
        ]
    )
    (record,) = cards.parse_cards(src)

    assert record.number == "4111111111111111"
    assert (record.month, record.year, record.cvv) == ("03", "2030", "999")
    assert record.email == "c@test.com"


def test_header_driven_combined_expiry_cell():
    src = _book(
        [
            ["Email", "Card Number", "Expiry", "CVV"],
            ["a@test.com", "4111111111111111", "12/28", "123"],
            ["b@test.com", "5555444433332222", "01-30", "456"],
            ["c@test.com", "4000000000000002", "12 / 2028", "789"],
        ]
    )
    first, second, third = cards.parse_cards(src)

    assert (first.month, first.year) == ("12", "28")
    assert (second.month, second.year) == ("01", "30")
    assert (third.month, third.year) == ("12", "2028")


def test_phone_number_column_does_not_hijack_the_card_column():
    # "Numbers" must not be read as a PAN; without a corroborating card field
    # the row falls through to the legacy layout and yields nothing.
    src = _book(
        [
            ["Email", "Numbers", "Status"],
            ["a@test.com", "9000000001", "Done"],
        ]
    )
    assert cards.parse_cards(src) == []


# --- parse_cards: legacy fallback -------------------------------------------
def test_legacy_fallback_without_headers():
    src = _legacy_book(
        [
            ("a@test.com", "4111111111111111", "12", "2028", "123"),
            ("b@test.com", "5555444433332222", "07", "29", "456"),
        ]
    )
    parsed = cards.parse_cards(src)

    assert [c.number for c in parsed] == [
        "4111111111111111",
        "5555444433332222",
    ]
    assert [c.email for c in parsed] == ["a@test.com", "b@test.com"]
    assert (parsed[0].month, parsed[0].year, parsed[0].cvv) == ("12", "2028", "123")


# --- parse_cards: normalization ---------------------------------------------
def test_numeric_cells_become_clean_strings():
    # openpyxl hands back ints/floats for unformatted numeric cells.
    src = _book(
        [
            ["Card Number", "Month", "Year", "CVV"],
            [4111111111111111, 1, 2028, 123],
            [4111111111111111.0, 7.0, 2029.0, 456.0],
        ]
    )
    first, second = cards.parse_cards(src)

    for record in (first, second):
        assert record.number == "4111111111111111"
        assert "." not in record.number
        assert "." not in record.year
        assert "." not in record.cvv
    assert (first.month, first.year, first.cvv) == ("01", "2028", "123")
    assert (second.month, second.year, second.cvv) == ("07", "2029", "456")


def test_spaces_dashes_padding_and_leading_zero_cvv():
    src = _book(
        [
            ["Card Number", "Month", "Year", "CVV"],
            ["4111 1111 1111 1111", "1", "28", "012"],
            ["4111-1111-1111-1112", " 9 ", "2031", "007"],
        ]
    )
    first, second = cards.parse_cards(src)

    assert first.number == "4111111111111111"
    assert second.number == "4111111111111112"
    assert first.month == "01"
    assert second.month == "09"
    assert first.cvv == "012"  # leading zero survives
    assert second.cvv == "007"

    # A two-digit CVV cannot be right — a spreadsheet dropped a leading zero
    # and nothing here may guess it back; that row is set aside with the cause
    # and the rest of the sheet is still used.
    sheet = cards.parse_card_sheet(
        _book(
            [
                ["Card Number", "Month", "Year", "CVV"],
                ["4111111111111111", "1", "28", "012"],
                ["4111111111111112", "9", "2031", "07"],
            ]
        )
    )
    assert [card.cvv for card in sheet.records] == ["012"]
    assert sheet.rejected[0]["row"] == 3 and "leading zeros" in sheet.rejected[0]["reason"]


def test_rows_without_a_card_number_are_skipped():
    src = _book(
        [
            ["Email", "Card Number", "Month", "Year", "CVV"],
            ["a@test.com", "4111111111111111", "12", "28", "123"],
            ["b@test.com", None, "12", "28", "456"],  # no card -> skipped
            ["c@test.com", "   ", "12", "28", "789"],  # blank card -> skipped
            ["d@test.com", "5555444433332222", "01", "30", "321"],
        ]
    )
    parsed = cards.parse_cards(src)

    assert [c.email for c in parsed] == ["a@test.com", "d@test.com"]


def test_stops_at_first_fully_empty_row():
    src = _book(
        [
            ["Card Number", "Month", "Year", "CVV"],
            ["4111111111111111", "12", "28", "123"],
            [None, None, None, None],
            ["5555444433332222", "01", "30", "456"],  # below the gap -> ignored
        ]
    )
    parsed = cards.parse_cards(src)

    assert [c.number for c in parsed] == ["4111111111111111"]


def test_missing_expiry_and_cvv_come_back_as_empty_strings():
    src = _book(
        [
            ["Card Number", "CVV"],
            ["4111111111111111", None],
        ]
    )
    (record,) = cards.parse_cards(src)

    assert record.number == "4111111111111111"
    assert (record.month, record.year, record.cvv) == ("", "", "")


# --- masking ----------------------------------------------------------------
def test_masked_and_repr_hide_the_pan_and_cvv():
    record = cards.CardRecord(
        number="4111111111111111",
        month="12",
        year="28",
        cvv="987",
        email="a@test.com",
    )

    assert record.masked() == "**** **** **** 1111"
    for text in (repr(record), str(record), f"{record}", f"{record!r}", f"card={record}"):
        assert "4111111111111111" not in text  # PAN never leaks
        assert "987" not in text  # CVV never leaks
        assert "1111" in text  # last four are fine for support
    assert "a@test.com" in repr(record)


def test_parsed_records_are_masked_in_repr():
    src = _book(
        [
            ["Card Number", "Month", "Year", "CVV"],
            ["4111111111111111", "12", "28", "987"],
        ]
    )
    (record,) = cards.parse_cards(src)

    assert "4111111111111111" not in repr(record)
    assert "987" not in repr(record)


def test_card_record_is_frozen_and_not_logged_anywhere():
    import dataclasses
    import pathlib

    record = cards.CardRecord("4111111111111111", "12", "28", "987")
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.number = "5555444433332222"  # type: ignore[misc]

    source = pathlib.Path(cards.__file__).read_text(encoding="utf-8")
    # Guard the rule itself: no logging call in this module may take a record
    # or a card field as an argument. Only counts are logged.
    for line in source.splitlines():
        if "log." in line and not line.strip().startswith(("#", "*")):
            assert "number" not in line
            assert "cvv" not in line


#: The backend folder, for a child interpreter that imports the app's modules.
BACKEND = Path(__file__).resolve().parents[1]
#: Made up. Kept as a number in a date-styled cell it is far too big to be a
#: date, and openpyxl's warning about that quotes it whole.
DATE_STYLED_NUMBER = 4987654321098765

#: Reads the sheet on stdin in a fresh interpreter, as the server would: card
#: parsing, or the account import (whose database is a stand-in that finds no
#: account). Prints how many warnings quoting the number were recorded, and
#: how many with every warning shown: the proof the sheet does make openpyxl
#: warn. Whatever Python prints itself goes to the child's stderr.
_READ_IN_A_FRESH_INTERPRETER = textwrap.dedent(
    """
    import io, json, sys, warnings

    which, number = sys.argv[1], sys.argv[2]
    data = sys.stdin.buffer.read()
    if which == "cards":
        from app.io.cards import parse_card_sheet as read
    else:
        from app.io.excel import import_accounts

        class NoDatabase:
            def scalar(self, *args):
                return None

            def add(self, *args):
                pass

            def commit(self):
                pass

        def read(source):
            return import_accounts(source, NoDatabase())

    read(io.BytesIO(data))
    with warnings.catch_warnings(record=True) as recorded:
        read(io.BytesIO(data))
    with warnings.catch_warnings(record=True) as control:
        warnings.simplefilter("always")
        read(io.BytesIO(data))
    quoting = lambda caught: sum(number in str(item.message) for item in caught)
    print(json.dumps({"recorded": quoting(recorded), "control": quoting(control)}))
    """
)


@pytest.mark.parametrize("which", ["cards", "excel"], ids=["card sheet", "account import"])
def test_a_card_number_in_a_date_styled_cell_is_never_printed(which):
    """openpyxl warns about a number in a date-styled cell that is too big to
    be a date, quoting the value, and Python prints a warning to stderr: the
    terminal, or the container's log. ``app.io`` ignores that one message from
    the moment either module is imported, for the card sheet and for the
    account import's legacy card columns alike.

    In a child interpreter: pytest runs each test, and imported the app's
    modules, inside warning filters of its own, which leave out a filter set
    at import time and add ones the server never has."""
    workbook = Workbook()
    sheet = workbook.active
    if which == "cards":
        sheet.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV"])
        sheet.append(["a@test.com", DATE_STYLED_NUMBER, "08", "2029", "839"])
        sheet["B2"].number_format = "yyyy-mm-dd"
    else:
        sheet.cell(row=2, column=excel.COL_EMAIL, value="a@test.com")
        sheet.cell(row=2, column=cards.LEGACY_COL_NUMBER, value=DATE_STYLED_NUMBER).number_format = "yyyy-mm-dd"
    buf = io.BytesIO()
    workbook.save(buf)

    number = str(DATE_STYLED_NUMBER)
    done = subprocess.run(
        [sys.executable, "-c", _READ_IN_A_FRESH_INTERPRETER, which, number],
        cwd=BACKEND,
        input=buf.getvalue(),
        capture_output=True,
        timeout=120,
    )
    stderr = done.stderr.decode(errors="replace")
    assert done.returncode == 0, stderr
    counts = json.loads(done.stdout)
    assert counts["control"] >= 1, "the sheet no longer makes openpyxl warn: the test proves nothing"
    assert number not in stderr
    assert counts["recorded"] == 0


# --- map_cards_to_accounts --------------------------------------------------
def _account(db, email: str) -> Account:
    account = Account(email=email)
    db.add(account)
    db.commit()
    return account


def test_map_cards_by_email_case_insensitive(db):
    matched = _account(db, "A@Test.com")
    unmatched = _account(db, "b@test.com")

    parsed = [
        cards.CardRecord("4111111111111111", "12", "28", "123", email="a@TEST.com"),
        cards.CardRecord("5555444433332222", "01", "30", "456", email="z@test.com"),
    ]
    mapping = cards.map_cards_to_accounts(parsed, [matched, unmatched])

    assert set(mapping) == {matched.id}
    assert mapping[matched.id].number == "4111111111111111"
    assert unmatched.id not in mapping  # no card -> the job skips it


def test_map_cards_positional_fallback_when_no_emails(db):
    first = _account(db, "a@test.com")
    second = _account(db, "b@test.com")
    third = _account(db, "c@test.com")

    parsed = [
        cards.CardRecord("4111111111111111", "12", "28", "123"),
        cards.CardRecord("5555444433332222", "01", "30", "456"),
    ]
    mapping = cards.map_cards_to_accounts(parsed, [first, second, third])

    assert mapping[first.id].number == "4111111111111111"
    assert mapping[second.id].number == "5555444433332222"
    assert third.id not in mapping  # more accounts than cards -> extras ignored


def test_map_cards_positional_ignores_extra_cards(db):
    only = _account(db, "a@test.com")

    parsed = [
        cards.CardRecord("4111111111111111", "12", "28", "123"),
        cards.CardRecord("5555444433332222", "01", "30", "456"),
    ]
    mapping = cards.map_cards_to_accounts(parsed, [only])

    assert list(mapping) == [only.id]


def test_map_cards_with_no_cards_is_empty(db):
    account = _account(db, "a@test.com")
    assert cards.map_cards_to_accounts([], [account]) == {}


# --- export_orders ----------------------------------------------------------
def _exported(db) -> Workbook:
    """The workbook export_orders wrote, read back; its temporary file — the
    caller's to delete — removed."""
    path = excel.export_orders(db)
    try:
        return load_workbook(path)
    finally:
        path.unlink()


def test_export_orders_includes_over_target_rows(db):
    account = Account(email="a@test.com", phone="9000000001")
    account.orders.append(
        Order(
            product_link="https://flipkart.com/x",
            qty=1,
            model="Phone X",
            color="Black",
            pin="122016",
            amount=999.99,
            target=1000.0,
            order_id="OD123",
            gst_name="Acme",
            status=OrderStatus.placed,
            placed_at=datetime.now(timezone.utc),
        )
    )
    account.orders.append(
        Order(
            product_link="https://flipkart.com/y",
            qty=1,
            model="Phone Y",
            color="Blue",
            pin="122016",
            amount=1000.01,
            target=1000.0,
            gst_name="Acme",
            status=OrderStatus.over_target,  # never placed, PLAN.md §6a
        )
    )
    db.add(account)
    db.commit()

    wb = _exported(db)
    sheet = wb.active

    assert sheet.title == "Orders"
    headers = [cell.value for cell in sheet[1]]
    assert headers == excel.ORDER_EXPORT_HEADERS
    assert headers[:3] == ["Email", "Product Link", "Qty"]
    assert headers[-3:] == ["Status", "Placed At (IST)", "Card Number"]

    rows = list(sheet.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 2

    by_status = {row[headers.index("Status")]: row for row in rows}
    assert set(by_status) == {"placed", "failed_over_target"}

    over = by_status["failed_over_target"]
    assert over[headers.index("Email")] == "a@test.com"
    assert over[headers.index("Amount")] == 1000.01
    assert over[headers.index("Target")] == 1000.0
    assert over[headers.index("Order ID")] is None  # not placed -> no order id
    assert over[headers.index("Placed At (IST)")] is None

    placed = by_status["placed"]
    assert placed[headers.index("Amount")] == 999.99
    assert placed[headers.index("Order ID")] == "OD123"
    assert placed[headers.index("Placed At (IST)")] is not None


def test_export_orders_newest_first(db):
    account = Account(email="a@test.com")
    for order_id in ("OD1", "OD2", "OD3"):
        account.orders.append(Order(order_id=order_id, status=OrderStatus.placed))
    db.add(account)
    db.commit()

    wb = _exported(db)
    sheet = wb.active
    headers = [cell.value for cell in sheet[1]]
    column = headers.index("Order ID")
    ids = [row[column] for row in sheet.iter_rows(min_row=2, values_only=True)]

    assert ids == ["OD3", "OD2", "OD1"]


def test_export_orders_empty_db_has_headers_only(db):
    wb = _exported(db)
    sheet = wb.active

    assert [cell.value for cell in sheet[1]] == excel.ORDER_EXPORT_HEADERS
    assert sheet.max_row == 1
