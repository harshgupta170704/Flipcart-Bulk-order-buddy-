"""Corporate-card parsing — the only place card data is read (PLAN.md §5, §8).

Cards arrive as a sheet upload (``.xlsx`` or CSV) at the *start of an ordering
run*, are held in memory for that run only, and are never persisted: there is deliberately no
Card table, no card field in ``Job.payload`` or ``Job.log``, and nothing in
this module writes to disk or hands a value to a queue.

Because this is the only card-aware code path, it is deliberately defensive
about leaks:

* :meth:`CardRecord.masked`, ``__repr__`` and ``__str__`` are masked, so an
  accidental f-string, log line or exception traceback cannot print a PAN, an
  expiry, a CVV or a password.
* Nothing here logs a card value — not even at debug level. Only *counts* are
  logged.

Real-world sheets vary, so :func:`parse_cards` is forgiving: it prefers a
header row (any column order, any casing, optionally a single combined expiry
cell) and falls back to the legacy ordering-workbook layout — card at T/U/V/W,
the columns ``app.io.excel`` intentionally ignores on import — when a sheet has
no usable header.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import itertools
import logging
import re
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Iterator, Sequence

from openpyxl import load_workbook

#: An .xlsx file is a zip archive and starts with this signature; anything
#: else handed in is read as a CSV sheet (the owner keeps the card list as one).
_XLSX_SIGNATURE = b"PK\x03\x04"

log = logging.getLogger("ordering.cards")

# --- Legacy layout (1 = column A), used only when no header row is found -----
LEGACY_COL_EMAIL = 3  # C
LEGACY_COL_NUMBER = 20  # T
LEGACY_COL_MONTH = 21  # U
LEGACY_COL_YEAR = 22  # V
LEGACY_COL_CVV = 23  # W
FIRST_DATA_ROW = 2

# Internally column positions are 0-based (row tuples), hence the -1.
_LEGACY_MAPPING: dict[str, int] = {
    "email": LEGACY_COL_EMAIL - 1,
    "number": LEGACY_COL_NUMBER - 1,
    "month": LEGACY_COL_MONTH - 1,
    "year": LEGACY_COL_YEAR - 1,
    "cvv": LEGACY_COL_CVV - 1,
}

# How many leading rows may be inspected while hunting for a header row. Real
# sheets put a title or a blank line above the header; data never starts later.
HEADER_SCAN_ROWS = 10

# Header aliases, normalized (lowercase, non-alphanumerics removed) and ordered
# strongest-first so "Card Number" wins over a bare "Number" in the same sheet.
_ALIASES: dict[str, tuple[str, ...]] = {
    "number": ("cardnumber", "cardno", "card", "number"),
    "month": ("expmonth", "expirymonth", "month", "mm"),
    "year": ("expyear", "expiryyear", "year", "yyyy", "yy"),
    "cvv": ("cvv", "cvv2", "cvc", "securitycode"),
    "email": ("accountemail", "emailid", "email", "account"),
    "expiry": ("expirydate", "expiry", "validthru", "mmyy", "exp"),
    "card_type": ("cardtype", "cardvariant", "cardcategory"),
    "password": (
        "cardpassword",
        "password",
        "netsafepassword",
        "securepassword",
        "transactionpassword",
        "onlinepassword",
        "cardpin",
        "pin",
    ),
}
#: A heading that is not an exact alias but plainly names a password ("HDFC
#: NetSafe Password", "Online Pwd"): the bank's page asks for it after Pay.
_PASSWORDISH = re.compile(r"passw(?:or)?d|pwd")

# A header row is only trusted if it names the card number *and* at least one
# other card field. Otherwise a legacy sheet's "Numbers" (phone) column could
# be mistaken for a PAN column.
_CORROBORATING = ("month", "year", "cvv", "expiry")

#: A header that is not one of the exact aliases but plainly names an email —
#: "Email Address", "Cardholder Email", "Login Email", "Account Email ID",
#: "Mail ID". Dropping such a column silently would pair cards to accounts by
#: position and charge the wrong card, so it is taken as the email column at a
#: rank below every exact alias.
_EMAILISH = re.compile(r"e?mail")
_FUZZY_RANK = 99
#: A cell that is an email address, for finding the email column by its values
#: when no header names it.
_EMAIL_VALUE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
#: A float-like text cell ("4111111111111111.0", "9.0", "2027.0") written by a
#: spreadsheet tool that kept a numeric column as floats: the fraction is noise.
_FLOAT_LIKE = re.compile(r"^-?\d+\.0+$")


class CardSheetError(ValueError):
    """The sheet holds a card row that cannot be a card. The message names the
    row and the field only — never a value — because it reaches the browser."""


@dataclass(frozen=True, repr=False)
class CardRecord:
    """One corporate card, in memory only.

    Missing fields come back as empty strings rather than ``None`` so callers
    can hand them straight to ``automation.ordering.CardInfo`` without null
    checks. ``cvv`` stays a ``str`` so leading zeros survive.
    """

    number: str
    month: str
    year: str
    cvv: str
    email: str | None = None
    #: The password the bank's own page asks for after Pay (HDFC's corporate
    #: cards, the owner, 26 Oct 2026); blank when the sheet has none. In memory
    #: for the run only, like the CVV.
    password: str = ""
    #: The sheet's Card Type column ("HDFC Virtual"), when it has one. A run
    #: given a card type uses only the rows of that type.
    card_type: str = ""

    def masked(self) -> str:
        """Last four digits only — the most that may ever be displayed."""
        last4 = self.number[-4:]
        return f"**** **** **** {last4}" if last4 else "**** **** **** ****"

    def __repr__(self) -> str:
        """Masked on purpose: repr is what leaks into logs and tracebacks.

        The last four digits are all of the card it shows. The expiry is
        masked like the CVV and the password: it used to print in clear
        ("exp=12/2028"), and the owner's rule keeps every part of a card but
        its number on the order row off every log and screen (NFR-002, 25 Oct
        2026; review of 27 Oct 2026).
        """
        return (
            f"CardRecord(number={self.masked()!r}, exp=**/****, "
            f"cvv='***', password={'***' if self.password else ''!r}, email={self.email!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


# --- Cell/value normalization ------------------------------------------------
def _cell_text(value: object) -> str | None:
    """Normalize a raw cell value to a trimmed string, or None if blank.

    openpyxl returns numeric-looking cells as ``int``/``float``; a float must
    not reach us as ``"4111.0"``, so whole floats are narrowed to ``int`` first.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        text = str(int(value)) if value.is_integer() else repr(value)
    elif isinstance(value, int):
        text = str(value)
    else:
        text = str(value)
    text = text.strip()
    if text == "" or text.lower() == "none":
        return None
    return text


def _at(values: Sequence[object], col: int | None) -> str | None:
    """Read one column from a row tuple, tolerating short/ragged rows."""
    if col is None or col < 0 or col >= len(values):
        return None
    return _cell_text(values[col])


#: Letters in front of the digits — HDFC's virtual corporate cards are listed
#: as "C4111…" on the bank's sheet (the owner, 26 Oct 2026). They are not part
#: of the number Flipkart's form takes.
_LETTER_PREFIX = re.compile(r"^[A-Za-z]+(?=\d{12,19}$)")


def _normalize_number(text: str | None) -> str:
    """Strip the spaces/dashes people type into a PAN ('4111-1111' -> digits)
    and a letter prefix in front of a whole card number ('C4111…' -> '4111…')."""
    if text is None:
        return ""
    digits = re.sub(r"[\s_\-‐-―]", "", text)
    return _LETTER_PREFIX.sub("", digits)


def _normalize_month(text: str | None) -> str:
    """Zero-pad to two chars: '1' -> '01'. Non-digits are dropped."""
    if text is None:
        return ""
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    return digits.zfill(2) if len(digits) < 2 else digits


def _normalize_year(text: str | None) -> str:
    """Keep the year exactly as the sheet states it.

    A 2-digit year is kept 2-digit: the Flipkart form takes whatever the sheet
    says, and inventing a century ('28' -> 2028? 1928?) could silently pick the
    wrong decade. No guessing.
    """
    if text is None:
        return ""
    return re.sub(r"\D", "", text)


def _normalize_cvv(text: str | None) -> str:
    """Keep as a string so '012' does not become 12."""
    if text is None:
        return ""
    return re.sub(r"\s", "", text)


def _split_expiry(value: object) -> tuple[str, str]:
    """Split one combined cell — '12/28', '12-28', '12 / 2028' — into (mm, yy).

    Excel sometimes parses such a cell into a real date, so datetimes are
    handled too. A leading 4-digit group means the sheet is year-first.
    """
    if isinstance(value, (_dt.datetime, _dt.date)):
        return f"{value.month:02d}", str(value.year)

    text = _cell_text(value)
    if text is None:
        return "", ""

    groups = re.findall(r"\d+", text)
    if len(groups) >= 2:
        first, second = groups[0], groups[1]
        if len(first) == 4 and len(second) <= 2:
            return second, first  # "2028/12"
        return first, second
    if len(groups) == 1:
        packed = groups[0]
        if len(packed) == 4:  # "1228"
            return packed[:2], packed[2:]
        if len(packed) == 6:  # "122028"
            return packed[:2], packed[2:]
    return "", ""


# --- Header detection --------------------------------------------------------
def _norm_header(value: object) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return re.sub(r"[^a-z0-9]", "", str(value).strip().lower())


def _match_header_row(values: Sequence[object]) -> dict[str, int]:
    """Map field -> 0-based column for one candidate header row."""
    mapping: dict[str, int] = {}
    ranks: dict[str, int] = {}
    for col, value in enumerate(values):
        key = _norm_header(value)
        if not key:
            continue
        matched = False
        for field, aliases in _ALIASES.items():
            if key not in aliases:
                continue
            matched = True
            rank = aliases.index(key)
            if field not in mapping or rank < ranks[field]:
                mapping[field] = col
                ranks[field] = rank
        if not matched and _EMAILISH.search(key) and ("email" not in mapping or ranks["email"] > _FUZZY_RANK):
            mapping["email"] = col
            ranks["email"] = _FUZZY_RANK
        elif not matched and _PASSWORDISH.search(key) and ("password" not in mapping or ranks["password"] > _FUZZY_RANK):
            mapping["password"] = col
            ranks["password"] = _FUZZY_RANK
    return mapping


def _email_column_by_values(rows: Sequence[Sequence[object]], mapping: dict[str, int]) -> int | None:
    """The column whose first non-blank cell is an email address, among the
    columns no field claimed — for a sheet whose email column has a header no
    rule recognises. None when there is no such column."""
    taken = set(mapping.values())
    for values in rows:
        for col, value in enumerate(values):
            if col in taken:
                continue
            text = _cell_text(value)
            if text and _EMAIL_VALUE.match(text):
                return col
    return None


def _find_header(rows: Sequence[Sequence[object]]) -> tuple[dict[str, int], int]:
    """Find the first usable header row. Returns (mapping, row index) or ({}, -1)."""
    for index, values in enumerate(rows):
        mapping = _match_header_row(values)
        if "number" in mapping and any(f in mapping for f in _CORROBORATING):
            return mapping, index
    return {}, -1


# --- Row -> record -----------------------------------------------------------
def _is_blank(values: Sequence[object]) -> bool:
    return all(_cell_text(v) is None for v in values)


def _record_from_row(
    values: Sequence[object], mapping: dict[str, int], row_number: int = 0
) -> CardRecord | None:
    """Build a record, or None when the row carries no card number.

    A row that carries one is checked for shape — the number 12 to 19 digits,
    a month 01 to 12, a year of 2 or 4 digits, a CVV of 3 or 4 digits — and
    refused with the row and the field named (never the value): a malformed
    card would otherwise be typed into Flipkart's form as it stands.
    """
    number = _normalize_number(_at(values, mapping.get("number")))
    if not number:
        return None

    month = _normalize_month(_at(values, mapping.get("month")))
    year = _normalize_year(_at(values, mapping.get("year")))

    expiry_col = mapping.get("expiry")
    if expiry_col is not None and (not month or not year):
        raw = values[expiry_col] if expiry_col < len(values) else None
        split_month, split_year = _split_expiry(raw)
        month = month or _normalize_month(split_month)
        year = year or _normalize_year(split_year)

    cvv = _normalize_cvv(_at(values, mapping.get("cvv")))
    where = f"row {row_number}" if row_number else "a row"
    if not number.isdigit() or not 12 <= len(number) <= 19:
        raise CardSheetError(f"{where}: the card number is not 12 to 19 digits")
    if month and not (month.isdigit() and 1 <= int(month) <= 12):
        raise CardSheetError(f"{where}: the expiry month is not 01 to 12")
    if year and (not year.isdigit() or len(year) not in (2, 4)):
        raise CardSheetError(f"{where}: the expiry year is not 2 or 4 digits")
    if cvv and (not cvv.isdigit() or len(cvv) not in (3, 4)):
        # A short CVV is almost always a spreadsheet that dropped a leading
        # zero ("007" shown and saved as 7); guessing the zeros back would be
        # guessing at a card, so the sheet is refused with the cause named.
        raise CardSheetError(
            f"{where}: the CVV is not 3 or 4 digits (a spreadsheet drops leading zeros — keep that column as text)"
        )

    email = _at(values, mapping.get("email"))
    return CardRecord(
        number=number,
        month=month,
        year=year,
        cvv=cvv,
        email=email,
        password=(_at(values, mapping.get("password")) or ""),
        card_type=(_at(values, mapping.get("card_type")) or "").strip(),
    )


@dataclass(frozen=True)
class CardSheet:
    """What a card sheet yielded: the cards that can be used, and the rows that
    could not be a card — each by row number and field only, never a value, so
    the list can be shown in the run's log and in the browser."""

    records: list[CardRecord]
    rejected: list[dict]

    def skipped_note(self) -> str:
        return "; ".join(f"row {item['row']}: {item['reason']}" for item in self.rejected)


def parse_card_sheet(source: str | BinaryIO) -> CardSheet:
    """Parse the corporate-card sheet (path or file-like).

    An ``.xlsx`` workbook or a CSV file, told apart by content, not by name:
    the owner keeps the list as a CSV. Header-driven when the sheet has one,
    legacy T/U/V/W layout when it does not. Rows with no card number are
    skipped; the first fully-empty row after data has started ends the sheet.
    A row that carries a card number but cannot be a card — a CVV of two
    digits, a number of the wrong length — is set aside and named by row and
    field, and the rest of the sheet is still used: one bad line in a long
    corporate list must not stop every other card (the owner, 26 Oct 2026).
    Nothing is persisted or logged beyond the counts and those row numbers.
    """
    if _is_workbook(source):
        wb = load_workbook(source, read_only=True, data_only=True)
        try:
            sheet = wb.active
            if sheet is None:
                return CardSheet([], [])
            records, rejected = _records_from_rows(sheet.iter_rows(values_only=True))
        finally:
            wb.close()
    else:
        records, rejected = _records_from_rows(_csv_rows(source))

    log.info("parsed %d card record(s) for this run, %d row(s) set aside", len(records), len(rejected))
    return CardSheet(records, rejected)


def parse_cards(source: str | BinaryIO) -> list[CardRecord]:
    """The usable cards of the sheet; see :func:`parse_card_sheet`."""
    return parse_card_sheet(source).records


def _is_workbook(source: str | BinaryIO) -> bool:
    """Whether ``source`` is an .xlsx file: by its zip signature when it can be
    peeked, by its name otherwise."""
    if isinstance(source, str):
        return not source.lower().endswith(".csv")
    try:
        position = source.tell()
        head = source.read(len(_XLSX_SIGNATURE))
        source.seek(position)
    except (AttributeError, OSError, ValueError):
        return True
    return head == _XLSX_SIGNATURE


def _csv_rows(source: str | BinaryIO) -> Iterator[tuple[object, ...]]:
    """A CSV's rows as tuples of strings, the way a worksheet yields its cells.
    Read with a BOM-tolerant UTF-8 so a sheet saved from Excel parses too."""
    if isinstance(source, str):
        with open(source, "rb") as handle:
            raw = handle.read()
    else:
        raw = source.read()
    text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else str(raw)
    # newline="" hands the line ends to the csv module untouched, so a sheet
    # saved with bare carriage returns (Excel for Mac's "CSV (Macintosh)") is
    # read row by row instead of as one line that csv refuses.
    for row in csv.reader(io.StringIO(text, newline="")):
        yield tuple(_narrow(cell.strip()) for cell in row)


def _narrow(cell: str) -> str:
    """A CSV cell a spreadsheet tool wrote as a float ("4111111111111111.0",
    "9.0") loses its empty fraction, as a workbook's float cells do."""
    return cell.split(".", 1)[0] if _FLOAT_LIKE.match(cell) else cell


def _records_from_rows(rows: Iterator[tuple[object, ...]]) -> tuple[list[CardRecord], list[dict]]:
    """The shared core: find the header (or fall back to the legacy columns)
    and build one record per card row; the rows that cannot be a card come
    back beside them, by row number and reason."""
    head = list(itertools.islice(rows, HEADER_SCAN_ROWS))

    mapping, header_index = _find_header(head)
    if mapping:
        first_data_index = header_index + 1
        if "email" not in mapping:
            # The header names no email column: look for one by its values,
            # in the rows already in hand (plus the next one, when the header
            # was the last of them), before settling for pairing by position.
            sample = list(head[first_data_index:])
            if not sample:
                following = next(rows, None)
                if following is not None:
                    sample.append(following)
                    head = [*head, following]
            email_col = _email_column_by_values(sample, mapping)
            if email_col is not None:
                mapping = {**mapping, "email": email_col}
        data_rows: Iterable[Sequence[object]] = itertools.chain(head[first_data_index:], rows)
    else:
        # No usable header: assume the legacy ordering sheet, data from row 2.
        mapping = _LEGACY_MAPPING
        first_data_index = FIRST_DATA_ROW - 1
        data_rows = itertools.chain(head[first_data_index:], rows)

    records: list[CardRecord] = []
    rejected: list[dict] = []
    for offset, values in enumerate(data_rows):
        if _is_blank(values):
            if records or rejected:
                break  # first blank row after data ends the region
            continue
        row_number = first_data_index + offset + 1
        try:
            record = _record_from_row(values, mapping, row_number=row_number)
        except CardSheetError as exc:
            # "row N: <reason>" — the row is kept in its own field.
            rejected.append({"row": row_number, "reason": str(exc).split(": ", 1)[-1]})
            continue
        if record is not None:
            records.append(record)
    return records, rejected


def map_cards_to_accounts(
    cards: list[CardRecord], accounts: Iterable[object]
) -> dict[int, CardRecord]:
    """Decide which card pays for which account. Returns {account_id: card}.

    Two strategies, in order:

    1. **By email** — used as soon as *any* parsed card carries an email.
       Matching is case-insensitive; the first card wins if an email repeats.
       Accounts with no matching card are simply absent from the result, and
       the ordering job skips them rather than charging the wrong card.
    2. **By position** — the fallback when no card has an email at all: card
       *i* pays for ``accounts[i]``, in the order both lists were given. Extra
       cards and extra accounts are ignored, so a short card sheet silently
       covers only the first N accounts. This is why email columns are
       preferred for anything but a single-card run.
    """
    account_list = list(accounts)
    mapped: dict[int, CardRecord] = {}

    if any(card.email for card in cards):
        by_email: dict[str, CardRecord] = {}
        for card in cards:
            if card.email:
                by_email.setdefault(card.email.strip().lower(), card)
        for account in account_list:
            key = (getattr(account, "email", None) or "").strip().lower()
            card = by_email.get(key)
            if card is not None:
                mapped[account.id] = card
        log.info(
            "matched %d of %d account(s) to a card by email",
            len(mapped),
            len(account_list),
        )
        return mapped

    for card, account in zip(cards, account_list):
        mapped[account.id] = card
    log.info(
        "matched %d of %d account(s) to a card by position",
        len(mapped),
        len(account_list),
    )
    return mapped
