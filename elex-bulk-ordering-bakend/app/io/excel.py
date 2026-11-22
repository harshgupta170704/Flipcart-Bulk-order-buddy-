"""Unified Excel import/export.

Replaces the three divergent ``ExcelHandler`` modules from the old repos with a
single column map and one read/write path. The column layout mirrors the legacy
workbook so existing sheets import unchanged (PLAN.md §1, §5):

    C = Email        F = Model     I = Pin       O = GST name
    D = Phone        G = Qty       J = Amount
    E = Status       H = Color     K = Order ID

Card columns (T/U/V/W) in legacy sheets are intentionally **ignored** — card
details are never imported or stored (PLAN.md §5). Data rows start at row 2.
"""

from __future__ import annotations

import contextlib
import io
import re
import tempfile
from pathlib import Path
from typing import BinaryIO

from openpyxl import Workbook, load_workbook
from openpyxl.cell import WriteOnlyCell
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.db.models import Account, AccountStatus, Order

# --- Column map (1 = column A) ------------------------------------------------
COL_EMAIL = 3  # C
COL_PHONE = 4  # D
COL_STATUS = 5  # E
COL_MODEL = 6  # F
COL_QTY = 7  # G
COL_COLOR = 8  # H
COL_PIN = 9  # I
COL_AMOUNT = 10  # J
COL_ORDER_ID = 11  # K
COL_GST_NAME = 15  # O

FIRST_DATA_ROW = 2

EXPORT_HEADERS = {
    COL_EMAIL: "Email",
    COL_PHONE: "Phone",
    COL_STATUS: "Account Status",
    COL_MODEL: "Model",
    COL_QTY: "Qty",
    COL_COLOR: "Color",
    COL_PIN: "Pin",
    COL_AMOUNT: "Amount",
    COL_ORDER_ID: "Order ID",
    COL_GST_NAME: "GST Name",
}


def _clean(value) -> str | None:
    """Normalize a cell value to a trimmed string or None."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() == "none":
        return None
    return text


class NoAccountsFound(ValueError):
    """The sheet was read but holds no email address anywhere: nothing to
    import, which the page must say rather than answer "0 created"."""


#: How many rows at the top are looked at to find the heading row, or — in a
#: sheet without one — the column the emails are in.
_DETECT_ROWS = 200
#: Columns read per row. The legacy layout ends at W; a sheet's emails are
#: never further right than this.
_MAX_COLUMNS = 40
#: Rows without an email in a row before the sheet is taken as finished: a
#: gap in the list is skipped, but a workbook formatted down to row 1,048,576
#: must not be walked to its end.
_BLANK_RUN = 500


def _heading(value) -> str:
    """A heading cell as compared: lower-case letters only ("E-mail ID" -> "emailid")."""
    return "".join(ch for ch in str(value or "").lower() if ch.isalpha())


def _phone_text(value) -> str | None:
    """A phone cell as stored: Excel hands a number typed without quotes over
    as 9876543210.0, which must not be kept with its ".0"."""
    text = _clean(value)
    if text is None:
        return None
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _columns(sheet) -> tuple[int, int | None, int] | None:
    """``(email column, phone column or None, first data row)`` — 0-based
    columns, 1-based row — or None when the sheet names no email anywhere.

    The heading row decides when there is one: the column whose heading says
    "mail" ("Email", "E-mail ID", "Platform Email") and, for the phone, one
    that says phone, mobile or contact. A sheet with no heading row is read by
    its values: the column holding the most email addresses, from its first
    row. Until 11 Nov 2026 the emails had to be in column C: a sheet with them
    in column A imported nothing and said "0 created", with no hint why.
    """
    counts: dict[int, int] = {}
    first_seen: dict[int, int] = {}
    phones: dict[int, int] = {}
    for row_number, row in enumerate(
        sheet.iter_rows(min_row=1, max_row=_DETECT_ROWS, max_col=_MAX_COLUMNS, values_only=True), start=1
    ):
        headings = [_heading(value) if isinstance(value, str) and "@" not in value else "" for value in row]
        # "Email", "E-mail ID", "Platform Email", "Mail id" — but not a name
        # that merely contains the letters ("Ismail Khan"), and never a row
        # that itself holds an address: that row is data.
        email_at = next(
            (index for index, text in enumerate(headings) if "email" in text or text in ("mail", "mailid", "mailaddress")),
            None,
        )
        holds_address = any(isinstance(value, str) and _EMAIL_RE.match(value.strip().lower()) for value in row)
        # A heading sits above the data: once an address has been read, a
        # later cell that mentions email ("Ramesh (no email yet)") is a note
        # in the list, not a heading.
        if email_at is not None and not holds_address and not counts and row_number <= 20 and len(headings[email_at]) <= 30:
            phone_at = next(
                (
                    index
                    for index, text in enumerate(headings)
                    if index != email_at and any(word in text for word in ("phone", "mobile", "contact"))
                ),
                None,
            )
            if phone_at is None and email_at == COL_EMAIL - 1:
                phone_at = COL_PHONE - 1  # the legacy layout, whatever its heading says
            return email_at, phone_at, row_number + 1
        for index, value in enumerate(row):
            text = _clean(value)
            if text is None:
                continue
            if _EMAIL_RE.match(text.lower()):
                counts[index] = counts.get(index, 0) + 1
                first_seen.setdefault(index, row_number)
            elif re.fullmatch(r"(?:\+?91[\s-]?)?\d{10}(?:\.0)?", text):
                phones[index] = phones.get(index, 0) + 1
    if not counts:
        return None
    email_at = max(counts, key=lambda index: (counts[index], -index))
    if email_at == COL_EMAIL - 1:
        phone_at: int | None = COL_PHONE - 1
    else:
        likely = [index for index in phones if index != email_at and phones[index] * 2 >= counts[email_at]]
        phone_at = min(likely, key=lambda index: abs(index - email_at)) if likely else None
    # No heading row: the data starts where the first email is. A first row
    # that holds words rather than an email is passed over by the reader.
    return email_at, phone_at, first_seen[email_at]


def import_accounts(source: str | BinaryIO, db: DbSession) -> dict:
    """Import accounts from an Excel file (path or file-like).

    Upserts by email, matched without regard to case: existing accounts have
    their phone refreshed, new ones are created. The email and phone columns
    are found by their headings, or by their values in a sheet without
    headings (:func:`_columns`); the legacy layout (email in C, phone in D)
    reads as it always did. A row with no email is skipped, not the end of
    the list; an email given twice is kept once and the second row named.
    Returns a summary dict; raises :class:`NoAccountsFound` when the sheet
    holds no email at all.
    """
    wb = load_workbook(source, read_only=True, data_only=True)
    try:
        sheet = wb.active
        found = _columns(sheet)
        if found is None:
            raise NoAccountsFound("no email address in the sheet")
        email_at, phone_at, first_row = found

        created = 0
        updated = 0
        skipped = 0
        rejected: list[dict] = []
        #: email -> the row it was first read on, and its account when created
        #: here (the session does not flush as it goes, so a second row with
        #: the same email would not find the first in the database).
        seen: dict[str, int] = {}
        blank_run = 0

        for row_number, row in enumerate(
            sheet.iter_rows(min_row=first_row, max_col=_MAX_COLUMNS, values_only=True), start=first_row
        ):
            raw_email = _clean(row[email_at]) if len(row) > email_at else None
            if raw_email is None:
                blank_run += 1
                if blank_run >= _BLANK_RUN:
                    break
                continue
            blank_run = 0
            # An account is an email address, matched without regard to case: a
            # stray word in the column must not become an account, and
            # "A@x.com" must not become a second row beside "a@x.com" (audit F-26).
            email = raw_email.strip().lower()
            if not _EMAIL_RE.match(email):
                rejected.append({"row": row_number, "reason": "not an email address"})
                continue
            if email in seen:
                rejected.append(
                    {"row": row_number, "reason": f"the same email as row {seen[email]} — kept once"}
                )
                continue
            seen[email] = row_number

            phone = _phone_text(row[phone_at]) if phone_at is not None and len(row) > phone_at else None

            account = db.scalar(select(Account).where(func.lower(Account.email) == email))
            if account is None:
                db.add(Account(email=email, phone=phone, status=AccountStatus.new))
                created += 1
            else:
                if phone is not None and account.phone != phone:
                    account.phone = phone
                    updated += 1
                else:
                    skipped += 1

        db.commit()
    finally:
        wb.close()
    return {"created": created, "updated": updated, "skipped": skipped, "rejected": rejected}


#: One address: something, an @, a domain with a dot — enough to tell an email
#: from a name or a note typed into the column.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_formula_text(value) -> bool:
    """Text openpyxl would store as a formula: a string starting with "=".
    Excel runs a stored formula when the file is opened, and a product, a
    seller or a business name comes from Flipkart or a person, never from
    this tool (spec NFR-008, audit F-36; review of 27 Oct 2026). A string
    starting with "+", "-" or "@" is stored as text already."""
    return isinstance(value, str) and value.startswith("=")


def _put(sheet, row: int, column: int, value) -> None:
    """``sheet.cell(row, column, value)``, with formula-like text kept as text."""
    cell = sheet.cell(row=row, column=column, value=value)
    if _is_formula_text(value):
        cell.data_type = "s"


def _write_only(sheet, value):
    """A value for a write-only sheet's row, formula-like text kept as text."""
    if not _is_formula_text(value):
        return value
    cell = WriteOnlyCell(sheet, value=value)
    cell.data_type = "s"
    return cell


def export_accounts(db: DbSession) -> bytes:
    """Export accounts and their latest order into a legacy-layout workbook.

    Returns the ``.xlsx`` file as bytes, ready to stream as a download.
    """
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Sheet1"

    for col, header in EXPORT_HEADERS.items():
        sheet.cell(row=1, column=col, value=header)

    accounts = db.scalars(select(Account).order_by(Account.id)).all()
    for offset, account in enumerate(accounts):
        row = FIRST_DATA_ROW + offset
        _put(sheet, row, COL_EMAIL, account.email)
        _put(sheet, row, COL_PHONE, account.phone)
        _put(sheet, row, COL_STATUS, account.status.value)

        latest = _latest_order(account)
        if latest is not None:
            _put(sheet, row, COL_MODEL, latest.model)
            _put(sheet, row, COL_QTY, latest.qty)
            _put(sheet, row, COL_COLOR, latest.color)
            _put(sheet, row, COL_PIN, latest.pin)
            _put(sheet, row, COL_AMOUNT, latest.amount)
            _put(sheet, row, COL_ORDER_ID, latest.order_id)
            _put(sheet, row, COL_GST_NAME, latest.gst_name)

    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


def _latest_order(account: Account) -> Order | None:
    if not account.orders:
        return None
    return max(account.orders, key=lambda o: o.id)


# --- Orders export (one row per order) ---------------------------------------
ORDER_EXPORT_HEADERS = [
    "Email",
    "Product Link",
    "Qty",
    "Model",
    "Color",
    "Pin",
    "Amount",
    "Target",
    "Order ID",
    "Unit No",
    "Unit ID",
    "GST Name",
    "Status",
    "Placed At (IST)",
    # The card the order was paid with, in full (the owner's decision of
    # 25 Oct 2026); blank for Cash on Delivery.
    "Card Number",
]


def _naive(value):
    """Drop the tzinfo openpyxl refuses to write.

    Called on the IST wall clock (``orders_csv.to_ist``), so the cell reads
    the same date and time as the CSV export beside it — not the stored UTC
    clock, which put an evening order on the previous calendar day.
    """
    if value is None or getattr(value, "tzinfo", None) is None:
        return value
    return value.replace(tzinfo=None)


def export_orders(db: DbSession, flt=None) -> Path:
    """Export the matching orders, newest first, as a flat results sheet.

    Separate from :func:`export_accounts`, which keeps the legacy
    one-row-per-account layout; this is the per-run results table the Ordering
    screen offers as a download (PLAN.md §6). Status is the ledger page's
    Outcome as a value (app.core.cancellations.outcome_value): the stored
    status's *value*, so an over-target row reads ``failed_over_target``
    (PLAN.md §6a) — except for an order Flipkart took and then cancelled,
    stored as failed too, which reads ``cancelled_by_flipkart`` or
    ``cancelled_by_user``. "failed" is only ever a run that sent nothing.

    ``flt`` is the same :class:`app.core.order_filter.OrderFilter` the ledger
    page and the CSV use, so this file holds exactly the rows the screen
    counted; with none, every order.

    Nothing of the file is held in memory, however many rows match (C-9: no
    cap, and nothing held). The rows stream from the database; openpyxl's
    write-only mode already writes the sheet to a temporary file of its own
    while they are appended; and the finished workbook is saved into a
    temporary file too — in the system's temp folder, readable by the
    server's own user only — whose path is returned. The caller sends it and
    deletes it: app.api.orders streams it as a FileResponse that removes it
    afterwards, the client gone or not. The file holds the card number the
    database already stores (FR-004, as the owner decided on 25 Oct 2026),
    never a CVV or an expiry: those are never stored, so none can be
    exported. An export that fails part-way leaves neither file behind.
    """
    from app.core import cancellations, order_filter  # local: keeps io free of a core import cycle
    from app.io import orders_csv

    # Write-only: rows go to disk-backed storage as they are appended, so an
    # export of every order (there is no cap) does not hold the workbook in
    # memory. The rows themselves stream from the database the same way.
    wb = Workbook(write_only=True)
    sheet = wb.create_sheet("Orders")
    try:
        sheet.append(ORDER_EXPORT_HEADERS)

        # One line per unit, the order's columns repeated, with the unit's number
        # and id so a three-unit order is three distinguishable lines, as on screen.
        for row in order_filter.export_rows(db, flt or order_filter.OrderFilter()):
            unit, order = row.unit, row.order
            values = [
                row.email,
                order.product_link,
                order.qty,
                order.model,
                order.color,
                order.pin,
                order.amount,
                order.target,
                order.order_id,
                unit.position,
                unit.unit_id,
                order.gst_name,
                cancellations.outcome_value(order, unit, requested=row.cancel_requested, reading=row.order_reading)
                or None,
                _naive(orders_csv.to_ist(order.placed_at)) if order.placed_at else None,
                order.card_number,
            ]
            sheet.append([_write_only(sheet, value) for value in values])

        # NamedTemporaryFile opens the file for its owner alone (mode 0600).
        handle = tempfile.NamedTemporaryFile(prefix="orders-export-", suffix=".xlsx", delete=False)
    except BaseException:
        _discard_sheet_file(sheet)
        raise
    path = Path(handle.name)
    try:
        with handle:
            wb.save(handle)
    except BaseException:
        _discard_sheet_file(sheet)
        path.unlink(missing_ok=True)
        raise
    return path


def _discard_sheet_file(sheet) -> None:
    """Remove the temporary file a write-only sheet writes its rows to, after
    an export that failed before it was saved. openpyxl removes it itself only
    once the workbook is saved, or when the process ends — and it holds every
    row written so far, card numbers included."""
    # Closed first: the sheet's row writer holds the file open, and one left
    # to the garbage collector would write to it after it is gone.
    with contextlib.suppress(Exception):
        sheet.close()
    writer = getattr(sheet, "_writer", None)
    if writer is not None:
        with contextlib.suppress(OSError, ValueError):
            writer.cleanup()
