"""Order details, in the shape the owner's own specification gives
(``docs/specs/order-unit-ledger/inputs/Flipkart Order Details Get API.pdf``).

Two questions, one path, as the document shows them:

* ``POST /orders/orderDetails`` with ``emailid`` and a date range — the
  account's orders in that window, paged by ``offset``/``limit``;
* the same path with ``orderId`` — everything the ledger holds about that one
  order: its units, payments, shipments and invoice.

Both are thin readings of the ledger: the data behind them is what the order
runs recorded and what the read-back later saw on Flipkart. Nothing is
fetched live to answer a call. Every key the document shows is present, with
``null`` where the ledger holds nothing (a client written to the document reads
``data.totals.subtotal`` and gets null, not an error); fields the ledger knows
beyond the document are added, never substituted.

Behind the portal's own login like every other route: this is a feature of
this tool, not a bridge to another system.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession, selectinload

from app.api.orders import MAX_ID, _bound
from app.core import cancellations, invoices
from app.core.order_status import classify, never_placed, paid_on_delivery, refund_completed
from app.core.order_units import representative_unit
from app.core.security import require_user
from app.db.base import get_db
from app.db.models import Account, Order, OrderUnit
from app.io import orders_csv

router = APIRouter(
    prefix="/orders",
    tags=["order-details"],
    dependencies=[Depends(require_user)],
)

MAX_LIMIT = 500

#: The document's status vocabulary, from the portal's reading of a unit's
#: wording — except that a unit that was cancelled is CANCELLED whatever its
#: latest wording says, as the ledger page and the CSV read it (see _status).
#: "deliveryState" and "platformStatus" keep the reading and the wording
#: themselves, as the CSV keeps its Delivery State.
STATUS_CODE = {
    "delivered": "DELIVERED",
    "cancelled": "CANCELLED",
    "returned": "RETURNED",
    "failed": "FAILED",
    "in_transit": "SHIPPED",
    "unknown": "PENDING",
}


class OrderDetailsRequest(BaseModel):
    """The request as the document gives it. ``limit`` arrives as a string
    there ("20"); both forms are accepted."""

    model_config = ConfigDict(extra="ignore")

    emailid: str | None = None
    orderId: str | None = None
    offset: int = Field(default=0, ge=0, le=MAX_ID)
    limit: int = 20
    fromDate: str | None = None
    toDate: str | None = None

    @field_validator("limit", mode="before")
    @classmethod
    def _limit_as_int(cls, value):
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be a whole number") from exc
        if not 1 <= number <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        return number


def _iso(value: datetime | None) -> str | None:
    """The stored (naive UTC) clock as an ISO string with its zone, as the
    document shows ("2025-04-17T09:12:34Z")."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(value: datetime | None) -> str | None:
    return None if value is None else value.strftime("%Y-%m-%d")


def _delivery_dates(unit: OrderUnit) -> tuple[str | None, str | None]:
    """``(estimatedDelivery, deliveredOn)`` by the ledger's one rule for the
    two days (:func:`app.io.orders_csv.delivery_days`): while the unit is on
    its way, the day it is now promised for and no delivery; once delivered —
    and after, when it is sent back — the day it was first promised for beside
    the day it came, so a late delivery shows as late here as in the CSV and
    on the ledger page; neither for a unit cancelled, failed or returned
    without a delivery day, whose date is the date of that event. The rule is
    the CSV's; only the format is this document's."""
    estimated, actual = orders_csv.delivery_days(unit)
    return _day(estimated), _day(actual)


def _state(unit: OrderUnit | None) -> str:
    if unit is None:
        return "unknown"
    return unit.delivery_state or classify(unit.platform_status)


def _status(order: Order, unit: OrderUnit | None) -> str:
    """The document's status of one unit, or of the order through the unit
    that stands for it (:func:`app.core.order_units.representative_unit`).

    CANCELLED for a unit that was cancelled, whatever its latest wording says
    (:func:`app.core.cancellations.unit_was_cancelled`): a refunded card
    cancellation reads "Refund Completed", and a unit Flipkart never listed
    went with its cancelled order — the ledger page and the CSV call both
    cancelled, as the change feed does the refund. An order with no unit is
    CANCELLED when Flipkart took it and then cancelled it, PENDING otherwise.
    """
    if unit is None:
        return "CANCELLED" if cancellations.placed_then_cancelled(order) else "PENDING"
    if cancellations.unit_was_cancelled(order, unit):
        return "CANCELLED"
    return STATUS_CODE.get(_state(unit), "PENDING")


def _cancelled_by(order: Order, reading: cancellations.CancelReading | None) -> str | None:
    """Who cancelled an order Flipkart took and then cancelled — "user" or
    "flipkart", the order's verdict as the dashboard and the alerts give it
    (``reading``, from :func:`app.core.cancellations.cancel_readings`) — and
    None for every other order."""
    if not cancellations.placed_then_cancelled(order):
        return None
    return cancellations.BY_USER if reading is not None and reading.user_cancelled else cancellations.BY_PLATFORM


def _outcome(order: Order, reading: cancellations.CancelReading | None) -> str:
    """The ledger page's Outcome for the order: who cancelled one Flipkart took
    and then cancelled (cancelled_by_flipkart / cancelled_by_user, the values
    the CSV and the workbook write) — never "failed", which means a run that
    sent nothing; the stored status otherwise ("placed", "unconfirmed"…)."""
    who = _cancelled_by(order, reading)
    if who is not None:
        return cancellations.CANCELLED_OUTCOME[who]
    return getattr(order.status, "value", str(order.status))


def _cancel_readings(db: DbSession, orders: list[Order]) -> dict[int, cancellations.CancelReading]:
    """The cancellation verdict of each of these orders that Flipkart took and
    then cancelled, keyed by order. Read for those alone, so a page with none
    costs no query more; one with some costs a query per chunk of them (and of
    their units), never one per order."""
    pks = [order.id for order in orders if cancellations.placed_then_cancelled(order)]
    return cancellations.cancel_readings(db, pks) if pks else {}


def _payment(order: Order) -> tuple[str, str]:
    """``(method, status)`` from what the ledger knows: the mode the run used
    and whether the order stands. An order handed in by id has no known mode,
    and the ledger must not assert a charge it never saw.

    A card order Flipkart took and then cancelled
    (:func:`app.core.cancellations.placed_then_cancelled`) was charged when it
    was placed, and the money is owed back: REFUND_DUE until every unit of it
    that was cancelled reads a completed refund in Flipkart's words
    (:func:`app.core.order_status.refund_completed`), REFUNDED then — as the
    order dialog's payment line says "refund due" and then "refunded". Only
    the units that carry wording are asked: a unit Flipkart never listed (a
    placeholder of a quantity it capped) went with its cancelled order, but
    has no page of its own that could ever read "Refund Completed", and held
    a refunded order at REFUND_DUE for ever (review of 27 Oct 2026). With no
    such unit, nothing says the money is back: REFUND_DUE. Such an order paid
    by Cash on Delivery collected nothing (NOT_CHARGED), and one with no known
    mode stays UNKNOWN. Otherwise NOT_CHARGED is left to an order Flipkart
    never took.

    Cash on Delivery is read off the order's own page first, as the dialog
    reads it (:func:`app.core.order_status.paid_on_delivery`): a row the
    read-back imported before 27 Oct 2026 kept the column default "card"
    whatever it was paid with, and must not be reported as money owed back.

    An order an order report says was not Cash on Delivery ("prepaid") was
    paid before it came, by a means the report does not name: its method is
    UNKNOWN, never CARD, and its money reads as a card's does — CAPTURED
    while it stands, REFUND_DUE or REFUNDED once cancelled. Before 27 Oct 2026
    the report's "No" was dropped, and such an order read UNKNOWN throughout.
    """
    mode = (order.payment_mode or "").lower()
    method = {"cod": "COD", "card": "CARD"}.get(mode, "UNKNOWN")
    # Paid before it came: by a run's own card, or as a report says.
    prepaid = mode in ("card", "prepaid")
    if any(paid_on_delivery(unit.payment_method) for unit in order.units):
        method, prepaid = "COD", False
    outcome = getattr(order.status, "value", str(order.status))
    if outcome == "unconfirmed":
        return method, "UNCONFIRMED"
    if cancellations.placed_then_cancelled(order):
        if method == "COD":
            return method, "NOT_CHARGED"
        if _never_placed(order):
            # "Order Not Placed" / "Payment Failed": the payment never went
            # through, so there is no charge to hand back (11 Nov 2026 — such
            # an order read REFUND_DUE for ever, a refund nobody owed).
            return method, "NOT_CHARGED"
        if prepaid:
            # A unit that says nothing at all is a placeholder Flipkart never
            # listed; one it read back, or that carries a reason or a request,
            # is a real unit whose money must read refunded (checker of 27 Oct).
            cancelled = [
                unit
                for unit in order.units
                if (unit.synced_at is not None or not cancellations.says_nothing(unit))
                and cancellations.unit_was_cancelled(order, unit)
            ]
            refunded = bool(cancelled) and all(refund_completed(unit.platform_status) for unit in cancelled)
            return method, "REFUNDED" if refunded else "REFUND_DUE"
        return method, "UNKNOWN"
    if outcome != "placed":
        return method, "NOT_CHARGED"
    if method == "COD":
        return method, "COD_ON_DELIVERY"
    if prepaid:
        return method, "CAPTURED"
    return method, "UNKNOWN"


def _never_placed(order: Order) -> bool:
    """Whether Flipkart's own wording says this order was never placed: every
    unit that carries wording says so (:func:`app.core.order_status.never_placed`),
    or — with no worded unit — the order row's own wording does."""
    worded = [unit.platform_status for unit in order.units if (unit.platform_status or "").strip()]
    if worded:
        return all(never_placed(status) for status in worded)
    return never_placed(order.platform_status)


def _primary(order: Order) -> OrderUnit | None:
    return min(order.units, key=lambda unit: unit.position) if order.units else None


def _invoice_fields(order: Order, path: str | None) -> dict:
    """The document's three invoice fields, plus where to download it from this API."""
    return {
        "downloadStatus": "DOWNLOADED" if path else "PENDING",
        "duplicateCount": 0,
        "invoiceFilePath": f"/invoices/{path}" if path else None,
        "invoiceDownloadUrl": f"/orders/{order.id}/invoice.pdf" if path else None,
    }


def _where_it_stands(order: Order, reading: cancellations.CancelReading | None) -> dict:
    """The order-level keys of both answers: the document's status, Flipkart's
    wording and the portal's reading of it, each from the unit that stands for
    the order (:func:`app.core.order_units.representative_unit`) — not the one
    at position 1, which may be a cancelled unit of an order that stands, or a
    delivered one beside a unit still on its way."""
    unit = representative_unit(order)
    return {
        "status": _status(order, unit),
        "platformStatus": unit.platform_status if unit else None,
        "deliveryState": _state(unit),
        # Beyond the document: the ledger page's Outcome — who cancelled an
        # order Flipkart took and then cancelled (cancelled_by_flipkart /
        # cancelled_by_user, as the CSV and the workbook write it), never the
        # "failed" of a run that sent nothing — and, added beside it, who
        # cancelled: "flipkart", "user", or null for an order not cancelled.
        "outcome": _outcome(order, reading),
        "cancelledBy": _cancelled_by(order, reading),
    }


def _summary_json(
    order: Order,
    email: str,
    gst_number: str | None,
    invoice_path: str | None = None,
    reading: cancellations.CancelReading | None = None,
) -> dict:
    """One list entry, field for field as the document's example, plus what
    the ledger knows beyond it. ``reading`` is the order's cancellation
    verdict (:func:`_cancel_readings`), for an order Flipkart cancelled."""
    method, status = _payment(order)
    return {
        "email": email,
        "orderId": order.order_id,
        **_invoice_fields(order, invoice_path),
        "productModel": order.model,
        "colour": order.color,
        "quantity": max(1, len(order.units)),
        "totalAmount": order.amount,
        "gstNumber": gst_number,
        "gstHolder": order.gst_name,
        "paymentStatus": status,
        "paymentMethod": method,
        "orderDate": _iso(order.placed_at),
        "recordedDate": _iso(order.created_at),
        "pincode": order.pin,
        **_where_it_stands(order, reading),
    }


def _detail_json(
    order: Order,
    email: str,
    gst_number: str | None,
    invoice_path: str | None = None,
    reading: cancellations.CancelReading | None = None,
) -> dict:
    primary = _primary(order)
    method, status = _payment(order)
    units = sorted(order.units, key=lambda unit: unit.position)
    dated = [(unit, *_delivery_dates(unit)) for unit in units]
    read_back = max((u.synced_at for u in units if u.synced_at), default=None)
    return {
        "orderId": order.order_id,
        "email": email,
        **_where_it_stands(order, reading),
        "channel": "ECOM",
        "platform": "flipkart",
        "createdAt": _iso(order.placed_at),
        "recordedAt": _iso(order.created_at),
        "updatedAt": _iso(read_back),
        "currency": "INR",
        "totals": {
            "subtotal": None,
            "tax": None,
            "discount": None,
            "shipping": None,
            "grandTotal": order.amount,
            "target": order.target,
        },
        "customer": {
            "name": None,
            "phone": None,
            "gstNumber": gst_number,
            "gstHolder": order.gst_name,
        },
        "addresses": {
            "shipping": {
                "name": None,
                "line1": None,
                "city": None,
                "state": None,
                "postalCode": order.pin,
                "country": "IN",
            },
            "billing": {"sameAsShipping": None},
        },
        "items": [
            {
                "itemId": unit.item_id,
                "unitId": unit.unit_id,
                "sku": None,
                "name": order.model,
                "colour": order.color,
                "quantity": 1,
                "unitPrice": None,
                "tax": None,
                "discount": None,
                "status": _status(order, unit),
                "platformStatus": unit.platform_status,
                "deliveryState": _state(unit),
                # The day it arrived, or the promise until then; nothing for a
                # unit that will not arrive, whose date is the cancellation's.
                "deliveryDate": delivered or estimated,
                "deliveryOtp": unit.delivery_otp,
                # Beyond the document: the address the platform's page shows.
                "deliveryAddress": unit.delivery_address,
                "deliveryPincode": unit.delivery_pincode,
                # Beyond the document too: the GST the page says the order was billed under.
                "billedGstin": unit.billed_gstin,
                "billedBusinessName": unit.billed_business_name,
            }
            for unit, estimated, delivered in dated
        ],
        "payments": [
            {
                "paymentId": None,
                "provider": None,
                "method": method,
                "amount": order.amount,
                "status": status,
                "paidAt": None,
            }
        ],
        "shipments": [
            {
                "shipmentId": None,
                "unitId": unit.unit_id,
                # The courier, as an order report named it (the CSV's
                # Shipment Partner); no read-back reads one yet.
                "carrier": unit.shipment_partner,
                "trackingNo": unit.tracking_id,
                "shippedAt": None,
                "estimatedDelivery": estimated,
                "deliveredOn": delivered,
            }
            for unit, estimated, delivered in dated
            if unit.tracking_id or unit.shipment_partner or estimated or delivered
        ],
        "invoice": {
            "downloadStatus": "DOWNLOADED" if invoice_path else "PENDING",
            "duplicateCount": 0,
            "filePath": f"/invoices/{invoice_path}" if invoice_path else None,
            "downloadUrl": f"/orders/{order.id}/invoice.pdf" if invoice_path else None,
            "platformUrl": primary.invoice_url if primary else None,
        },
        "pincode": order.pin,
        "metadata": {
            "source": (primary.source if primary else "run").upper(),
            "jobId": order.job_id,
            "productLink": order.product_link,
            "brand": order.brand,
            "category": order.category,
        },
    }


@router.post("/orderDetails")
def order_details(payload: OrderDetailsRequest, db: DbSession = Depends(get_db)) -> dict:
    """Orders for an email within a date range, or one order in full."""
    if payload.orderId:
        return _one(db, payload)
    return _many(db, payload)


def _one(db: DbSession, payload: OrderDetailsRequest) -> dict:
    order_id = payload.orderId.strip().upper()
    stmt = (
        select(Order, Account.email)
        .join(Account, Account.id == Order.account_id)
        .options(selectinload(Order.units))
        .where(Order.order_id == order_id)
    )
    if payload.emailid:
        stmt = stmt.where(func.lower(Account.email) == payload.emailid.strip().lower())
    # Should two rows ever carry one id, the dated one (a run's, or one read
    # back) is the real order; an undated upload under another account is not.
    row = db.execute(stmt.order_by(Order.placed_at.desc().nulls_last(), Order.id.asc()).limit(1)).first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no order {order_id} in the ledger")
    order, email = row
    # The order's GSTIN, else the GST book's one registration for its business
    # name: the CSV's rule for its Business GST No, so the two answer the same.
    gst = orders_csv.gst_numbers(db, [order])
    path = invoices.paths_on_file(db, [order.id]).get(order.id)
    reading = _cancel_readings(db, [order]).get(order.id)
    return {"success": True, "data": _detail_json(order, email, gst.get(order.id), path, reading)}


def _many(db: DbSession, payload: OrderDetailsRequest) -> dict:
    date_from = _bound(payload.fromDate, end=False)
    date_to = _bound(payload.toDate, end=True)
    # An order handed in by id or first seen on the platform has no order date
    # until the read-back supplies one; it is matched on the date it was
    # recorded, as the ledger's own sort does, rather than silently left out.
    dated = func.coalesce(Order.placed_at, Order.created_at)
    base = select(Order).join(Account, Account.id == Order.account_id).where(Order.order_id.is_not(None))
    if payload.emailid:
        base = base.where(func.lower(Account.email) == payload.emailid.strip().lower())
    if date_from is not None:
        base = base.where(dated >= date_from)
    if date_to is not None:
        base = base.where(dated < date_to)

    total = int(db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    page = (
        base.add_columns(Account.email)
        .options(selectinload(Order.units))
        .order_by(dated.desc().nulls_last(), Order.id.desc())
        .offset(payload.offset)
        .limit(payload.limit)
    )
    rows = db.execute(page).all()
    orders = [order for order, _ in rows]
    gst = orders_csv.gst_numbers(db, orders)
    paths = invoices.paths_on_file(db, [order.id for order in orders])
    readings = _cancel_readings(db, orders)
    data = [
        _summary_json(order, email, gst.get(order.id), paths.get(order.id), readings.get(order.id))
        for order, email in rows
    ]
    return {
        "success": True,
        "filters": {
            "emailid": payload.emailid,
            "fromDate": payload.fromDate,
            "toDate": payload.toDate,
            "dateField": "orderDate, or recordedDate for an order with no order date yet",
        },
        "pageInfo": {
            "offset": payload.offset,
            "limit": payload.limit,
            "count": len(data),
            "total": total,
            "hasMore": payload.offset + len(data) < total,
            "nextOffset": payload.offset + payload.limit,
        },
        "data": data,
    }
