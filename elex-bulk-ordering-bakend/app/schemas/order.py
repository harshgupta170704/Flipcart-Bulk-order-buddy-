"""Pydantic schemas for the orders API.

``status`` can be ``failed_over_target`` — the price-cap outcome from
PLAN.md §6a. Such a row is a *recorded* order that was deliberately **not**
placed, with the amount we saw, so the UI can show "failed — over target"
next to the target it breached.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.order_status import UNKNOWN, classify, unit_days_late
from app.db.models import Invoice, Order, OrderStatus, OrderUnit

if TYPE_CHECKING:  # pragma: no cover — typing only
    from app.core.cancellations import CancelReading
    from app.core.unit_facts import ProductFacts

#: The width of ``Order.order_id``: a longer id is a clean 422 here rather than
#: a database error on a backend that enforces length (audit F-40).
MAX_ORDER_ID_LENGTH = 64


class OrderSettleIn(BaseModel):
    """How an operator settles an *unconfirmed* order after checking Flipkart.

    ``placed`` needs the order id they found there; ``failed`` means the order
    is not on the account, so its units go back.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["placed", "failed"]
    order_id: str | None = Field(default=None, pattern=r"^OD\d{10,}$", max_length=MAX_ORDER_ID_LENGTH)


class InvoiceOut(BaseModel):
    """One invoice on file for an order, as the row dialog lists it: what the
    reader took off the PDF (type, number, date, seller, GSTINs, total, the
    lines with their tax split and IMEI) and where the PDF itself is served."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    invoice_type: str | None = None
    invoice_number: str | None = None
    invoice_date: datetime | None = None
    order_date: datetime | None = None
    seller_name: str | None = None
    seller_gstin: str | None = None
    buyer_name: str | None = None
    buyer_gstin: str | None = None
    grand_total: float | None = None
    original_invoice: str | None = None
    reason: str | None = None
    lines: list[dict] = Field(default_factory=list)
    #: sync (fetched during a read-back) or upload.
    source: str
    fetched_at: datetime | None = None
    size: int
    #: Why the reader could not read the PDF, when it could not.
    parse_error: str | None = None
    #: The PDF: ``GET /orders/{order_row_id}/invoices/{id}.pdf``.
    download_path: str = ""

    @classmethod
    def from_row(cls, row: "Invoice", order_row_id: int) -> "InvoiceOut":
        out = cls.model_validate(row)
        out.lines = list(row.lines or [])
        out.download_path = f"/orders/{order_row_id}/invoices/{row.id}.pdf"
        return out


class OrderOut(BaseModel):
    """One row of the ledger: a unit, with its order's fields alongside.

    ``id`` is the unit row's; ``order_row_id`` is the order's, which is what
    settling and any other order-level action addresses.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    order_row_id: int | None = None
    #: 1-based place of this unit in its order, and how many the order has.
    unit_position: int = 1
    units_in_order: int = 1
    #: Who created the unit row: run / sync / upload.
    source: str = "run"
    #: The platform the unit was bought on (``OrderUnit.platform``): "flipkart"
    #: for every unit for now (the owner's decision of 24 Oct 2026).
    platform: str = "flipkart"
    account_id: int
    account_email: str | None = None
    #: The run's Ref Code, and the accounting references a person filled in.
    ref_code: str | None = None
    po_number: str | None = None
    rtgs_source: str | None = None
    rtgs_beneficiary: str | None = None
    ewb_number: str | None = None
    product_link: str | None = None
    qty: int | None = None
    model: str | None = None
    color: str | None = None
    pin: str | None = None
    amount: float | None = None
    target: float | None
    #: "card" or "cod" as a run chose; "unknown" for an order no run placed;
    #: "prepaid" when an order report said it was not Cash on Delivery without
    #: saying how it was paid — never a card.
    payment_mode: str = None
    #: The card the order was paid with, in full (the owner's decision of
    #: 25 Oct 2026); null for Cash on Delivery and for rows read back or uploaded.
    card_number: str | None = None
    order_id: str | None = None
    gst_name: str | None = None
    #: The GSTIN the run billed the order under (``Order.gstin``), the spec's
    #: "Business GST No" beside the business name: one name can hold a
    #: registration per state. Null when the run chose no GST, and for rows
    #: read back or uploaded.
    gstin: str | None = None
    #: The phone the order was placed with (the account's billing phone, else
    #: its own, else the address's); null for rows read back or uploaded.
    billing_phone: str | None = None
    status: OrderStatus
    processing_stage: str | None = None
    error_code: str | None = None
    placed_at: datetime | None = None
    #: When this tool first wrote the row — the "Recorded" date the filter offers.
    created_at: datetime | None = None
    brand: str | None = None
    category: str | None = None
    job_id: int | None = None
    #: What the run said about this account's attempt, without the verdict it
    #: opens with ("Cash on Delivery is not available for this product —
    #: nothing was charged"); null for an order no run recorded. Filled by the
    #: list endpoint for the rows that did not end as a placed order
    #: (app.core.run_notes), so "Failed" comes with its reason.
    run_message: str | None = None

    # --- read back from the platform by a sync ------------------------------
    # These were missing from the schema until 2026-10-24, so the UI's "sync
    # columns" could never show a value and its "read back" note was never true.
    item_id: str | None = None
    unit_id: str | None = None
    seller: str | None = None
    tracking_id: str | None = None
    platform_status: str | None = None
    #: The portal's one reading of ``platform_status`` (app.core.order_status):
    #: delivered / cancelled / returned / failed / in_transit / unknown. Computed
    #: here so the table, the dashboard and the alerts cannot disagree.
    delivery_state: str = UNKNOWN
    delivery_date: datetime | None = None
    #: The day Flipkart promised (the latest promise read while on its way,
    #: kept once delivered) and the day it says the unit was delivered —
    #: ``delivery_date`` runs the two together (26 Oct 2026).
    promised_date: datetime | None = None
    delivered_date: datetime | None = None
    #: The first promise read, which a slipping delivery's later promises do
    #: not replace; null on a row read before it was kept (then
    #: ``promised_date`` is the earliest promise known).
    first_promised_date: datetime | None = None
    #: Days the delivery came after the first promise: 0 on time, null while
    #: either date is unknown (app.core.order_status.unit_days_late, which the
    #: CSV's Days Late reads too).
    days_late: int | None = None
    #: How the unit is paid, and the order page's "Total amount" for it —
    #: paid, or to be paid on delivery, as the page's price details say
    #: ("Cash On Delivery", 235.0); null until a read-back sees them. The
    #: name is the column's; whether the money was paid depends on the method
    #: and the delivery state.
    payment_method: str | None = None
    amount_paid: float | None = None
    delivery_otp: str | None = None
    invoice_url: str | None = None
    #: The order page's address block as the read-back saw it, and its pincode
    #: (spec FR-044/045: what the address alerts compare with the Address Book).
    delivery_address: str | None = None
    delivery_pincode: str | None = None
    #: When a read-back last saw that address replaced by a different one
    #: (``OrderUnit.address_changed_at``, what raises the address-change alert,
    #: spec FR-045); null until that happens.
    address_changed_at: datetime | None = None
    #: The GST the order page shows the order billed under (GSTIN and business
    #: name); compared with ``gstin`` by the GST-mismatch alert.
    billed_gstin: str | None = None
    billed_business_name: str | None = None
    #: What the order's product invoices say of the unit (app.core.unit_facts):
    #: its IMEI and how it was told — "unit" (the order's only unit),
    #: "position" (one of as many IMEIs as units, taken by position, since the
    #: invoice does not name the unit) or "order" (every IMEI of the order,
    #: when which is whose cannot be told) — and the name the order was billed
    #: to. Null until a product invoice is on file.
    imei: str | None = None
    imei_scope: str | None = None
    billing_name: str | None = None
    #: The newest step of the order page that had happened when it was read:
    #: its date and its wording (the CSV's "Last Tracking Update").
    last_tracking_update: datetime | None = None
    last_tracking_step: str | None = None
    #: An order report's own last tracking update, as written ("25-10-2026
    #: Cancelled"); the CSV writes it while the page's timeline dates no step.
    last_tracking_text: str | None = None
    #: The courier, as an order report names it (the CSV's "Shipment Partner").
    shipment_partner: str | None = None
    #: The refund, as the CSV writes it (spec FR-022): its status — Flipkart's
    #: own words for a cancelled unit's refund ("Refund Completed"), else an
    #: order report's (app.io.orders_csv.refund_status) — and the amount and
    #: the method an order report gave; null when nothing said.
    refund_status: str | None = None
    refund_amount: float | None = None
    refund_method: str | None = None
    #: The order's amount shared over its units (the CSV's "Unit Amount"):
    #: over its quantity, or the units it holds when they are more
    #: (app.io.orders_csv.unit_amount).
    unit_amount: float | None = None
    #: When a person marked the unit's goods as in hand, and who; null until
    #: someone does (the CSV's "Received").
    received_at: datetime | None = None
    received_by: str | None = None
    #: Why the platform cancelled this unit, in its own words, as the order
    #: page printed it ("Sorry, your order was cancelled as per seller policy
    #: restrictions applicable to this brand."); null until a read-back saw one.
    cancel_reason: str | None = None
    #: Who cancelled the order, when it is one Flipkart took and then cancelled
    #: (failed, with Flipkart's order id): "user" when this unit's wording or a
    #: cancel request says the account asked for it, "flipkart" otherwise. A
    #: unit that says nothing — one Flipkart never listed — takes its order's
    #: verdict, as the dashboard and the alert give it. Null for every other
    #: order — a "failed" with no order id is the run's own failure, before
    #: anything reached Flipkart (app.core.cancellations).
    cancelled_by: Literal["flipkart", "user"] | None = None
    #: A PDF is in the invoice store for this order (GET /orders/{order_row_id}/invoice.pdf).
    #: The order page's timeline as last read back: steps of {"status", "date"}
    #: in page order, the date null for a step still to come.
    timeline: list[dict] | None = None
    invoice_on_file: bool = False
    #: How many invoices the order has on file (product, fees, transport…);
    #: the row dialog lists them (``GET /orders/{id}/invoices``).
    invoices_on_file: int = 0
    synced_at: datetime | None = None
    #: Kinds of open cancel / reschedule request on this unit (spec FR-020):
    #: ["cancel"], ["reschedule"], both, or empty. Filled by the list endpoint
    #: from one query over the page's units, so the table can show a tag.
    open_request_kinds: list[str] = Field(default_factory=list)

    @field_validator("delivery_state", mode="before")
    @classmethod
    def _unknown_when_not_derived(cls, value):
        # A row older than the column holds NULL until the startup backfill
        # runs; to a reader that is simply "unknown".
        return UNKNOWN if value is None else value

    @classmethod
    def from_unit(
        cls,
        unit: "OrderUnit",
        order: Order,
        account_email: str | None = None,
        *,
        units_in_order: int | None = None,
        cancel_requested: bool = False,
        order_reading: CancelReading | None = None,
        product: "ProductFacts | None" = None,
    ) -> "OrderOut":
        """Build one ledger row from a unit and its order.

        The email, the unit count and whether the tool holds a cancel request
        for the unit (open or done) are passed in rather than read through the
        relationships, so the list endpoint can select them in one query for
        the page instead of emitting a lazy load per row. So is
        ``order_reading``, the order's :class:`app.core.cancellations.CancelReading`,
        which a unit that says nothing of who cancelled it reads its
        ``cancelled_by`` from; the list endpoint reads it only for such units.
        And ``product``, what the order's product invoices say
        (app.core.unit_facts), read for the whole page in one query.
        """
        out = cls.model_validate(order)
        out.id = unit.id
        out.order_row_id = order.id
        out.account_email = account_email
        out.unit_position = unit.position
        out.units_in_order = (
            units_in_order if units_in_order is not None else max(1, len(order.units))
        )
        out.source = unit.source
        out.platform = unit.platform
        out.item_id = unit.item_id
        out.unit_id = unit.unit_id
        out.seller = unit.seller
        out.tracking_id = unit.tracking_id
        out.platform_status = unit.platform_status
        out.delivery_state = unit.delivery_state or classify(unit.platform_status)
        out.delivery_date = unit.delivery_date
        out.promised_date = unit.promised_date
        out.delivered_date = unit.delivered_date
        out.first_promised_date = unit.first_promised_date
        out.days_late = unit_days_late(unit)
        out.payment_method = unit.payment_method
        out.amount_paid = unit.amount_paid
        out.delivery_otp = unit.delivery_otp
        out.invoice_url = unit.invoice_url
        out.delivery_address = unit.delivery_address
        out.delivery_pincode = unit.delivery_pincode
        out.address_changed_at = unit.address_changed_at
        out.billed_gstin = unit.billed_gstin
        out.billed_business_name = unit.billed_business_name
        out.cancel_reason = unit.cancel_reason
        from app.core.cancellations import cancelled_by  # local: it imports the models
        from app.core.order_units import timeline_steps  # local: order_units imports the models

        out.cancelled_by = cancelled_by(order, unit, requested=cancel_requested, reading=order_reading)
        out.timeline = timeline_steps(unit.timeline)
        out.synced_at = unit.synced_at
        from app.core import unit_facts  # local: it imports the models

        out.imei, out.imei_scope = unit_facts.unit_imei(product, unit.position, out.units_in_order)
        out.billing_name = product.billing_name if product is not None else None
        tracked = unit_facts.last_tracking_update(unit)
        out.last_tracking_update, out.last_tracking_step = tracked if tracked else (None, None)
        out.last_tracking_text = unit.last_tracking_text
        out.shipment_partner = unit.shipment_partner
        from app.io.orders_csv import refund_status, unit_amount  # local: as the CSV shares them, one rule for both

        out.refund_status = refund_status(order, unit)
        out.refund_amount = unit.refund_amount
        out.refund_method = unit.refund_method
        # Over the quantity, or the units the order holds when they are more:
        # the shares never add up to more than the order.
        out.unit_amount = unit_amount(order, out.units_in_order)
        out.received_at = unit.received_at
        out.received_by = unit.received_by
        return out

    @classmethod
    def from_order(cls, order: Order, account_email: str | None = None) -> "OrderOut":
        """The order's first unit as a row — for callers that hold one order."""
        from sqlalchemy.orm import object_session

        from app.core.cancellations import units_with_cancel_request
        from app.core.order_units import ensure_units, primary_unit

        unit = primary_unit(order) or ensure_units(order)[0]
        session = object_session(order)
        requested = (
            unit.id is not None and session is not None and unit.id in units_with_cancel_request(session, [unit.id])
        )
        return cls.from_unit(unit, order, account_email, cancel_requested=requested)


class StatusCount(BaseModel):
    value: str
    count: int


class PlatformStatusCount(StatusCount):
    #: The portal's reading of this wording — what the grouped picker groups by.
    state: str


class OrderFilterOptionsOut(BaseModel):
    """The values present in the ledger, for building its filters."""

    statuses: list[StatusCount] = Field(default_factory=list)
    platform_statuses: list[PlatformStatusCount] = Field(default_factory=list)
    delivery_states: list[StatusCount] = Field(default_factory=list)
    processing_stages: list[StatusCount] = Field(default_factory=list)
    error_codes: list[StatusCount] = Field(default_factory=list)
    total: int = 0


class OrderViewIn(BaseModel):
    """A saved view of the ledger: which columns show, and the filters applied."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(default_factory=list, max_length=64)
    filters: dict = Field(default_factory=dict)


class OrderViewOut(OrderViewIn):
    saved_at: datetime | None = None


class OrderReceivedIn(BaseModel):
    """``POST /orders/received``: mark these ledger rows' goods as in hand, or
    take the mark back (spec FR-001, FR-017's "Received")."""

    model_config = ConfigDict(extra="forbid")

    #: The ledger's own row ids (``OrderOut.id``), as the page ticks them.
    unit_ids: list[Annotated[int, Field(ge=1, le=2**31 - 1)]] = Field(min_length=1, max_length=500)
    received: bool


class OrderReceivedOut(BaseModel):
    """How many units the mark changed, and the ids no unit has."""

    changed: int
    missing: list[int] = Field(default_factory=list)


class OrderIdsRejectedOut(BaseModel):
    row: int
    reason: str


class OrderIdsImportOut(BaseModel):
    """What an uploaded sheet of order ids did, and what to read back next."""

    created: int
    existing: int
    rejected: list[OrderIdsRejectedOut] = Field(default_factory=list)
    #: The accounts and ids a read-back should cover now.
    account_ids: list[int] = Field(default_factory=list)
    order_ids: list[str] = Field(default_factory=list)


class OrderReportImportOut(BaseModel):
    """What an uploaded order report did (``POST /orders/import-report``):
    units added and updated, rows that changed nothing, every refused row with
    its reason, and the columns the file carried that the ledger keeps nowhere
    yet."""

    created: int
    updated: int
    unchanged: int
    #: Rows that said otherwise than Flipkart's own page for a unit the
    #: read-back has seen: Flipkart's reading was kept for those values.
    kept_platform: int = 0
    rejected: list[OrderIdsRejectedOut] = Field(default_factory=list)
    not_kept: list[str] = Field(default_factory=list)


class OrderPage(BaseModel):
    """One page of orders plus the *full* match count (the "Showing N of M" line)."""

    items: list[OrderOut] = Field(default_factory=list)
    total: int
    page: int
    size: int
    pages: int
