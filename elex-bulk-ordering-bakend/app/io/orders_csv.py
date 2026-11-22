"""The ledger's CSV exports: the spec's ``order_units_<date>_<HHMM>.csv`` and
the two sheets the owner keeps by hand (25 Oct 2026): the out-for-delivery
sheet and the order-data sheet, each with exactly its columns.

Two things a plain ``csv.writer`` would get wrong for these files, both learned
from the reference product's own export opened in Excel (ui-evidence X1):

* Dates are written as DD-MM-YYYY and times as HH:MM:SS in India Standard
  Time, which is how the operator reads them; the database holds UTC. The
  owner's two sheets write the short DD/MM/YY their own files use.
* Long digit-only identifiers (unit ids, IMEIs, phone numbers, card numbers)
  are written so that a spreadsheet keeps every digit. Excel turns
  ``439123456789012345678`` into ``4.39E+20`` on open and the digits are gone
  for good; the ``="..."`` form is the one way a CSV can tell it "this is text".
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.cancellations import (
    BY_PLATFORM,
    BY_USER,
    cancelled_by,
    outcome_value,
    says_nothing,
    unit_cancelled_by_user,
    unit_was_cancelled,
)
from app.core.order_filter import ExportRow
from app.core.order_status import (
    DELIVERED,
    RETURNED,
    SETTLED,
    cancelled_by_platform,
    classify,
    paid_on_delivery,
    unit_days_late,
)
from app.db.models import Gst, Order, OrderStatus, OrderUnit

IST = timezone(timedelta(hours=5, minutes=30))
UTC_TZ = timezone.utc
#: Names per IN clause: SQLite's bound-parameter ceiling is 999 on older builds.
_CHUNK = 500

#: The spec's Order Unit columns, in the recorded export's order (ui-evidence
#: X1 / the Order Unit data model; FR-017). A column the ledger does not hold
#: yet is written blank rather than left out, so the file always has the
#: shape the operator's other tools expect. "Internal Order" is written as
#: the unit's ``source`` (C-3's decision: run / sync / upload).
SPEC_HEADERS = [
    "Platform",
    "Order Date",
    "Order Time",
    "Platform Email",
    "Status",
    "Product",
    "Order ID",
    "Unit ID",
    "Ref Code",
    "Source",
    "PO Number",
    "Unit Amount",
    "Quantity",
    "Total Order Amount",
    "Estimated Delivery",
    "Actual Delivery Date",
    "Tracking ID",
    "Last Tracking Update",
    "Shipment Partner",
    "Business Name",
    "Business GST No",
    "Billing Name",
    "Billing Phone",
    "Billing Pincode",
    "Card Number",
    "RTGS Source",
    "RTGS Beneficiary",
    "Cash on Delivery",
    "EWB Number",
    "Invoice on file",
    "Refund Status",
    "Refund Amount",
    "Refund Method",
    "Cancelled",
    "Cancelled by User",
    "Platform Order Status",
    "Seller Name",
    "IMEI",
    "Received",
]

#: What this portal knows beyond the spec's list, appended after it so a
#: reader of the spec's columns finds them where they expect and loses nothing.
PORTAL_HEADERS = [
    "Outcome",
    "Color",
    "Unit No",
    "Item ID",
    "Target",
    "Payment Mode",
    "Delivery State",
    "Delivery OTP",
    "Phone Last 6",
    "Brand",
    "Category",
    "Run",
    "Read Back At",
    "Recorded At",
    "Product Link",
    # Added 26 Oct 2026, at the very end so every column before it keeps its place.
    "Cancellation Reason",
    # How the unit is paid and the order page's "Total amount", from its price
    # details, and how many days its delivery came after the first promise.
    # The total is what was paid, or is to be paid on delivery, or nothing at
    # all for a Cash on Delivery unit that was cancelled — so it is named for
    # what it is, not "Amount Paid" (its name until 26 Oct 2026, same place).
    "Paid By",
    "Order Total (Flipkart)",
    "Days Late",
]

HEADERS = SPEC_HEADERS + PORTAL_HEADERS

#: A run of 12 or more digits is an identifier, never a quantity.
_LONG_DIGITS = re.compile(r"^\d{12,}$")
#: In a column that only ever holds an identifier — a phone, an IMEI — even a
#: short run of digits is one (NFR-008: a 10-digit phone is not a number).
#: Any all-digit value in an identifier-only column (phone, IMEI, card, OTP) is
#: kept as text: a four-digit delivery OTP "0123" must not become 123 (audit F-37).
_ANY_DIGITS = re.compile(r"^\d+$")
#: A phone as a person types it: digits, perhaps after a "+", perhaps grouped
#: by spaces or dashes ("+919876543210", "98765 43210", "+91-98765-43210").
#: In an identifier column it is kept as text like an all-digit value (SC-007):
#: the leading-space guard changes the value, and a spreadsheet still reads
#: " +919876543210" as the number 919876543210.
_PHONE_SHAPED = re.compile(r"^\+?\d+(?:[ -]+\d+)*$")

#: The platform's name as the owner's own sheets write it.
PLATFORM_WORD = "FLIPKART"
#: What the Status cell says before Flipkart has been read back: the tool's
#: own outcome, in the words the ledger page uses.
OUTCOME_WORD: dict[OrderStatus, str] = {
    OrderStatus.pending: "Pending",
    OrderStatus.placed: "Placed",
    OrderStatus.unconfirmed: "Unconfirmed",
    OrderStatus.failed: "Failed",
    OrderStatus.over_target: "Over target",
}
#: And for an order Flipkart took and then cancelled, which is stored as
#: failed too: who cancelled it, as the ledger page's Outcome badge words it.
#: "Failed" is only ever a run that sent nothing.
CANCELLED_WORD: dict[str, str] = {BY_PLATFORM: "Cancelled by Flipkart", BY_USER: "Cancelled by user"}


def to_ist(value: datetime | None) -> datetime | None:
    """The stored (naive UTC) clock as India Standard Time."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST)


def fmt_date(value: datetime | None) -> str:
    local = to_ist(value)
    return local.strftime("%d-%m-%Y") if local else ""


def fmt_short_date(value: datetime | None) -> str:
    """DD/MM/YY — the owner's own sheets write dates this way."""
    local = to_ist(value)
    return local.strftime("%d/%m/%y") if local else ""


def fmt_time(value: datetime | None) -> str:
    local = to_ist(value)
    return local.strftime("%H:%M:%S") if local else ""


def fmt_datetime(value: datetime | None) -> str:
    local = to_ist(value)
    return local.strftime("%d-%m-%Y %H:%M:%S") if local else ""


#: A cell starting with one of these is read as a formula by a spreadsheet;
#: free text from Flipkart or a user must never be (audit F-36, 25 Oct 2026).
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def text_guard(value: str | None) -> str:
    """Keep a long digit-only identifier intact when the file is opened in a
    spreadsheet, and keep free text that happens to start like a formula from
    being run as one (a leading space, which spreadsheets show as text);
    anything else passes through unchanged."""
    if value is None:
        return ""
    text = str(value)
    if _LONG_DIGITS.match(text):
        return f'="{text}"'
    if text.startswith(_FORMULA_STARTS):
        return " " + text
    return text


#: The ="…" form this module itself writes an identifier in (text_guard and
#: identifier_cell): digits, spaces, a "+" and dashes inside the quotes, and
#: nothing else, so free text shaped like it can never pass as one.
_OUR_IDENTIFIER = re.compile(r'="[0-9 +\-]*"')
#: A plain number, which a spreadsheet reads as one: a discount of -1750.00
#: must stay a number to be summed, and is no formula.
_PLAIN_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def safe_cell(value: str | float | int | None) -> str:
    """The last guard every cell of every CSV export passes through: anything
    starting like a formula gets text_guard's leading space, whatever column
    it is in — product, seller, business name, status, colour, link…

    Each free-text column used to be guarded one by one, and most were not
    (Flipkart's wording, the seller, the product, the business name: review
    of 27 Oct 2026), so a name starting with "=" or "@" ran as a formula in
    the accountant's spreadsheet. Two kinds of cell pass as they are: an
    identifier this module wrote as ="…" (digits only inside), and a plain
    number, a negative one included."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if not text or _OUR_IDENTIFIER.fullmatch(text) or _PLAIN_NUMBER.fullmatch(text):
        return text
    return " " + text if text.startswith(_FORMULA_STARTS) else text


def safe_row(cells: Iterable[str | float | int | None]) -> list[str]:
    """:func:`safe_cell` over a whole line."""
    return [safe_cell(cell) for cell in cells]


def identifier_cell(value: str | None) -> str:
    """A cell of a column that holds identifiers only (phone, IMEI, card):
    kept as text whatever its length, blank when there is nothing.

    A billing phone is typed by a person, so a phone-shaped value — digits
    with a "+", spaces or dashes, "+91 98765 43210" — is kept as text the same
    way, exactly as typed. Anything else goes through :func:`text_guard`, so a
    value starting like a formula is never run as one."""
    if value is None:
        return ""
    text = str(value)
    return f'="{text}"' if _ANY_DIGITS.match(text) or _PHONE_SHAPED.match(text) else text_guard(text)


def phone_last_6(phone: str | None) -> str:
    """The last six digits of the account's phone, as the owner's sheets carry
    it — kept as text, since a leading zero would otherwise vanish."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return identifier_cell(digits[-6:]) if digits else ""


def yes_no(value: bool | None) -> str:
    return "Yes" if value else "No"


def money(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def status_word(unit: OrderUnit, order: Order, *, requested: bool = False, reading=None) -> str:
    """Flipkart's own wording once read back; the tool's outcome until then.

    A unit with no wording of an order Flipkart took and then cancelled — a
    unit it never listed — says who cancelled the order (:data:`CANCELLED_WORD`),
    ``requested`` meaning the tool holds a cancel request for the unit and
    ``reading`` being its order's verdict, which such a unit takes (as on the
    ledger page).
    """
    if unit.platform_status:
        return unit.platform_status
    who = cancelled_by(order, unit, requested=requested, reading=reading)
    if who is not None:
        return CANCELLED_WORD[who]
    return OUTCOME_WORD.get(order.status, order.status.value if order.status else "")


def delivery_days(unit: OrderUnit) -> tuple[datetime | None, datetime | None]:
    """``(estimated, actual)``: the day Flipkart promised and the day it
    delivered — the ledger's one rule for the two, which the CSV and the
    Order Details API each write in their own format.

    Read apart since 26 Oct 2026 (``promised_date``, ``delivered_date``), so a
    delivered unit keeps its promise beside its delivery day and shows whether
    it came late. Beside a delivery the promise is the first one Flipkart gave
    (``first_promised_date``), the one its lateness is counted from; while the
    unit is on its way it is the latest, the day it is now expected. A row
    read back before then has only ``delivery_date``: the promise while on its
    way, the delivery day once delivered.

    ``delivered_date`` is only ever written from a delivery, so a unit
    delivered and since sent back (a return, a refund, a return withdrawn)
    keeps its promise and its delivery day here, as on the ledger page. For a
    cancelled, failed or returned unit ``delivery_date`` is the date of that
    event instead (a cancelled order's timeline ends "Cancelled, Oct 24" —
    seen live on 25 Oct 2026), which is neither a promise nor a delivery, so a
    unit settled with no delivery day shows neither.
    """
    state = unit.delivery_state or classify(unit.platform_status)
    if state == DELIVERED:
        return unit.first_promised_date or unit.promised_date, unit.delivered_date or unit.delivery_date
    if unit.delivered_date is not None:
        return unit.first_promised_date or unit.promised_date, unit.delivered_date
    if state in SETTLED:
        return None, None
    return unit.promised_date or unit.delivery_date, None


def delivery_dates(unit: OrderUnit) -> tuple[str, str]:
    """:func:`delivery_days` as DD-MM-YYYY, blank where there is none."""
    estimated, actual = delivery_days(unit)
    return fmt_date(estimated), fmt_date(actual)


def unit_amount(order: Order, units_in_order: int | None = None) -> float | None:
    """The order's amount shared over its units: the CSV's Unit Amount and the
    ledger page's (app.schemas.order.OrderOut.from_unit), one rule for both.
    None when the order has no amount.

    Over the order's quantity, or over the units it holds when they are more.
    An order can hold more units than its quantity says — an order report's
    rows of one order id with the quantity 1, or none given — and shared over
    the quantity alone each unit took the whole amount, the shares adding up
    to more than the order (review of 27 Oct 2026). Never over fewer units
    than the quantity: the units of an order not all on file yet do not each
    take a bigger share.
    """
    if order.amount is None:
        return None
    qty = order.qty if order.qty and order.qty > 0 else 1
    return order.amount / max(qty, units_in_order or 1)


def gst_numbers(db: DbSession, orders: list[Order]) -> dict[int, str | None]:
    """The GSTIN per order, to stand beside its business name: the one recorded
    with the order, else the GST book's registration for that name — and
    None, never a guess, when the book holds none or more than one for it (one
    per state is normal). The Order Details API's customer GSTIN and the CSV's
    Business GST No, one rule for both. The book is read only when an order
    names its business without a GSTIN: one query per chunk of names."""
    names = sorted({order.gst_name for order in orders if order.gst_name and not order.gstin})
    by_name: dict[str, set[str]] = {}
    for start in range(0, len(names), _CHUNK):
        rows = db.execute(
            select(Gst.business_name, Gst.gstin).where(Gst.business_name.in_(names[start : start + _CHUNK]))
        ).all()
        for name, gstin in rows:
            by_name.setdefault(name, set()).add(gstin)
    numbers: dict[int, str | None] = {}
    for order in orders:
        if order.gstin:
            numbers[order.id] = order.gstin
            continue
        candidates = by_name.get(order.gst_name or "", set())
        numbers[order.id] = next(iter(candidates)) if len(candidates) == 1 else None
    return numbers


def business_gst(order: Order, unit: OrderUnit, gst_number: str | None = None) -> tuple[str, str]:
    """The spec's Business Name and Business GST No: one business's pair,
    never one's name beside another's GSTIN.

    The order's own pair whenever it names either — the run's choice, or a
    report's — with ``gst_number`` (:func:`gst_numbers`: the GST book's one
    registration for the name) where it holds no GSTIN, or blank; the pair the
    order page says it was billed under only when the order names neither (an
    order handed in by id has no run's). The two used to fall back apart, so
    an order naming its business without a GSTIN was written beside the GSTIN
    its page showed, which can be another entity's (review of 27 Oct 2026).
    """
    if order.gst_name or order.gstin:
        gstin = order.gstin or gst_number
        if not gstin and _same_business(order.gst_name, unit.billed_business_name):
            # The page billed this very business: its GSTIN is this pair's,
            # where the GST book holds none, or several, for the name.
            gstin = unit.billed_gstin
        return order.gst_name or "", gstin or ""
    return unit.billed_business_name or "", unit.billed_gstin or ""


def _same_business(one: str | None, other: str | None) -> bool:
    """Two business names naming one business, spacing and case aside."""
    first, second = " ".join((one or "").split()).casefold(), " ".join((other or "").split()).casefold()
    return bool(first) and first == second


def cancelled_by_user_cell(unit: OrderUnit, cancelled: bool, *, requested: bool = False, reading=None) -> str:
    """The spec's "Cancelled by User" for one unit: "Yes" when someone asked
    the tool to cancel it (``requested``: a cancel request open or done), the
    platform's status or reason says the user (the account) cancelled, or an
    order report said so; "No" when the unit was not cancelled (``cancelled``,
    from app.core.cancellations.unit_was_cancelled), when an order report said
    the user did not, or when the wording clearly says Flipkart or its seller
    did ("Sorry, your order was cancelled as per seller policy…"); and blank
    when it was cancelled and nothing says by whom — like Cash on Delivery for
    an order whose mode is not known."""
    if not cancelled:
        return "No"
    if unit_cancelled_by_user(unit, requested=requested):
        return "Yes"
    if unit.cancelled_by_user is False:
        # The report's "No" (spec FR-022), which nothing above outweighed.
        return "No"
    # A unit that says nothing itself (one the platform never listed) goes
    # with its order: "Yes" when the order was the account's cancellation, as
    # the page says; otherwise nothing on this row says by whom.
    if reading is not None and says_nothing(unit, requested=requested):
        return "Yes" if reading.user_cancelled else ""
    return "No" if cancelled_by_platform(unit.platform_status, unit.cancel_reason) else ""


def refund_status(order: Order, unit: OrderUnit) -> str | None:
    """The spec's "Refund Status" of one unit, the CSV's and the ledger
    page's: Flipkart's own wording once it has refunded a cancelled unit
    ("Refund Completed"), which the portal reads as returned — so the row
    says the money is back while still saying the unit was cancelled — else
    what an order report said of the refund (``OrderUnit.refund_status``,
    spec FR-022), else None.

    Flipkart's wording first, as wherever a report meets the read-back: a
    report's "Refund pending" must not stand over a page that says the money
    is back. Only a cancellation's refund is read off the wording — a return
    after a delivery is no cancellation's — while a report's refund is
    whatever refund it names."""
    state = unit.delivery_state or classify(unit.platform_status)
    if state == RETURNED and unit_was_cancelled(order, unit):
        return unit.platform_status
    return unit.refund_status


def page_tracking_line(unit: OrderUnit) -> str | None:
    """The newest step of the order page that had happened when it was read,
    as this file writes it ("27-10-2026 Shipped",
    app.core.unit_facts.last_tracking_update); None while the page dates no
    step."""
    from app.core import unit_facts  # local: app.core imports this module's neighbours

    tracked = unit_facts.last_tracking_update(unit)
    return f"{fmt_date(tracked[0])} {tracked[1]}" if tracked else None


def last_tracking_cell(unit: OrderUnit) -> str:
    """The spec's "Last Tracking Update": the order page's newest dated step
    (:func:`page_tracking_line`), else what an order report wrote in its
    column (``OrderUnit.last_tracking_text``), else blank.

    Flipkart's dated page first: a report's line stands only while no
    read-back has dated a step."""
    return page_tracking_line(unit) or unit.last_tracking_text or ""


#: Every ``Order.payment_mode`` that says how the order is paid: a run's card
#: or Cash on Delivery, or an order report's "prepaid" — not Cash on Delivery,
#: by a means it did not name. "unknown" says nothing.
PAYMENT_KNOWN = ("cod", "card", "prepaid")


def cash_on_delivery_cell(order: Order, unit: OrderUnit) -> str:
    """The spec's "Cash on Delivery": "Yes" or "No" from the order's payment
    mode, and blank, not "No", when the mode is not known (an order handed in
    by id, or a report that left the column empty).

    A report's "No" ("prepaid") yields to the unit's own page when that says
    "Cash On Delivery" (app.core.order_status.paid_on_delivery), as the Order
    Details API and the row dialog read it: the read-back wins wherever a
    report meets it."""
    mode = order.payment_mode
    if mode == "prepaid" and paid_on_delivery(unit.payment_method):
        return "Yes"
    return yes_no(mode == "cod") if mode in PAYMENT_KNOWN else ""


def export_filename(now: datetime | None = None, *, sheet: str | None = None) -> str:
    stamp = (now or datetime.now(IST)).astimezone(IST)
    prefix = "order_units" if sheet is None else sheet
    return f"{prefix}_{stamp:%Y-%m-%d_%H%M}.csv"


def row_for(
    unit: OrderUnit,
    order: Order,
    email: str | None,
    *,
    invoice_on_file: bool = False,
    phone: str | None = None,
    cancel_requested: bool = False,
    order_reading=None,
    product=None,
    units_in_order: int | None = None,
    gst_number: str | None = None,
) -> list[str]:
    """One line per unit, in :data:`HEADERS` order: the unit's own status and
    ids, the order's everything else, and a blank wherever the ledger does not
    hold the spec's column yet (PO number, RTGS, EWB number).

    ``product`` is what the order's product invoices say
    (app.core.unit_facts.ProductFacts): the IMEI and the billing name come
    from it, the IMEI matched to the unit by app.core.unit_facts.unit_imei's
    rule. The business name and GSTIN are one business's pair
    (:func:`business_gst`), ``gst_number`` being the order's GSTIN as
    :func:`gst_numbers` gives it."""
    from app.core import unit_facts  # local: app.core imports this module's neighbours
    state = unit.delivery_state or classify(unit.platform_status)
    # Whether the unit was cancelled, which its latest wording alone does not
    # say: a refunded card cancellation reads "Refund Completed" (returned).
    cancelled = unit_was_cancelled(order, unit)
    qty = order.qty if order.qty and order.qty > 0 else 1
    estimated, actual = delivery_dates(unit)
    imei, _scope = unit_facts.unit_imei(product, unit.position, units_in_order or qty)
    business_name, business_gstin = business_gst(order, unit, gst_number)
    spec = [
        "flipkart",
        # The order date is when the platform took the order. An attempt that
        # never became one has none — blank here, a dash on the page, and
        # outside any Order-date range — rather than quietly dated by when it
        # was recorded (that is the Recorded date field, for those who want it).
        fmt_date(order.placed_at),
        fmt_time(order.placed_at),
        email or "",
        unit.platform_status or "",
        order.model or "",
        text_guard(order.order_id),
        text_guard(unit.unit_id),
        text_guard(order.ref_code),
        unit.source or "",
        text_guard(order.po_number),
        money(unit_amount(order, units_in_order)),
        "" if order.qty is None else str(order.qty),
        money(order.amount),
        estimated,
        actual,
        text_guard(unit.tracking_id),
        # The newest step of the order page that had happened when it was
        # read, else an order report's own line.
        last_tracking_cell(unit),
        # The courier, as an order report names it (no read-back reads one).
        unit.shipment_partner or "",
        business_name,
        business_gstin,
        # The name the product invoice was billed to.
        getattr(product, "billing_name", None) or "",
        # The phone the order was placed with (the account's billing phone,
        # else its own, else the address's), kept as text: a 10-digit phone
        # is not a number. Blank for rows a read-back or an id sheet created.
        identifier_cell(order.billing_phone),
        order.pin or "",
        # The card the order was paid with, in full: the owner's decision of
        # 25 Oct 2026 (FR-004). Blank for Cash on Delivery and for rows this
        # tool did not place.
        identifier_cell(order.card_number),
        text_guard(order.rtgs_source),
        text_guard(order.rtgs_beneficiary),
        cash_on_delivery_cell(order, unit),
        text_guard(order.ewb_number),
        yes_no(invoice_on_file),
        # Flipkart's words for a cancellation's refund, else an order report's;
        # the amount and the method only a report gives (spec FR-022).
        text_guard(refund_status(order, unit)),
        money(unit.refund_amount),
        unit.refund_method or "",
        yes_no(cancelled),
        cancelled_by_user_cell(unit, cancelled, requested=cancel_requested, reading=order_reading),
        # The order's own wording, as the read-back mirrors it from the unit
        # that stands for the order (app.core.order_units.representative_unit):
        # beside each unit's own Status, how the order as a whole reads.
        order.platform_status or "",
        unit.seller or "",
        # From the product invoices, kept as text (15 digits).
        identifier_cell(imei),
        # "Yes" once a person marked the goods in hand (POST /orders/received);
        # blank until then, since no one has said either way.
        "Yes" if unit.received_at is not None else "",
    ]
    portal = [
        # The ledger page's Outcome: who cancelled an order Flipkart took and
        # then cancelled, never the "failed" of a run that sent nothing.
        outcome_value(order, unit, requested=cancel_requested, reading=order_reading),
        order.color or "",
        str(unit.position),
        text_guard(unit.item_id),
        money(order.target),
        order.payment_mode or "",
        state,
        identifier_cell(unit.delivery_otp),
        phone_last_6(phone),
        order.brand or "",
        order.category or "",
        "" if order.job_id is None else str(order.job_id),
        fmt_datetime(unit.synced_at),
        # When this tool first wrote the row — the "Recorded" date the filter
        # offers, so a range on it can be checked against the file.
        fmt_datetime(order.created_at),
        order.product_link or "",
        # Flipkart's own sentence, free text: guarded so it can never be read
        # as a spreadsheet formula.
        text_guard(unit.cancel_reason),
        text_guard(unit.payment_method),
        money(unit.amount_paid),
        _days_late_cell(unit),
    ]
    return spec + portal


def _days_late_cell(unit: OrderUnit) -> str:
    """Days the delivery came after the first promise ("0" on time), by the
    ledger page's own rule (app.core.order_status.unit_days_late): blank until
    the unit has been delivered with both dates known, and for one never
    delivered, such as a cancelled one; kept for one delivered and since sent
    back."""
    late = unit_days_late(unit)
    return "" if late is None else str(late)


# --- the owner's two sheets (25 Oct 2026) -----------------------------------


def out_for_delivery_row(row: ExportRow) -> list[str]:
    """The owner's "out for delivery" sheet: what the person at the door needs."""
    unit, order = row.unit, row.order
    return [
        fmt_short_date(order.placed_at),
        status_word(unit, order, requested=row.cancel_requested, reading=row.order_reading),
        order.model or "",
        text_guard(order.order_id),
        identifier_cell(unit.delivery_otp),
        text_guard(unit.tracking_id),
        phone_last_6(row.phone),
    ]


def order_data_row(row: ExportRow) -> list[str]:
    """The owner's "order data" sheet: every order after placing, with the
    delivery promise, the OTP and the tracking id as they fill in."""
    unit, order = row.unit, row.order
    state = unit.delivery_state or classify(unit.platform_status)
    # The promise while the unit is on its way; once it is delivered or
    # settled otherwise there is nothing left to expect.
    promise = "" if state in SETTLED else fmt_short_date(unit.promised_date or unit.delivery_date)
    return [
        PLATFORM_WORD,
        fmt_short_date(order.placed_at),
        status_word(unit, order, requested=row.cancel_requested, reading=row.order_reading),
        order.model or "",
        text_guard(order.order_id),
        promise,
        identifier_cell(unit.delivery_otp),
        text_guard(unit.tracking_id),
        phone_last_6(row.phone),
    ]


@dataclass(frozen=True)
class Sheet:
    """A CSV layout: its file-name prefix, its header row, and its line."""

    name: str
    headers: list[str]
    line: Callable[[ExportRow], list[str]]


def full_ledger_row(row: ExportRow) -> list[str]:
    return row_for(
        row.unit,
        row.order,
        row.email,
        invoice_on_file=row.invoice_on_file,
        phone=row.phone,
        cancel_requested=row.cancel_requested,
        order_reading=row.order_reading,
        product=row.product,
        units_in_order=row.units_in_order,
        gst_number=row.gst_number,
    )


FULL_LEDGER = Sheet("order_units", HEADERS, full_ledger_row)
SHEETS: dict[str, Sheet] = {
    "out_for_delivery": Sheet(
        "out_for_delivery",
        ["Order Date", "Status", "Product", "Order ID", "Delivery OTP", "Tracking ID", "Last 6 Digits Of Phone"],
        out_for_delivery_row,
    ),
    "order_data": Sheet(
        "order_data",
        [
            "Platform", "Order Date", "Status", "Product", "Order ID",
            "Estimated Delivery Date", "Delivery OTP", "Tracking ID", "Phone Last 6 Numbers",
        ],
        order_data_row,
    ),
}


def sheet_named(name: str | None) -> Sheet:
    """The layout a name picks; the full ledger when there is none.
    Raises ``KeyError`` for a name that is not a sheet."""
    return FULL_LEDGER if not name else SHEETS[name]


#: How much text :func:`stream` gathers before handing it on. Each piece of a
#: streamed answer is one hop between the server's threads; a piece per line
#: made a year's ledger (40,000 units) forty thousand hops and most of its
#: 13 s (27 Oct 2026). The invoice-data CSV does the same.
FLUSH_CHARS = 64 * 1024


def stream(rows: Iterable[ExportRow], *, sheet: Sheet = FULL_LEDGER) -> Iterator[str]:
    """Yield the CSV text, header first, then the units' lines in pieces of
    about :data:`FLUSH_CHARS`.

    ``rows`` are the ledger's export rows, read from the database as the text
    is written out — the response starts before the last row is fetched, and
    no number of rows has to fit in memory at once. The BOM up front is what
    makes Excel read UTF-8.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(sheet.headers)
    yield "﻿" + buffer.getvalue()
    buffer.seek(0)
    buffer.truncate()
    for row in rows:
        writer.writerow(safe_row(sheet.line(row)))
        if buffer.tell() >= FLUSH_CHARS:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    if buffer.tell():
        yield buffer.getvalue()
