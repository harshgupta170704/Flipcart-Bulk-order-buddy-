"""The order report: one row per unit, in the format this project defined
(docs/specs/order-unit-ledger/order-report-format.md and its template).

The spec's decided way for units and their status to come into the ledger is
an uploaded report (C-1; FR-021 a report adds units, FR-022 a later report
updates them, FR-024 one row per platform unit id, FR-060 unreadable rows are
refused one by one and the rest imported, FR-061). Flipkart buyer accounts
offer no such download, so the format is the portal's own: anything that can
write these columns can feed the ledger — a sheet kept by hand, another
tool's export, the ledger's own CSV export handed back (whose long ids and
phones are in a spreadsheet's text form ="…", read as the text inside).

Three rules keep what is already stored safe:

* **The read-back wins.** A unit the read-back has seen on Flipkart
  (``synced_at`` set) only has its empty fields filled from a report; its
  status, dates, tracking and address stay as Flipkart showed them. A unit
  never read back takes the report's values.
* **Nothing is blanked.** An empty cell leaves the stored value alone. An
  address is taken only when the row names a place — its first line and its
  pincode — so a row that leaves either out changes no address, and the same
  place written otherwise (fewer parts, other spelling) leaves the held one
  as it is unless the row only adds to it.
* **A unit id names one unit.** A row whose unit id the ledger holds under
  another order, or under another account, is refused, naming it, as the
  order-id sheet does.

What no read-back reads — the refund, the courier, the report's own last
tracking update, whether the user cancelled — is kept under the same rules
(spec FR-022: a later report updates a unit's refunds and cancellations;
read past as "not kept" until 27 Oct 2026). A report's "Cash on Delivery: No"
is kept as the payment mode "prepaid": not Cash on Delivery, paid some way it
does not name — never "card", a run's own card.

Card columns are never read, stored or echoed; columns the ledger has no place
for yet (IMEI, PO number, e-way bill…) are named in the answer as not kept.
Nothing here signs in to Flipkart: a report is the operator's word for what
it says, and the next read-back of a signed-in account corrects it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from app.db.models import Order, OrderUnit

# The id sheet's readers, shared: the same cells, dates and mangled-id test.
from app.io.order_ids import (
    _DATE_FORMATS,
    Rejected,
    _cell,
    _mangled_id,
    _raw,
    _read_rows,
    normalise_header,
    unit_id_refusal,
)

#: The template's columns, in its order (order-report-template.csv).
TEMPLATE_HEADERS = (
    "platform", "order_id", "unit_id", "order_date", "order_time", "platform_email", "status", "product",
    "unit_amount", "quantity", "total_order_amount", "estimated_delivery", "actual_delivery_date",
    "tracking_id", "shipment_partner", "last_tracking_update", "seller_name", "imei", "business_name",
    "business_gstin", "billing_name", "billing_phone", "billing_pincode", "card_masked", "cash_on_delivery",
    "ewb_number", "delivery_name", "delivery_line1", "delivery_line2", "delivery_city", "delivery_state",
    "delivery_pincode", "delivery_phone", "refund_status", "refund_amount", "refund_method", "cancelled",
    "cancelled_by_user", "received", "invoice_on_file", "po_number", "source",
)
#: A row without one of these is refused; a sheet without the column, whole.
REQUIRED = ("platform", "order_id", "unit_id", "order_date", "status", "product")
#: Columns the ledger has no place for yet: read past, and named in the answer.
NOT_KEPT = ("imei", "billing_name", "ewb_number", "po_number")
#: Columns read past without a word: the ledger works them out itself
#: (cancelled, invoice on file, source), or they are a card's, never stored.
#: ``cancelled`` is only asked whether a row's "No" under cancelled_by_user
#: is an answer (:func:`_who_cancelled`); nothing of it is stored.
_SILENT = ("card_masked", "cancelled", "invoice_on_file", "source")

#: ``Order.payment_mode`` for an order a report says was not Cash on
#: Delivery: paid before it came, by a means the report does not name.
PREPAID = "prepaid"
#: The mode of an order no run placed and nothing has told the ledger about.
UNKNOWN_PAYMENT = "unknown"

#: Rows one report may hold: a year of a busy account's units, with room.
MAX_ROWS = 20_000


def _width(*columns) -> int:
    """The narrowest of these text columns: a value written to all of them
    must fit each one."""
    return min(column.type.length for column in columns)


#: How long each kept value may be: the width of the column it is stored in —
#: on the unit, and on the order where the order row mirrors it. SQLite keeps a
#: longer value whatever the width; PostgreSQL refuses it, and with it the
#: whole report — a bare 500 and nothing imported, for one long status or
#: phone (review of 27 Oct 2026). So the row is refused by name instead, the
#: rest still load (FR-060). Read off the models, so a column widened later
#: widens this with it.
_WIDTH = {
    "order id": _width(Order.__table__.c.order_id),
    "unit id": _width(OrderUnit.__table__.c.unit_id),
    "tracking id": _width(OrderUnit.__table__.c.tracking_id, Order.__table__.c.tracking_id),
    "status": _width(OrderUnit.__table__.c.platform_status, Order.__table__.c.platform_status),
    "product": _width(Order.__table__.c.model),
    "seller_name": _width(OrderUnit.__table__.c.seller, Order.__table__.c.seller),
    "business_name": _width(Order.__table__.c.gst_name),
    "business_gstin": _width(Order.__table__.c.gstin),
    "billing_phone": _width(Order.__table__.c.billing_phone),
    "billing_pincode": _width(Order.__table__.c.pin),
    "shipment_partner": _width(OrderUnit.__table__.c.shipment_partner),
    "last_tracking_update": _width(OrderUnit.__table__.c.last_tracking_text),
    "refund_status": _width(OrderUnit.__table__.c.refund_status),
    "refund_method": _width(OrderUnit.__table__.c.refund_method),
}
#: The joined delivery address is kept in a text column of no fixed width;
#: this much of it is plenty for the alerts and the page.
MAX_ADDRESS = 1000

_COLUMN = {normalise_header(name): name for name in TEMPLATE_HEADERS}
_ORDER_ID_RE = re.compile(r"^OD\d{10,}$")
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
#: An Indian pincode: six digits, as the read-back reads one out of the
#: order page's address (and as ``OrderUnit.delivery_pincode`` keeps it).
_PINCODE_RE = re.compile(r"^\d{6}$")
_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}
IST = timezone(timedelta(hours=5, minutes=30))


class ReportError(ValueError):
    """The reader refusing a report as a whole, in its own words: too many
    rows, or a required column missing. Meant for the operator, unlike the
    spreadsheet library's own errors, which can quote a cell."""


@dataclass
class ReportRow:
    """One unit as a report row gives it, read and checked."""

    row_number: int
    order_id: str
    #: Upper-cased, as the ledger stores and matches unit ids.
    unit_id: str
    #: When the order was placed, as the stored (naive UTC) clock.
    placed_at: datetime
    email: str | None
    status: str
    product: str
    unit_amount: float | None = None
    quantity: int | None = None
    total: float | None = None
    #: The promised and the actual delivery day, as the read-back stores a day.
    promised: datetime | None = None
    delivered: datetime | None = None
    tracking_id: str | None = None
    seller: str | None = None
    business_name: str | None = None
    business_gstin: str | None = None
    billing_phone: str | None = None
    billing_pincode: str | None = None
    cash_on_delivery: bool | None = None
    #: The delivery address as one line, and the two parts that name its
    #: place (spec C-5: the first line and the pincode). All three are None
    #: unless the row gives both of those parts.
    delivery_address: str | None = None
    delivery_line1: str | None = None
    delivery_pincode: str | None = None
    received: bool | None = None
    #: What no read-back reads (spec FR-022): the courier, the report's own
    #: last tracking update, the refund, and whether the user cancelled.
    shipment_partner: str | None = None
    last_tracking: str | None = None
    refund_status: str | None = None
    refund_amount: float | None = None
    refund_method: str | None = None
    cancelled_by_user: bool | None = None


@dataclass
class Report:
    rows: list[ReportRow] = field(default_factory=list)
    rejected: list[Rejected] = field(default_factory=list)
    #: Columns the file carried that the ledger keeps nowhere yet.
    not_kept: list[str] = field(default_factory=list)


class _RowRefused(ValueError):
    """One row's problem, worded for the operator; the rest still load."""


def _text(row: list[object], index: int | None, label: str) -> str | None:
    value = _cell(row, index)
    if not value:
        return None
    if len(value) > _WIDTH[label]:
        raise _RowRefused(f"{label} is longer than {_WIDTH[label]} characters")
    return value


def _identifier(row: list[object], index: int | None, label: str) -> str | None:
    why = _mangled_id(_raw(row, index), label)
    if why:
        raise _RowRefused(why)
    value = _cell(row, index)
    if len(value) > _WIDTH[label]:
        raise _RowRefused(f"{label} is longer than {_WIDTH[label]} characters")
    return value or None


def _money(row: list[object], index: int | None, label: str) -> float | None:
    raw = _raw(row, index)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = _cell(row, index).replace("₹", "").replace(",", "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise _RowRefused(f"{label} is not an amount: write it as a plain number, such as 17707.00") from None
    if value < 0:
        raise _RowRefused(f"{label} cannot be negative")
    return value


def _count(row: list[object], index: int | None, label: str) -> int | None:
    amount = _money(row, index, label)
    if amount is None:
        return None
    if not amount.is_integer() or not 1 <= amount <= 1000:
        raise _RowRefused(f"{label} must be a whole number from 1 to 1000")
    return int(amount)


def _yes_no(row: list[object], index: int | None, label: str) -> bool | None:
    text = _cell(row, index).lower()
    if not text:
        return None
    if text in _YES:
        return True
    if text in _NO:
        return False
    raise _RowRefused(f"{label} must be Yes or No")


def _day(row: list[object], index: int | None, label: str) -> datetime | None:
    """A delivery day, stored as the read-back stores one: that day at
    midnight, no clock attached."""
    raw = _raw(row, index)
    if isinstance(raw, datetime):
        return datetime(raw.year, raw.month, raw.day)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day)
    text = _cell(row, index)
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%d-%m-%Y")
    except ValueError:
        raise _RowRefused(f"{label} is not a date: use DD-MM-YYYY") from None
    return parsed


def _typed_moment(text: str) -> datetime:
    """``order_date`` as typed, a clock in IST: the format's DD-MM-YYYY, or
    the id sheet's wider reading (an ISO day, slashes, a time after the day)."""
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    raise _RowRefused("order_date is not a date: use DD-MM-YYYY")


def _clock(row: list[object], index: int | None) -> time | None:
    """``order_time`` as a time of day, or None when the cell is empty."""
    raw = _raw(row, index)
    if isinstance(raw, datetime):
        return raw.time()
    if isinstance(raw, time):
        return raw
    text = _cell(row, index)
    if not text:
        return None
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, pattern).time()
        except ValueError:
            continue
    raise _RowRefused("order_time is not a time: use HH:MM:SS")


def _placed_at(row: list[object], date_index: int | None, time_index: int | None) -> datetime:
    """The order's date and time (IST, as the format says) as the stored
    naive-UTC clock.

    The time is ``order_time``'s, whichever way the date is written: an ISO
    or slashed date used to be read whole by the id sheet's reader and its
    order_time dropped, filing the order at midnight (review of 27 Oct 2026).
    With no order_time, a date that carries its own time keeps it; one that
    does not is midnight."""
    raw_day = _raw(row, date_index)
    if isinstance(raw_day, datetime):
        typed = raw_day if raw_day.tzinfo is None else raw_day.astimezone(IST).replace(tzinfo=None)
    elif isinstance(raw_day, date):
        typed = datetime.combine(raw_day, time(0, 0))
    else:
        typed = _typed_moment(_cell(row, date_index))
    clock = _clock(row, time_index)
    if clock is not None:
        typed = datetime.combine(typed.date(), clock)
    return typed.replace(tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)


def _tracking_line(row: list[object], index: int | None) -> str | None:
    """``last_tracking_update`` as written: free text ("25-10-2026
    Cancelled"), the way the ledger's own CSV writes it. A spreadsheet that
    took the cell for a date and time hands back a datetime; that is written
    the format's way (DD-MM-YYYY, then HH:MM when it has a time), never
    Python's."""
    raw = _raw(row, index)
    if isinstance(raw, datetime):
        return raw.strftime("%d-%m-%Y %H:%M" if raw.time() != time(0, 0) else "%d-%m-%Y")
    if isinstance(raw, date):
        return raw.strftime("%d-%m-%Y")
    return _text(row, index, "last_tracking_update")


def _who_cancelled(row: list[object], columns: dict[str, int], status: str) -> bool | None:
    """``cancelled_by_user`` as an answer: True for Yes, False for No, None
    when the cell is empty — or when it says No of a unit the row does not say
    was cancelled (its ``cancelled`` cell is not Yes, nor does its status read
    as a cancellation).

    "Cancelled by the user? No" of a unit still on its way answers nothing:
    the ledger's own CSV writes it for every unit that was not cancelled, and
    kept, it would call a cancellation that came later Flipkart's. A Yes is
    kept whatever the status: a cancellation the user asked for may not show
    yet ("Cancellation requested"), or show as a refund."""
    from app.core.order_status import CANCELLED, classify

    said = _yes_no(row, columns.get("cancelled_by_user"), "cancelled_by_user")
    if said is False:
        cancelled = _cell(row, columns.get("cancelled")).lower() in _YES or classify(status) == CANCELLED
        return False if cancelled else None
    return said


def _pincode(row: list[object], index: int | None) -> str | None:
    """``delivery_pincode`` as six digits, or None when the cell is empty."""
    text = _cell(row, index)
    if not text:
        return None
    if not _PINCODE_RE.match(text):
        raise _RowRefused(f"delivery_pincode {text!r} is not a pincode: write its 6 digits, such as 226013")
    return text


def _address(row: list[object], columns: dict[str, int], pincode: str | None) -> tuple[str | None, str | None]:
    """The delivery address as one line, its parts in the order the page
    prints them, and its first line — both None unless the row names a place:
    its first line and its pincode.

    Joined from whatever parts were filled, an address with its pincode left
    out read as a new address — an address change and a pincode of None,
    alerts and all — although the row only said less (review of 27 Oct
    2026); and the ledger's own export, whose "Delivery State" column holds
    the unit's delivery reading, gave every unit the address "in_transit"."""
    line1 = _cell(row, columns.get("delivery_line1"))
    if not line1 or not pincode:
        return None, None
    parts = [
        _cell(row, columns.get(name))
        for name in ("delivery_name", "delivery_line1", "delivery_line2", "delivery_city", "delivery_state", "delivery_pincode", "delivery_phone")
    ]
    return ", ".join(part for part in parts if part)[:MAX_ADDRESS], line1


def _read_row(number: int, row: list[object], columns: dict[str, int]) -> ReportRow:
    platform = (_cell(row, columns.get("platform")) or "").lower()
    if platform != "flipkart":
        raise _RowRefused(f"platform {platform or '(blank)'!r} is not supported yet: Flipkart only for now")
    for name in REQUIRED:
        if not _cell(row, columns.get(name)):
            raise _RowRefused(f"{name} is empty")
    order_id = (_identifier(row, columns.get("order_id"), "order id") or "").upper()
    if not _ORDER_ID_RE.match(order_id):
        raise _RowRefused(f"order id {order_id!r} is not a Flipkart order id (OD followed by digits)")
    # Upper-cased as the ledger stores it (OrderUnit's validator), so "u1"
    # finds the unit "U1" a first upload filed rather than failing the whole
    # report on the unique key (review of 27 Oct 2026).
    unit_id = (_identifier(row, columns.get("unit_id"), "unit id") or "").upper()
    refusal = unit_id_refusal(unit_id)
    if refusal:
        raise _RowRefused(refusal)
    email = _cell(row, columns.get("platform_email")).lower() or None
    if email and not _EMAIL_RE.match(email):
        raise _RowRefused("platform_email is not an email address")
    gstin = (_text(row, columns.get("business_gstin"), "business_gstin") or "").upper() or None
    if gstin and not _GSTIN_RE.match(gstin):
        raise _RowRefused("business_gstin is not a GSTIN (15 characters, such as 09AAAAA0000A1Z5)")
    pincode = _pincode(row, columns.get("delivery_pincode"))
    address, line1 = _address(row, columns, pincode)
    # Read in the order the row's checks always ran, so a row wrong twice is
    # refused for the same reason as before.
    placed_at = _placed_at(row, columns.get("order_date"), columns.get("order_time"))
    status = _text(row, columns.get("status"), "status") or ""
    return ReportRow(
        row_number=number,
        order_id=order_id,
        unit_id=unit_id,
        placed_at=placed_at,
        email=email,
        status=status,
        product=_text(row, columns.get("product"), "product") or "",
        unit_amount=_money(row, columns.get("unit_amount"), "unit_amount"),
        quantity=_count(row, columns.get("quantity"), "quantity"),
        total=_money(row, columns.get("total_order_amount"), "total_order_amount"),
        promised=_day(row, columns.get("estimated_delivery"), "estimated_delivery"),
        delivered=_day(row, columns.get("actual_delivery_date"), "actual_delivery_date"),
        tracking_id=_identifier(row, columns.get("tracking_id"), "tracking id"),
        seller=_text(row, columns.get("seller_name"), "seller_name"),
        business_name=_text(row, columns.get("business_name"), "business_name"),
        business_gstin=gstin,
        billing_phone=_text(row, columns.get("billing_phone"), "billing_phone"),
        billing_pincode=_text(row, columns.get("billing_pincode"), "billing_pincode"),
        cash_on_delivery=_yes_no(row, columns.get("cash_on_delivery"), "cash_on_delivery"),
        delivery_address=address,
        delivery_line1=line1,
        delivery_pincode=pincode if address is not None else None,
        received=_yes_no(row, columns.get("received"), "received"),
        shipment_partner=_text(row, columns.get("shipment_partner"), "shipment_partner"),
        last_tracking=_tracking_line(row, columns.get("last_tracking_update")),
        refund_status=_text(row, columns.get("refund_status"), "refund_status"),
        refund_amount=_money(row, columns.get("refund_amount"), "refund_amount"),
        refund_method=_text(row, columns.get("refund_method"), "refund_method"),
        cancelled_by_user=_who_cancelled(row, columns, status),
    )


def read_report(data: bytes, filename: str) -> Report:
    """Read a report. Each row is checked on its own: an unreadable one is
    refused with its row number and the reason, the readable ones are kept,
    in file order, and a unit id given twice is refused the second time.

    A report refused as a whole raises :class:`ReportError`. A file that is
    no sheet at all raises whatever the spreadsheet library raises, which may
    quote a cell, so it is never shown as it is."""
    rows = _read_rows(data, filename)
    report = Report()
    if not rows:
        raise ReportError("the report is empty: keep the template's header row and add one row per unit")
    if len(rows) - 1 > MAX_ROWS:
        raise ReportError(f"the report has {len(rows) - 1:,} rows; at most {MAX_ROWS:,} are read at once")
    columns: dict[str, int] = {}
    for index, cell in enumerate(rows[0]):
        name = _COLUMN.get(normalise_header(cell))
        if name and name not in columns:
            columns[name] = index
    missing = [name for name in REQUIRED if name not in columns]
    if missing:
        raise ReportError(
            "the header row needs the columns " + ", ".join(REQUIRED) + f" (missing: {', '.join(missing)})"
        )
    report.not_kept = [name for name in NOT_KEPT if name in columns]
    seen: dict[str, int] = {}
    for number, row in enumerate(rows[1:], start=2):
        if not any(_cell(row, index) for index in range(len(row))):
            continue  # a blank line
        try:
            parsed = _read_row(number, row, columns)
        except _RowRefused as exc:
            report.rejected.append(Rejected(number, str(exc)))
            continue
        if parsed.unit_id in seen:
            report.rejected.append(Rejected(number, f"unit id {parsed.unit_id} is given twice (first on row {seen[parsed.unit_id]})"))
            continue
        seen[parsed.unit_id] = number
        report.rows.append(parsed)
    return report


# --- into the ledger -------------------------------------------------------------------------
@dataclass
class ReportOutcome:
    created: int = 0
    updated: int = 0
    #: Rows that moved nothing because the ledger already says what they say.
    unchanged: int = 0
    #: Rows naming a status, date, tracking id, seller or address other than
    #: the one Flipkart's own page gave for that unit: Flipkart's reading was
    #: kept, and the answer says so rather than a bare "updated". One that
    #: filled nothing is counted here alone — never as unchanged, since the
    #: ledger does not say what it says (review of 27 Oct 2026).
    kept_platform: int = 0
    rejected: list[Rejected] = field(default_factory=list)
    not_kept: list[str] = field(default_factory=list)


def _same_place(held: str | None, held_pincode: str | None, row: ReportRow) -> bool:
    """Whether the row's address names the place a unit's held address does.

    The alerts' own reading of "the same address" (spec C-5;
    app.core.alerts.address_in_book): the same pincode, and the row's first
    line found, as whole words, in the held text. The read-back keeps the
    order page's block on one line ("Home · Tower 5, 1005, … 226013 · name ·
    phone") and a report its parts joined by commas, so two texts naming one
    place seldom read alike: compared whole, every row for a unit the
    read-back had seen counted as contradicting its address (review of 27 Oct
    2026). The very same text is the same place whatever its first line; a
    first line too short to tell addresses apart matches nothing else, as in
    the alerts."""
    from app.core.alerts import MIN_LINE1_CHARS
    from app.core.order_units import normalised_line

    shown = normalised_line(held)
    if not shown or row.delivery_address is None:
        return False
    given = normalised_line(row.delivery_address)
    if shown == given:
        return True
    held_pin = (held_pincode or "").strip()
    if held_pin and held_pin != row.delivery_pincode:
        return False
    held_words, given_words = set(shown.split()), set(given.split())
    if held_words <= given_words or given_words <= held_words:
        # One says what the other does, with a part left out or added: a
        # phone, a city, a first line too short to compare alone ("12"), or
        # the pincode a held text never had (checker of 27 Oct 2026).
        return True
    if not held_pin and f" {row.delivery_pincode} " not in f" {shown} ":
        # A held address whose pincode was never read: its text carries it.
        return False
    line1 = normalised_line(row.delivery_line1)
    return len(line1) >= MIN_LINE1_CHARS and f" {line1} " in f" {shown} "


def _says_less(held: str, given: str) -> bool:
    """Whether ``given`` says nothing ``held`` does not: every word of it is
    in the held address (the same text, or parts of it left out)."""
    from app.core.order_units import normalised_line

    return set(normalised_line(given).split()) <= set(normalised_line(held).split())


def _set_unit(unit, row: ReportRow, *, read_back: bool, now: datetime) -> bool:
    """Write a row onto a unit; True if anything moved. A unit the read-back
    has seen only has its empty fields filled; an empty cell blanks nothing.

    The same for what no read-back reads — the courier, the report's own last
    tracking update, the refund, who cancelled (spec FR-022) — so a unit the
    read-back has seen keeps a value it holds, whatever a later report says,
    and the read-back's own reading of them, once it has one, is never
    written over."""
    from app.core.order_status import UNDELIVERED, classify
    from app.io.orders_csv import page_tracking_line

    moved = False
    tracking_line = row.last_tracking
    if tracking_line is not None and tracking_line == page_tracking_line(unit):
        # The very line the ledger writes from Flipkart's page — its own CSV
        # handed back — is no report's word: kept, it would outlive the
        # page's next reading.
        tracking_line = None
    values = {
        "platform_status": row.status,
        "promised_date": row.promised,
        "delivered_date": row.delivered,
        "tracking_id": row.tracking_id,
        "seller": row.seller,
        "shipment_partner": row.shipment_partner,
        "last_tracking_text": tracking_line,
        "refund_status": row.refund_status,
        "refund_amount": row.refund_amount,
        "refund_method": row.refund_method,
        # False is a value here — the report's "No" — and only None is "said nothing".
        "cancelled_by_user": row.cancelled_by_user,
    }
    for name, value in values.items():
        if value is None:
            continue
        current = getattr(unit, name)
        if read_back and current is not None:
            continue
        if current != value:
            setattr(unit, name, value)
            moved = True
    if row.promised is not None and unit.first_promised_date is None:
        unit.first_promised_date = unit.promised_date
        moved = True
    if row.delivery_address is not None and not (read_back and unit.delivery_address is not None):
        if unit.delivery_address is None:
            # The first snapshot, which is no change (as for the read-back's).
            unit.delivery_address, unit.delivery_pincode = row.delivery_address, row.delivery_pincode
            moved = True
        elif not _same_place(unit.delivery_address, unit.delivery_pincode, row):
            # Another place than the one held: the address-change alert's
            # cue, as when a read-back sees one (spec FR-045).
            unit.address_changed_at = now
            unit.delivery_address, unit.delivery_pincode = row.delivery_address, row.delivery_pincode
            moved = True
        else:
            # The same place: no change of address. The held text stands when
            # the row says less — a row that leaves the phone or the city out
            # blanks neither — and a unit never read back takes the row's own
            # text when it says anything the held one does not (a corrected
            # flat or phone), still with no change flagged (checker of 27 Oct).
            if not _says_less(unit.delivery_address, row.delivery_address):
                unit.delivery_address = row.delivery_address
                moved = True
            if unit.delivery_pincode is None:
                unit.delivery_pincode = row.delivery_pincode
                moved = True
    # The day the ledger's Delivery date column, its filter and the "Product
    # invoice missing" clock read (``delivery_date``), which a report never
    # wrote though the read-back always does (review of 27 Oct 2026): the
    # read-back's rule, from what the unit now holds — the day it was
    # delivered, else the promise while it is on its way. A cancelled or
    # returned unit's is the day of that event on Flipkart's page, which no
    # report gives, so it is left as it is — like any value a row cannot give.
    state = unit.delivery_state or classify(unit.platform_status)
    if unit.delivered_date is not None:
        day = unit.delivered_date
    elif state in UNDELIVERED:
        day = unit.promised_date
    else:
        day = None
    if day is not None and unit.delivery_date != day and not (read_back and unit.delivery_date is not None):
        unit.delivery_date = day
        moved = True
    return moved


def _platform_said_otherwise(unit, row: ReportRow) -> bool:
    """Whether the row names a value that Flipkart's own page gave otherwise
    for a unit the read-back has seen — one :func:`_set_unit` leaves as it is.
    Spacing and case aside; days compared as days; two wordings of one state
    ("Delivered" and "Delivered on Oct 27") agree; an address by its place
    (:func:`_same_place`), not by its wording."""
    from app.core import order_status

    def words(text: str) -> str:
        return " ".join(text.split()).casefold()

    def state(text: str) -> str:
        known = order_status.classify(text)
        return known if known != order_status.UNKNOWN else words(text)

    pairs = (
        (row.status, unit.platform_status, state),
        (row.promised, unit.promised_date, datetime.date),
        (row.delivered, unit.delivered_date, datetime.date),
        (row.tracking_id, unit.tracking_id, words),
        (row.seller, unit.seller, words),
    )
    if any(said is not None and held is not None and key(said) != key(held) for said, held, key in pairs):
        return True
    return (
        row.delivery_address is not None
        and unit.delivery_address is not None
        and not _same_place(unit.delivery_address, unit.delivery_pincode, row)
    )


def _placeholder_for(order, row: ReportRow):
    """The placeholder (a unit with no id yet) a new unit id is given: the
    first whose read-back reading agrees with the row, else the first never
    read back, else the first by position; None when the order has none.

    A read-back that opens an order by id reads a page with no unit links and
    files what it saw on the next placeholder in position order
    (app.core.order_units.apply_observations): a reading with no unit id. By
    position alone, a report's unit could take another unit's reading, which
    the read-back rule then keeps over the report's (review of 27 Oct 2026)."""
    placeholders = [unit for unit in sorted(order.units, key=lambda unit: unit.position) if unit.unit_id is None]
    agreeing = next(
        (
            unit
            for unit in placeholders
            if unit.synced_at is not None and unit.platform_status and not _platform_said_otherwise(unit, row)
        ),
        None,
    )
    if agreeing is not None:
        return agreeing
    unread = next((unit for unit in placeholders if unit.synced_at is None), None)
    if unread is not None:
        return unread
    return placeholders[0] if placeholders else None


def _fill_order(order, row: ReportRow) -> bool:
    """Fill the order's empty fields from a row; never overwrite one."""
    moved = False
    fills = {
        "model": row.product,
        "qty": row.quantity,
        "amount": row.total,
        "placed_at": row.placed_at,
        "gst_name": row.business_name,
        "gstin": row.business_gstin,
        "billing_phone": row.billing_phone,
        "pin": row.billing_pincode,
    }
    for name, value in fills.items():
        if value is not None and getattr(order, name) in (None, ""):
            setattr(order, name, value)
            moved = True
    if row.cash_on_delivery is not None and order.payment_mode == UNKNOWN_PAYMENT:
        # The report's word for a mode no run chose. Its "No" was dropped, as
        # saying nothing of how the order was paid (card, UPI…), so the CSV's
        # Cash on Delivery stayed blank and the report's answer was lost
        # (review of 27 Oct 2026); it is kept as "prepaid", never as a card.
        order.payment_mode = _payment_mode(row.cash_on_delivery)
        moved = True
    return moved


def _payment_mode(cash_on_delivery: bool | None) -> str:
    """``Order.payment_mode`` for a report's Cash on Delivery cell: "cod" for
    Yes, :data:`PREPAID` for No, :data:`UNKNOWN_PAYMENT` when it is empty."""
    if cash_on_delivery is None:
        return UNKNOWN_PAYMENT
    return "cod" if cash_on_delivery else PREPAID


def _mirror(order) -> None:
    """The order row's reading, from its units, for an order the read-back has
    never seen — the read-back's own rule otherwise mirrors it at its next
    pass. The unit that stands for the order is the shared one
    (app.core.order_units.representative_unit). An order Flipkart took (it has
    an id) is failed when every unit that says anything reads cancelled, and
    put back to placed when that stops being so — a later report showing a
    unit on its way or delivered that an earlier one left out or got wrong, as
    the read-back's own _revive does (review of 27 Oct 2026). An order without
    an id is a run's, never touched here."""
    from app.core import cancellations, order_status
    from app.core.order_units import representative_unit
    from app.db.models import OrderStatus

    unit = representative_unit(order)
    if unit is not None:
        for name in ("platform_status", "tracking_id", "seller"):
            value = getattr(unit, name)
            if value is not None and getattr(order, name, None) != value:
                setattr(order, name, value)
    said = [u for u in order.units if u.platform_status]
    if not said or not order.order_id:
        return

    def cancelled(u) -> bool:
        state = order_status.classify(u.platform_status)
        return state == order_status.CANCELLED or (
            state == order_status.RETURNED and cancellations.shows_cancellation(u)
        )

    if all(cancelled(u) for u in said):
        if order.status == OrderStatus.placed:
            order.status = OrderStatus.failed
    elif order.status == OrderStatus.failed and any(
        order_status.classify(u.platform_status) in (order_status.IN_TRANSIT, order_status.DELIVERED) for u in said
    ):
        # Only a unit on its way or delivered puts the order back, as the
        # read-back's _revive: "Refund Completed" on a cancelled card order,
        # or "Returned to origin", is no sign the order stands (checker of
        # 27 Oct 2026).
        order.status = OrderStatus.placed


def _settle(order, unit_price: float | None) -> None:
    """An order the read-back has never seen, made whole once a report's rows
    for it are in: ``qty`` at least the units it holds, a missing total priced
    from a unit's price, and its reading mirrored (:func:`_mirror`).

    A new order filed from rows that left the quantity blank had qty 1 however
    many units it held, so the page and the CSV showed the whole total as
    each unit's price beside a quantity of 1, and unit limits — a sum of
    ``qty`` — counted one unit for two (review of 27 Oct 2026). ``qty`` is the
    order's intent and is never lowered; the read-back, which never writes it,
    leaves it to whoever filed the order."""
    if order.synced_at is not None:
        return
    qty = max(order.qty or 1, len(order.units))
    if order.qty != qty:
        order.qty = qty
    if order.amount is None and unit_price is not None:
        order.amount = round(unit_price * qty, 2)
    _mirror(order)


def import_report(db, report: Report, *, uploaded_by: str, commit_every: int | None = None) -> ReportOutcome:
    """Bring a read report into the ledger. See the module docstring for the
    rules.

    The caller commits. With ``commit_every``, this commits as it goes, after
    at least that many rows and only where every order filed since the last
    commit has no row further down — so an order is never left half-filed,
    each committed order is whole (its quantity, price and reading settled),
    and the answer still counts the whole report. Without it, the report is
    one transaction."""
    from sqlalchemy import func, select

    from app.db.models import Account, utcnow

    out = ReportOutcome(rejected=list(report.rejected), not_kept=list(report.not_kept))
    now = utcnow()
    emails = {row.email for row in report.rows if row.email}
    accounts: dict[str, int] = {}
    if emails:
        found = db.execute(select(Account.id, Account.email).where(func.lower(Account.email).in_(emails))).all()
        accounts = {email.lower(): account_id for account_id, email in found}
    # What an order's rows say of it, the first that says it: the quantity a
    # new order is filed with (so its placeholders are made at once), and the
    # unit price an order with no total is priced from.
    stated_qty: dict[str, int] = {}
    unit_price: dict[str, float] = {}
    last_row: dict[str, int] = {}
    for index, row in enumerate(report.rows):
        if row.quantity is not None:
            stated_qty.setdefault(row.order_id, row.quantity)
        if row.unit_amount is not None:
            unit_price.setdefault(row.order_id, row.unit_amount)
        last_row[row.order_id] = index

    pending: dict[int, Order] = {}
    filed = 0
    for index, row in enumerate(report.rows):
        order = _file_row(db, row, out, accounts=accounts, stated_qty=stated_qty, uploaded_by=uploaded_by, now=now)
        filed += 1
        if order is not None:
            pending[order.id] = order
        if (
            commit_every
            and filed >= commit_every
            and all(last_row.get(held.order_id, -1) <= index for held in pending.values())
        ):
            for held in pending.values():
                _settle(held, unit_price.get(held.order_id))
            db.commit()
            pending.clear()
            filed = 0

    for held in pending.values():
        _settle(held, unit_price.get(held.order_id))
    out.rejected.sort(key=lambda r: r.row_number)
    return out


def _file_row(
    db,
    row: ReportRow,
    out: ReportOutcome,
    *,
    accounts: dict[str, int],
    stated_qty: dict[str, int],
    uploaded_by: str,
    now: datetime,
) -> Order | None:
    """File one row: its unit found, or made under its order; the row counted
    in ``out``. Returns the unit's order, or None for a row refused."""
    from sqlalchemy import select

    from app.core.order_units import PLATFORM, SOURCE_UPLOAD, ensure_units
    from app.db.models import OrderStatus

    unit = db.scalar(select(OrderUnit).where(OrderUnit.platform == PLATFORM, OrderUnit.unit_id == row.unit_id))
    created = False
    if unit is not None:
        order = unit.order
        if order.order_id != row.order_id:
            other = order.order_id or f"order #{order.id}"
            out.rejected.append(Rejected(row.row_number, f"unit id {row.unit_id} belongs to {other}, not {row.order_id}"))
            return None
        if row.email and accounts.get(row.email) != order.account_id:
            # As for a unit not yet held: the row's billing details would be
            # written onto another account's order (review of 27 Oct 2026).
            out.rejected.append(
                Rejected(row.row_number, f"{row.order_id} is in the ledger under another account than {row.email}")
            )
            return None
    else:
        holders = db.scalars(select(Order).where(Order.order_id == row.order_id)).all()
        if len(holders) > 1:
            out.rejected.append(Rejected(row.row_number, f"{row.order_id} is in the ledger twice; settle that first"))
            return None
        order = holders[0] if holders else None
        if order is not None:
            if row.email and accounts.get(row.email) != order.account_id:
                out.rejected.append(
                    Rejected(row.row_number, f"{row.order_id} is in the ledger under another account than {row.email}")
                )
                return None
        else:
            if not row.email:
                out.rejected.append(Rejected(row.row_number, "platform_email is needed to file an order the ledger does not hold"))
                return None
            account_id = accounts.get(row.email)
            if account_id is None:
                out.rejected.append(Rejected(row.row_number, f"no account with the email {row.email}"))
                return None
            awaiting = db.scalars(
                select(Order.id).where(
                    Order.account_id == account_id,
                    Order.status == OrderStatus.unconfirmed,
                    Order.order_id.is_(None),
                )
            ).all()
            if awaiting:
                # A run sent an order on this account that Flipkart never
                # confirmed: it may be this one. A second row for it would
                # count its units twice, so the operator settles it first,
                # as the order-id sheet asks too.
                listed = ", ".join(f"#{pk}" for pk in awaiting[:3])
                out.rejected.append(
                    Rejected(
                        row.row_number,
                        f"{row.email} has an unconfirmed order awaiting settling ({listed}); if "
                        f"{row.order_id} is that order, settle it in the ledger (View → Found it) first",
                    )
                )
                return None
            order = Order(
                account_id=account_id,
                order_id=row.order_id,
                status=OrderStatus.placed,
                placed_at=row.placed_at,
                model=row.product,
                # The quantity any of its rows states; with none, what its
                # units come to once they are all in (_settle).
                qty=stated_qty.get(row.order_id),
                amount=row.total,
                payment_mode=_payment_mode(row.cash_on_delivery),
                gst_name=row.business_name,
                gstin=row.business_gstin,
                billing_phone=row.billing_phone,
                pin=row.billing_pincode,
            )
            db.add(order)
            ensure_units(order, source=SOURCE_UPLOAD)
        unit = _placeholder_for(order, row)
        if unit is None:
            unit = OrderUnit(platform=PLATFORM, position=len(order.units) + 1, source=SOURCE_UPLOAD)
            order.units.append(unit)
        unit.unit_id = row.unit_id
        created = True
    read_back = unit.synced_at is not None
    contradicted = read_back and _platform_said_otherwise(unit, row)
    moved = _set_unit(unit, row, read_back=read_back, now=now)
    moved = _fill_order(order, row) or moved
    if row.received and unit.received_at is None:
        unit.received_at, unit.received_by = now, uploaded_by
        moved = True
    # Visible to the next row: autoflush is off, and a second row of the
    # same order must find this one's unit and order.
    db.flush()
    if contradicted:
        out.kept_platform += 1
    if created:
        out.created += 1
    elif moved:
        out.updated += 1
    elif not contradicted:
        out.unchanged += 1
    return order
