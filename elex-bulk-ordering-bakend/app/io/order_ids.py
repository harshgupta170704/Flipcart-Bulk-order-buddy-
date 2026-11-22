"""A list of order ids to track, read from a CSV or XLSX the operator uploads.

The operator's own process (``docs/specs/order-unit-ledger/inputs``,
"Order Status Automation") starts from an Excel of order ids: the tool logs
into the accounts, reads each order back and reports per unit. This reader
turns that sheet into rows the import endpoint can act on. It is header-driven
— columns are found by name, in any order, with spaces, hyphens and
underscores interchangeable and case ignored — because the sheet is typed or
exported by hand and will not always be laid out the same way.

Required: the account's email on the platform and the order id. Optional:
how many units the order has (one when absent) and a note. Anything else in
the sheet is ignored.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import BinaryIO

#: Header spellings, normalised (lower case, separators removed) → field.
_HEADER_ALIASES: dict[str, str] = {
    "platformemail": "email",
    "email": "email",
    "emailid": "email",
    "account": "email",
    "accountemail": "email",
    "orderid": "order_id",
    "order": "order_id",
    "orderno": "order_id",
    "ordernumber": "order_id",
    "qty": "qty",
    "quantity": "qty",
    "units": "qty",
    "unitid": "unit_id",
    "orderdate": "order_date",
    "date": "order_date",
    "orderedon": "order_date",
    "placedat": "order_date",
    "note": "note",
    "notes": "note",
    "remark": "note",
}

#: An order date typed in the sheet: the report format's DD-MM-YYYY, or ISO.
_DATE_FORMATS = ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")

_ORDER_ID_RE = re.compile(r"^OD\d{10,}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SEPARATORS = re.compile(r"[\s_\-./]+")

#: A sheet larger than this is not a list someone typed; it is a mistake.
MAX_ROWS = 5_000

#: The width of the columns an order id and a unit id are stored in
#: (``Order.order_id``, ``OrderUnit.unit_id``). A longer one is a row refused
#: by name here, never a database error that fails the whole sheet on a
#: backend that enforces length (audit F-40).
MAX_ID_LENGTH = 64


class IdSheetError(ValueError):
    """The reader refusing a sheet as a whole, in its own words: too many
    rows, or no email or order id column. Those words are meant for the
    operator, unlike the spreadsheet library's own errors, which can quote a
    cell's text (app.api.orders answers those with one fixed sentence)."""


@dataclass
class IdRow:
    row_number: int
    email: str
    order_id: str
    qty: int = 1
    unit_id: str | None = None
    note: str | None = None
    #: The order's date as the sheet gives it (an IST day), as the stored UTC clock.
    order_date: "datetime | None" = None


def parse_order_date(value: object) -> "datetime | None":
    """A sheet's order date as the stored (naive UTC) clock, or None when blank.

    Raises ValueError for text that is not a date. A bare day is read in IST,
    the operator's calendar, at midnight — the same reading as the ledger's
    date filter.
    """
    from datetime import datetime, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        parsed = None
        for pattern in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"not a date: {text!r} (use DD-MM-YYYY)")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ist)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass
class Rejected:
    row_number: int
    reason: str


@dataclass
class IdSheet:
    rows: list[IdRow] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)


def normalise_header(text: object) -> str:
    return _SEPARATORS.sub("", str(text or "").strip().lower())


def _columns(header: list[object]) -> dict[str, int]:
    """Field → column index, from whatever the header row calls things."""
    found: dict[str, int] = {}
    for index, cell in enumerate(header):
        name = _HEADER_ALIASES.get(normalise_header(cell))
        if name and name not in found:
            found[name] = index
    return found


def _cell(row: list[object], index: int | None) -> str:
    value = _raw(row, index)
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # a spreadsheet's 2.0 is the operator's 2
    return str(value).strip()


#: A spreadsheet's text form, ="…": the one way a CSV can tell Excel "this is
#: text", which the ledger's own export writes every long id, phone and IMEI
#: in (app.io.orders_csv). A doubled quote inside stands for one quote; a
#: formula that does anything more ( ="4"&"39" ) is not this form.
_TEXT_FORM = re.compile(r'^="((?:[^"]|"")*)"$')


def _unwrapped(value: object) -> object:
    """A cell in the text form ="…" as the text inside it; any other value as
    it is.

    Read as written, ="439123456789012345678" is no unit id the platform ever
    gave: the ledger's own export handed back as a report gave each order a
    second, phantom unit beside every real one, and as an id sheet it filed
    the quotes and the equals sign as a new order's unit id (review of 27 Oct
    2026). Unwrapped before any check, so the mangled-id test, the order id's
    pattern and the lengths all read the value itself."""
    if isinstance(value, str):
        match = _TEXT_FORM.match(value.strip())
        if match:
            return match.group(1).replace('""', '"').strip()
    return value


def _raw(row: list[object], index: int | None) -> object:
    """A cell as the sheet holds it (a number stays a number), or None — the
    text form ="…" read as the text inside it (:func:`_unwrapped`)."""
    if index is None or index >= len(row):
        return None
    return _unwrapped(row[index])


#: A unit id as the platform gives one: a run of digits (Flipkart's are 21
#: long), letters allowed beside at least one digit. Anything else — a
#: formula, a space, a dash, a quote, or a word typed for an id not known yet
#: ("NA", "TBD") — names no unit the platform has, and stored it would be a
#: unit no read-back ever matches.
_UNIT_ID_RE = re.compile(r"^(?=[0-9A-Z]*[0-9])[0-9A-Z]+$")


def unit_id_refusal(unit_id: str) -> str | None:
    """Why a unit id cell (unwrapped, upper-cased) cannot be stored, or None
    when it is a plain run of digits and letters. The reason is worded for
    the operator, as a refused row's is."""
    if _UNIT_ID_RE.match(unit_id):
        return None
    return f"unit id {unit_id!r} is not a unit id: write its digits and letters alone, such as 439123456789012345678"


#: How a spreadsheet writes a long number it has rounded: Excel shows the unit
#: id 439123456789012345678 as 4.39123E+20, and a CSV saved from it keeps that.
_SCIENTIFIC_RE = re.compile(r"^[+-]?\d+(?:\.\d*)?E[+-]?\d+$", re.IGNORECASE)

#: The digits a spreadsheet keeps of a number; the ones after are lost.
SPREADSHEET_DIGITS = 15


#: Digits as a spreadsheet shows a number it holds, never as an id is
#: written: grouped ("439,123,456,789,012,000,000", the lakh form, or with
#: spaces), or with a zero fraction ("439123456789012000000.00").
_NUMBER_SHOWN_RE = re.compile(r"^\d{1,3}(?:[,\u00a0' ]\d{2,3})+(?:\.0+)?$|^\d+\.0+$")


def _rounded(text: str) -> bool:
    """A run of digits longer than a spreadsheet keeps, ending in the zeros it
    puts in place of the rest: "439123456789012000000", what Excel writes for
    a unit id it took for a number and shown with no exponent (review of
    27 Oct 2026). An id that truly ends in so many zeros is one in a million;
    the refusal says how to type it as text."""
    return text.isdigit() and len(text) > SPREADSHEET_DIGITS and set(text[SPREADSHEET_DIGITS:]) == {"0"}


def _mangled_id(value: object, label: str) -> str | None:
    """Why an id cell cannot be trusted, or None when it reads as typed.

    A Flipkart unit id is a run of 21 digits. A spreadsheet that takes such an
    id for a number keeps only its first 15 digits and shows the rest as an
    exponent or as zeros, and the lost digits cannot be got back. Stored, the
    rounded value would name a unit the platform never had, which no read-back
    would ever match. So the row is refused by name instead (spec FR-060;
    order-report-format.md, "Long identifiers"): scientific notation in a CSV
    ("4.39123E+20"), the rounded digits shown whole ("439123456789012000000")
    or grouped ("439,123,…"), and in an .xlsx a number cell that comes back as a float
    (a long id, or a fraction, which is never an id) or as a whole number of
    more than 15 digits.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        shown = f"{value:.15G}"
    elif isinstance(value, int) and len(str(abs(value))) > SPREADSHEET_DIGITS:
        shown = str(value)
    elif isinstance(value, str) and (
        _SCIENTIFIC_RE.match(value.strip()) or _NUMBER_SHOWN_RE.match(value.strip()) or _rounded(value.strip())
    ):
        shown = value.strip()
    else:
        return None
    return (
        f"{label} {shown!r} was saved as a number, which keeps only {SPREADSHEET_DIGITS} digits, "
        f"so the id may be wrong; format the {label} column as Text and type the ids again"
    )


def _read_rows(data: bytes, filename: str) -> list[list[object]]:
    """Every row of the sheet as a list of cells, header first."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            sheet = wb.worksheets[0]
            return [list(row) for row in sheet.iter_rows(values_only=True)]
        finally:
            wb.close()
    # CSV: UTF-8 with or without a BOM; Excel's "CSV UTF-8" writes one.
    text = data.decode("utf-8-sig", errors="replace")
    # newline="": the csv module then takes "\r", "\n" and "\r\n" alike, as
    # the card-sheet reader does — Excel for Mac's CSV ends its rows in a bare
    # "\r", which was refused as unreadable (review of 27 Oct 2026).
    return [list(row) for row in csv.reader(io.StringIO(text, newline=""))]


def read_id_sheet(data: bytes, filename: str) -> IdSheet:
    """Parse the sheet. Unreadable rows are reported with their row number
    and the reason; readable ones are returned, in sheet order, with
    duplicates (same email and order id) reported rather than repeated.

    A sheet refused as a whole raises IdSheetError. A file that is no sheet
    at all raises whatever the spreadsheet library raises, which may quote
    a cell, so it is never shown as it is."""
    rows = _read_rows(data, filename)
    sheet = IdSheet()
    if not rows:
        return sheet
    if len(rows) - 1 > MAX_ROWS:
        raise IdSheetError(f"the sheet has {len(rows) - 1:,} rows; at most {MAX_ROWS:,} are accepted")

    columns = _columns(rows[0])
    missing = [name for name in ("email", "order_id") if name not in columns]
    if missing:
        raise IdSheetError(
            "the header row needs an email column and an order id column "
            f"(missing: {', '.join(missing)}); found: "
            + ", ".join(str(c) for c in rows[0] if c not in (None, ""))
        )

    seen: set[tuple[str, str]] = set()
    for number, row in enumerate(rows[1:], start=2):
        if not any(_cell(row, i) for i in range(len(row))):
            continue  # a blank line, not a mistake
        email = _cell(row, columns.get("email")).lower()
        order_id = _cell(row, columns.get("order_id")).upper()
        qty_text = _cell(row, columns.get("qty"))
        if not _EMAIL_RE.match(email):
            sheet.rejected.append(Rejected(number, f"not an email: {email!r}"))
            continue
        mangled = _mangled_id(_raw(row, columns.get("order_id")), "order id") or _mangled_id(
            _raw(row, columns.get("unit_id")), "unit id"
        )
        if mangled:
            sheet.rejected.append(Rejected(number, mangled))
            continue
        if not _ORDER_ID_RE.match(order_id):
            sheet.rejected.append(Rejected(number, f"not a Flipkart order id: {order_id!r}"))
            continue
        if len(order_id) > MAX_ID_LENGTH:
            sheet.rejected.append(
                Rejected(number, f"order id is {len(order_id)} characters long; at most {MAX_ID_LENGTH} fit")
            )
            continue
        unit_id = _cell(row, columns.get("unit_id")).upper() or None
        if unit_id is not None and len(unit_id) > MAX_ID_LENGTH:
            sheet.rejected.append(
                Rejected(number, f"unit id is {len(unit_id)} characters long; at most {MAX_ID_LENGTH} fit")
            )
            continue
        refusal = None if unit_id is None else unit_id_refusal(unit_id)
        if refusal:
            sheet.rejected.append(Rejected(number, refusal))
            continue
        qty = 1
        if qty_text:
            try:
                number_value = float(qty_text)
            except ValueError:
                sheet.rejected.append(Rejected(number, f"quantity is not a number: {qty_text!r}"))
                continue
            if not number_value.is_integer():
                sheet.rejected.append(
                    Rejected(number, f"quantity must be a whole number, not {qty_text}")
                )
                continue
            qty = int(number_value)
            if not 1 <= qty <= 99:
                sheet.rejected.append(Rejected(number, f"quantity must be 1–99, not {qty}"))
                continue
        try:
            order_date = parse_order_date(_raw(row, columns.get("order_date")))
        except ValueError as exc:
            sheet.rejected.append(Rejected(number, str(exc)))
            continue
        key = (email, order_id)
        if key in seen:
            sheet.rejected.append(Rejected(number, f"repeats {order_id} for {email}"))
            continue
        seen.add(key)
        sheet.rows.append(
            IdRow(
                row_number=number,
                email=email,
                order_id=order_id,
                qty=qty,
                unit_id=unit_id,
                note=_cell(row, columns.get("note")) or None,
                order_date=order_date,
            )
        )
    return sheet


def read_id_sheet_from(source: BinaryIO, filename: str) -> IdSheet:
    return read_id_sheet(source.read(), filename)


@dataclass
class ImportOutcome:
    """What an uploaded id list did: rows that became orders to track, rows
    already in the ledger, rows refused — and which accounts and ids a
    read-back should now cover."""

    created: int = 0
    existing: int = 0
    rejected: list[Rejected] = field(default_factory=list)
    account_ids: list[int] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)


def import_sheet(db, sheet: IdSheet) -> ImportOutcome:
    """Turn a parsed sheet into order rows to track, in one transaction.

    A row needs an account the portal knows (by email) — the read-back signs
    in as that account to open the order. A Flipkart order id names one order
    on the whole platform, so an id the ledger already holds is counted when it
    is under the same account and refused when it is under another; an account
    with an unconfirmed order still awaiting settling has its row refused too,
    with a pointer to Settle, rather than a second row being made for what may
    be the same order. New rows are ``placed`` with the sheet's order date when
    it gives one (undated rows count against unit limits until read back) and
    no payment mode: the operator says the order exists; what the platform
    says about it arrives with the read-back.

    A unit id names one unit on the platform (spec FR-024), so a row whose
    unit id the ledger already holds under another order is refused, naming
    that order: the sheet and the ledger disagree about whose unit it is, and
    a new order with a blank unit in its place would hide that.
    """
    from sqlalchemy import func, select

    from app.core.order_units import PLATFORM, SOURCE_UPLOAD, ensure_units
    from app.db.models import Account, Order, OrderStatus, OrderUnit

    out = ImportOutcome(rejected=list(sheet.rejected))
    emails = {row.email for row in sheet.rows}
    accounts: dict[str, int] = {}
    if emails:
        found = db.execute(
            select(Account.id, Account.email).where(func.lower(Account.email).in_(emails))
        ).all()
        accounts = {email.lower(): account_id for account_id, email in found}

    touched: set[int] = set()
    for row in sheet.rows:
        account_id = accounts.get(row.email)
        if account_id is None:
            out.rejected.append(Rejected(row.row_number, f"no account with the email {row.email}"))
            continue
        if row.unit_id:
            holder = db.execute(
                select(Order.id, Order.order_id)
                .join(OrderUnit, OrderUnit.order_pk == Order.id)
                .where(OrderUnit.platform == PLATFORM, OrderUnit.unit_id == row.unit_id)
                .limit(1)
            ).first()
            if holder is not None and holder.order_id != row.order_id:
                other = holder.order_id or f"order #{holder.id}"
                out.rejected.append(
                    Rejected(row.row_number, f"unit id {row.unit_id} is already in the ledger under {other}")
                )
                continue
        holders = db.execute(
            select(Order.account_id, Account.email)
            .join(Account, Account.id == Order.account_id)
            .where(Order.order_id == row.order_id)
        ).all()
        if any(holder_id == account_id for holder_id, _ in holders):
            out.existing += 1
            touched.add(account_id)
            out.order_ids.append(row.order_id)
            continue
        if holders:
            other = sorted({email for _, email in holders})[0]
            out.rejected.append(
                Rejected(row.row_number, f"{row.order_id} is already in the ledger under {other}")
            )
            continue
        awaiting = db.scalars(
            select(Order.id).where(
                Order.account_id == account_id,
                Order.status == OrderStatus.unconfirmed,
                Order.order_id.is_(None),
            )
        ).all()
        if awaiting:
            listed = ", ".join(f"#{pk}" for pk in awaiting[:3])
            out.rejected.append(
                Rejected(
                    row.row_number,
                    f"{row.email} has an unconfirmed order awaiting settling ({listed}); if "
                    f"{row.order_id} is that order, settle it in the ledger (View → Found it) "
                    "instead of uploading it",
                )
            )
            continue
        if True:
            order = Order(
                account_id=account_id,
                order_id=row.order_id,
                qty=row.qty,
                status=OrderStatus.placed,
                placed_at=row.order_date,
                payment_mode="unknown",
            )
            db.add(order)
            units = ensure_units(order, source=SOURCE_UPLOAD)
            if row.unit_id:
                # Free: a unit id another order holds was refused above, and
                # an order holding it under this id is not new.
                units[0].unit_id = row.unit_id
            db.flush()
            out.created += 1
            touched.add(account_id)
            out.order_ids.append(row.order_id)
    db.commit()
    out.account_ids = sorted(touched)
    out.rejected.sort(key=lambda item: item.row_number)
    return out
