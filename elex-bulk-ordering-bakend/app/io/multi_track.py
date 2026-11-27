"""The courier's multi-track sheet, handed back with what the ledger knows.

E-Kart's portal exports a "multi_track" workbook: one row per shipment, its
header on the second row under a blank one, with TRACKING ID and ORDER ID among
its 32 columns. The operator works from that file. Uploaded here, it comes back
as the same file — every sheet, row and cell left as it was — with four columns
added after its last: the account the unit was ordered on, Flipkart's delivery
OTP, the last six digits of the phone it is delivered to, and Flipkart's own
status. A row is matched to a unit by its tracking ID, else by its order ID
when that order has one unit only (owner's request, 26 Nov 2026).
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field

from openpyxl import load_workbook
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Account, Order, OrderUnit
from app.io.orders_csv import status_word, unit_phone

#: The columns added after the sheet's own, in this order.
ADDED_HEADERS = ("ACCOUNT", "DELIVERY OTP", "LAST 6 DIGITS OF PHONE", "FLIPKART STATUS")
#: The rows searched for the header: E-Kart's sits on the second.
HEADER_SEARCH_ROWS = 10
#: Shipments one sheet may carry; a portal export is a few hundred.
MAX_ROWS = 20_000
#: Ids looked up per query, under every database's parameter limit.
LOOKUP_CHUNK = 500


class MultiTrackError(ValueError):
    """The sheet is readable but is not a multi-track sheet; worded for the operator."""


@dataclass(frozen=True)
class Filled:
    """The four cells a row gains."""

    account: str = ""
    otp: str = ""
    last6: str = ""
    status: str = ""

    def cells(self) -> list[str]:
        return [self.account, self.otp, self.last6, self.status]


@dataclass
class Outcome:
    """The sheet as it goes back, and how many of its rows were found."""

    data: bytes
    rows: int = 0
    matched: int = 0
    unmatched_tracking: list[str] = field(default_factory=list)


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _order_key(value: str) -> str:
    """An order id as both sides write it: ``OD`` and the digits, upper case."""
    value = value.strip().upper()
    if value and value.isdigit():
        value = "OD" + value
    return value


def _header_at(rows: list[list]) -> tuple[int, int, int | None]:
    """Where the header is: its row index, the TRACKING ID column, the ORDER ID column."""
    for at, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
        names = [_clean(cell).upper() for cell in row]
        if "TRACKING ID" in names:
            order_col = names.index("ORDER ID") if "ORDER ID" in names else None
            return at, names.index("TRACKING ID"), order_col
    raise MultiTrackError(
        "This is not a multi-track sheet: no column named TRACKING ID in its first rows. "
        "Upload the multi_track file exported from E-Kart."
    )


def _lookup(db: Session, tracking_ids: set[str], order_ids: set[str]) -> tuple[dict, dict]:
    """The ledger's units for these ids: by tracking ID, and by order ID (all its units)."""
    by_tracking: dict[str, Filled] = {}
    by_order: dict[str, list[Filled]] = defaultdict(list)
    tracking, orders = sorted(tracking_ids), sorted(order_ids)
    for start in range(0, max(len(tracking), len(orders), 1), LOOKUP_CHUNK):
        t_part = tracking[start : start + LOOKUP_CHUNK]
        o_part = orders[start : start + LOOKUP_CHUNK]
        clauses = []
        if t_part:
            clauses.append(OrderUnit.tracking_id.in_(t_part))
        if o_part:
            clauses.append(Order.order_id.in_(o_part))
        if not clauses:
            continue
        query = (
            select(OrderUnit, Order, Account.email, Account.phone)
            .join(Order, Order.id == OrderUnit.order_pk)
            .outerjoin(Account, Account.id == Order.account_id)
            .where(or_(*clauses))
        )
        for unit, order, email, account_phone in db.execute(query).all():
            phone = "".join(ch for ch in (unit_phone(unit, order, account_phone) or "") if ch.isdigit())
            filled = Filled(
                account=email or "",
                otp=unit.delivery_otp or "",
                last6=phone[-6:],
                status=status_word(unit, order),
            )
            if unit.tracking_id:
                by_tracking[unit.tracking_id.strip().upper()] = filled
            if order.order_id:
                by_order[_order_key(order.order_id)].append(filled)
    return by_tracking, by_order


def _match(by_tracking: dict, by_order: dict, tracking: str, order_id: str) -> Filled | None:
    if tracking and tracking.upper() in by_tracking:
        return by_tracking[tracking.upper()]
    units = by_order.get(_order_key(order_id)) if order_id else None
    if units and len(units) == 1:
        return units[0]
    return None


def fill(db: Session, data: bytes, filename: str) -> Outcome:
    """The uploaded sheet with the four columns added; CSV in, CSV out, else XLSX."""
    if filename.lower().endswith(".csv"):
        return _fill_csv(db, data)
    return _fill_xlsx(db, data)


def _fill_xlsx(db: Session, data: bytes) -> Outcome:
    book = load_workbook(io.BytesIO(data))
    sheet = book.worksheets[0]
    head = [[cell.value for cell in row] for row in sheet.iter_rows(min_row=1, max_row=HEADER_SEARCH_ROWS)]
    at, t_col, o_col = _header_at(head)
    header_row = at + 1
    last_col = max(
        (c.column for c in sheet[header_row] if _clean(c.value)),
        default=sheet.max_column,
    )
    body = list(range(header_row + 1, sheet.max_row + 1))
    if len(body) > MAX_ROWS:
        raise MultiTrackError(f"The sheet has {len(body):,} rows; send at most {MAX_ROWS:,} at a time.")

    keys: list[tuple[int, str, str]] = []
    for r in body:
        tracking = _clean(sheet.cell(r, t_col + 1).value)
        order_id = _clean(sheet.cell(r, o_col + 1).value) if o_col is not None else ""
        if tracking or order_id:
            keys.append((r, tracking, order_id))
    by_tracking, by_order = _lookup(
        db, {t.upper() for _, t, _ in keys if t}, {_order_key(o) for _, _, o in keys if o}
    )

    template = sheet.cell(header_row, last_col)
    for offset, name in enumerate(ADDED_HEADERS, start=1):
        cell = sheet.cell(header_row, last_col + offset, name)
        if template.has_style:
            cell._style = template._style
    outcome = Outcome(data=b"", rows=len(keys))
    for r, tracking, order_id in keys:
        found = _match(by_tracking, by_order, tracking, order_id)
        if found is None:
            if tracking:
                outcome.unmatched_tracking.append(tracking)
            continue
        outcome.matched += 1
        for offset, value in enumerate(found.cells(), start=1):
            cell = sheet.cell(r, last_col + offset, value or None)
            # Text, so a leading zero in an OTP or phone stays.
            cell.number_format = "@"
    buffer = io.BytesIO()
    book.save(buffer)
    outcome.data = buffer.getvalue()
    return outcome


def _fill_csv(db: Session, data: bytes) -> Outcome:
    text = data.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    at, t_col, o_col = _header_at(rows)
    body = rows[at + 1 :]
    if len(body) > MAX_ROWS:
        raise MultiTrackError(f"The sheet has {len(body):,} rows; send at most {MAX_ROWS:,} at a time.")
    width = len(rows[at])

    def get(row: list, col: int | None) -> str:
        return _clean(row[col]) if col is not None and col < len(row) else ""

    by_tracking, by_order = _lookup(
        db,
        {get(r, t_col).upper() for r in body if get(r, t_col)},
        {_order_key(get(r, o_col)) for r in body if get(r, o_col)},
    )
    out = io.StringIO()
    writer = csv.writer(out)
    for row in rows[:at]:
        writer.writerow(row)
    writer.writerow([*rows[at], *ADDED_HEADERS])
    outcome = Outcome(data=b"")
    for row in body:
        tracking, order_id = get(row, t_col), get(row, o_col)
        padded = row + [""] * (width - len(row))
        if not (tracking or order_id):
            writer.writerow(padded)
            continue
        outcome.rows += 1
        found = _match(by_tracking, by_order, tracking, order_id)
        if found is None:
            if tracking:
                outcome.unmatched_tracking.append(tracking)
            writer.writerow(padded)
            continue
        outcome.matched += 1
        account, otp, last6, status = found.cells()
        # ="…" keeps a leading zero in Excel, as the app's own CSVs do.
        writer.writerow([*padded, account, f'="{otp}"' if otp else "", f'="{last6}"' if last6 else "", status])
    outcome.data = ("﻿" + out.getvalue()).encode("utf-8")
    return outcome
