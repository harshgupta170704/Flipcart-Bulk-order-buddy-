"""The unit grain: one row per unit of an order, as Flipkart reports on it.

``Order`` keeps what this tool intended and paid for — the account, the
product, the amount, the cap, the payment, the run. ``OrderUnit`` keeps what the
platform later said about one piece of it: its own id, status, delivery date,
tracking id, seller, invoice link. An order of three units is three unit rows,
which is how a partial delivery or one cancelled unit becomes visible at all;
until these rows existed the read-back applied only the first unit it saw and
dropped the rest.

Two writers create units: an order run (one placeholder per requested unit,
filled in by the next read-back) and the read-back itself (one unit per
observation; an order this tool never placed arrives this way). An uploaded
list of order ids to track is the third, through the same helper.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core import unit_events
from app.core.order_status import DELIVERED, IN_TRANSIT, UNKNOWN, classify
from app.db.models import Order, OrderStatus, OrderUnit, utcnow

#: The only platform for now (owner's decision, 24 Oct 2026). A column rather
#: than an assumption, so a second platform is an addition, not a migration.
PLATFORM = "flipkart"

SOURCE_RUN = "run"
SOURCE_SYNC = "sync"
SOURCE_UPLOAD = "upload"

#: What a read-back observes per unit and may write onto its row, one field
#: at a time. ``item_id`` and ``unit_id`` are identity, handled separately;
#: model/colour/amount are the order's, never a unit's; the delivery address
#: and its pincode travel together (:data:`ADDRESS_FIELDS`).
OBSERVED_UNIT_FIELDS = (
    "platform_status",
    "delivery_date",
    # The promise and the delivery, apart (26 Oct 2026). A later promise
    # replaces an earlier one; a page that shows none — every page once the
    # unit is delivered — leaves the last one standing, like every field here.
    # The first promise is kept apart as well, written once
    # (``first_promised_date``, in apply_observations): lateness counts from it.
    "promised_date",
    "delivered_date",
    "payment_method",
    "amount_paid",
    "seller",
    "tracking_id",
    # The courier, read off the same line of the order's history as the
    # tracking id (27 Oct 2026); a report fills it only on a unit not read.
    "shipment_partner",
    "invoice_url",
    "delivery_otp",
    "billed_gstin",
    "billed_business_name",
    "cancel_reason",
    "timeline",
)
#: Observed as one. The pincode is read out of the address text, so a page
#: that shows a new address brings that address's pincode — or none, when it
#: could not be read — and never leaves the old one standing beside it.
ADDRESS_FIELDS = ("delivery_address", "delivery_pincode")

#: The width of ``OrderUnit.cancel_reason``.
CANCEL_REASON_MAX = 255
#: The width of ``OrderUnit.payment_method``.
PAYMENT_METHOD_MAX = 100


def cancel_reason_text(value: object) -> str | None:
    """A cancellation reason as the unit row keeps it: one line, single-spaced,
    cut to the column's width; None for nothing.

    Here rather than left to the reader: the read-back's reader trims text but
    does not shorten it, and a longer value fails the write on a database that
    enforces the width (PostgreSQL) — taking the account's whole read-back
    with it. Applied the same whoever hands the reason over, so a re-read of
    the same page compares equal and moves nothing.
    """
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:CANCEL_REASON_MAX].rstrip() or None


#: Observed fields tidied after reading and before comparing, by field.
def payment_method_text(value: object) -> str | None:
    """How a unit was paid, as the row keeps it: one line, single-spaced, cut
    to the column's width; None for nothing — for the reason the cancellation
    reason is tidied here too (a longer value fails the write on PostgreSQL)."""
    if value is None:
        return None
    text = " ".join(str(value).split())[:PAYMENT_METHOD_MAX].rstrip()
    return text or None


def amount_paid_value(value: object) -> float | None:
    """A unit's page's "Total amount" — paid, or to be paid on delivery — as a
    number; None for nothing, or for anything that is not a sensible amount
    (negative, not finite)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) and amount >= 0 else None


_TIDY = {
    "cancel_reason": cancel_reason_text,
    "payment_method": payment_method_text,
    "amount_paid": amount_paid_value,
}


def normalised_line(text: str | None) -> str:
    """An address as compared: lower-case, letters and digits only, one space
    between words — so "Tower 5, 1005" and "tower-5 1005" are the same line.
    One reading for "did the address change?" here and "is it one of ours?"
    in the alerts, so a page re-rendering its punctuation moves neither."""
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in (text or ""))
    return " ".join(cleaned.split())


def ensure_units(order: Order, *, source: str = SOURCE_RUN) -> list[OrderUnit]:
    """Give an order its unit rows if it has none: one per requested unit.

    The first unit carries whatever the order row already knew from an earlier
    read-back (rows older than this table hold the first unit's ids and status
    on the order itself); the rest are placeholders — no unit id yet — that
    the next read-back fills in order.
    """
    if order.units:
        return list(order.units)
    count = max(1, int(order.qty or 1))
    units: list[OrderUnit] = []
    for position in range(1, count + 1):
        unit = OrderUnit(platform=PLATFORM, position=position, source=source)
        if position == 1:
            unit.unit_id = order.unit_id
            unit.item_id = order.item_id
            unit.seller = order.seller
            unit.tracking_id = order.tracking_id
            unit.platform_status = order.platform_status
            unit.delivery_date = order.delivery_date
            unit.delivery_otp = order.delivery_otp
            unit.invoice_url = order.invoice_url
            unit.synced_at = order.synced_at
        order.units.append(unit)
        units.append(unit)
    return units


def primary_unit(order: Order) -> OrderUnit | None:
    """The order's first unit by position: the row shown for a caller that
    holds one order (app.schemas.order.OrderOut.from_order).

    Not the unit the order row mirrors: the read-back decides that from every
    unit the platform has shown (app.jobs.sync_tasks._mirror_from_units), and
    :func:`representative_unit` is the one that speaks for the order's state.
    """
    if not order.units:
        return None
    return min(order.units, key=lambda unit: unit.position)


def representative_unit(order: Order) -> OrderUnit | None:
    """The unit whose reading stands for the whole order where one must — the
    Order Details API's order-level status, wording and delivery state.

    The read-back's rule for the order row (app.jobs.sync_tasks._mirror_from_units),
    plus one step. Among the units that carry a reading — the platform has
    shown them (``synced_at`` set), or an order report filed their status —
    and all of them when none does: the first still on its way, so an order
    with a unit outstanding never reads delivered or cancelled; then the first
    whose wording is unfamiliar; then the first delivered, so an order that
    stands with units delivered and cancelled reads delivered (the mirror falls
    back to position there); then the first by position. A placeholder nothing
    was read of decides nothing: a requested unit that never existed (the
    quantity was capped) would otherwise hold a delivered order at "unknown"
    for ever.

    A report leaves ``synced_at`` empty on purpose (app.io.order_report: no
    read-back has seen the unit), and counting only the units read back made
    an order with one unit read delivered and the other filed as shipped read
    delivered while that unit was still to come (review of 27 Oct 2026).
    """
    units = sorted(order.units, key=lambda unit: unit.position)
    if order.status == OrderStatus.failed and order.order_id:
        # Flipkart took this order and it failed on the units the read-back
        # read (app.jobs.sync_tasks._mirror_from_units): its reading comes from
        # those alone, never from a unit a report filed since, or the order
        # row would read failed and "Shipped" at once (checker of 27 Oct 2026).
        shown = [unit for unit in units if unit.synced_at is not None] or units
    else:
        shown = [unit for unit in units if unit.synced_at is not None or (unit.platform_status or "").strip()] or units
    if not shown:
        return None
    states = {id(unit): unit.delivery_state or classify(unit.platform_status) for unit in shown}
    for wanted in (IN_TRANSIT, UNKNOWN, DELIVERED):
        found = next((unit for unit in shown if states[id(unit)] == wanted), None)
        if found is not None:
            return found
    return shown[0]


@dataclass
class UnitChanges:
    """What one read-back did to one order's units."""

    created: int = 0
    #: Placeholders that received their unit id.
    filled: int = 0
    #: Units whose observed fields actually moved.
    updated: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.created or self.filled or self.updated)


def _key(value: object) -> str | None:
    """A unit id as stored: trimmed and upper-cased, matched by equality."""
    if value is None:
        return None
    text = str(value).strip().upper()
    return text or None


def _strip(value: object) -> str | None:
    """An item id as read: trimmed, never matched on."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _taken_elsewhere(db: DbSession, unit_id: str, order: Order) -> bool:
    """Whether another order's unit already carries this unit id.

    A unit id is unique per platform (spec FR-024). The one way two orders
    come to claim the same id is a duplicate order row — a data problem the
    duplicate itself exposes, so the second row's unit simply keeps no id
    rather than failing the whole read-back on the constraint.
    """
    stmt = select(OrderUnit.id).where(OrderUnit.platform == PLATFORM, OrderUnit.unit_id == unit_id)
    if order.id is not None:
        stmt = stmt.where(OrderUnit.order_pk != order.id)
    return db.scalar(stmt.limit(1)) is not None


def _read(observation: object, field: str):
    """Default field reader: the attribute as the observation holds it."""
    return getattr(observation, field, None)


def apply_observations(
    db: DbSession,
    order: Order,
    observations: Sequence[object],
    *,
    source: str = SOURCE_SYNC,
    now: datetime | None = None,
    read=_read,
) -> UnitChanges:
    """Fold a read-back's observations of one order into its unit rows.

    Each observation finds its unit by unit id; one without a known id takes
    the next placeholder in position order; when neither exists the order has
    more units than we knew of and a row is added. A field the platform did
    not show never blanks one we hold (the same rule as the order row), and
    ``synced_at`` is stamped on every observed unit whether or not anything
    moved — "nothing changed" and "never looked" are different answers.

    Placeholders left over are kept, not deleted: a requested unit the platform
    never listed is information (the quantity was not honoured), and the
    recent-orders page may simply have cut an order's units at its row limit.

    A unit this pass finds delivered, out for delivery, cancelled, returned or
    failed, when it was not before, also gets a row in the change feed
    (app.core.unit_events), in the same transaction.
    """
    stamp = now or utcnow()
    changes = UnitChanges()
    # An order a run placed keeps "run" on its placeholders even when the
    # read-back is what first materialises them; one the platform showed us
    # first is "sync" through and through.
    ensure_units(order, source=SOURCE_RUN if order.job_id is not None else source)
    touched: set[int] = set()

    for observation in observations:
        unit_id = _key(read(observation, "unit_id"))
        unit: OrderUnit | None = None
        claimable = bool(unit_id)
        if unit_id:
            unit = next((u for u in order.units if u.unit_id == unit_id), None)
            if unit is None and _taken_elsewhere(db, unit_id, order):
                claimable = False
        if unit is None:
            unit = next(
                (u for u in order.units if u.unit_id is None and id(u) not in touched), None
            )
            if unit is None and not unit_id:
                # An observation with no unit id of an order whose units are all
                # known — the by-id read-back of a page with no unit links — is
                # a report on the next unit in position order, not a new unit.
                unit = next(
                    (
                        u
                        for u in sorted(order.units, key=lambda item: item.position)
                        if id(u) not in touched
                    ),
                    None,
                )
            if unit is None:
                unit = OrderUnit(platform=PLATFORM, position=len(order.units) + 1, source=source)
                order.units.append(unit)
                changes.created += 1
            elif claimable and unit_id and unit.unit_id is None:
                changes.filled += 1  # a placeholder got its id
            if claimable and unit_id and unit.unit_id is None:
                unit.unit_id = unit_id
        touched.add(id(unit))

        # What the unit read as before this observation (None for its first
        # reading), so the change feed can tell what this page changed.
        before = unit_events.previous_reading(unit)
        moved = False
        item_id = _strip(read(observation, "item_id"))
        if item_id and unit.item_id != item_id:
            unit.item_id = item_id
            moved = True
        earlier_promise = unit.promised_date
        for field in OBSERVED_UNIT_FIELDS:
            value = read(observation, field)
            tidy = _TIDY.get(field)
            if tidy is not None:
                value = tidy(value)
            if value is None:
                continue
            if getattr(unit, field) != value:
                setattr(unit, field, value)
                moved = True
        # The first promise, written once: Flipkart moves its promise when a
        # delivery slips, and a unit that arrives on the moved day must still
        # read late against the day it was first promised. A row read back
        # before this column existed holds its earlier promise in
        # promised_date only, and that one is older than this page's.
        if unit.first_promised_date is None and read(observation, "promised_date") is not None:
            unit.first_promised_date = earlier_promise or unit.promised_date
            moved = True
        address = read(observation, "delivery_address")
        if address is not None:
            pincode = read(observation, "delivery_pincode")
            if unit.delivery_address is not None and normalised_line(
                unit.delivery_address
            ) != normalised_line(address):
                # A different address than the one seen before: the
                # address-change alert's cue (spec FR-045). The first
                # snapshot is not a change, and nor is the same address
                # rendered with other punctuation, case or spacing.
                unit.address_changed_at = stamp
            if (unit.delivery_address, unit.delivery_pincode) != (address, pincode):
                unit.delivery_address = address
                unit.delivery_pincode = pincode
                moved = True
        unit.synced_at = stamp
        unit_events.record(db, order, unit, before, at=stamp)
        if moved:
            changes.updated += 1

    # Make this pass's unit ids visible to the next order in the same session
    # (autoflush is off), so a duplicate order row cannot claim them again.
    db.flush()
    return changes


def _ensure_units_before_flush(session, _flush_context, _instances) -> None:
    """No order is ever written without its units, whoever wrote it.

    Every writer — the order run, the read-back, an upload, a test — goes
    through a flush, so this is the one place the rule holds. Units appended
    here are part of the same flush.
    """
    for obj in list(session.new):
        if isinstance(obj, Order) and not obj.units:
            ensure_units(obj)


def install_flush_guard() -> None:
    """Register the flush-time rule once per process (idempotent)."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    if not event.contains(Session, "before_flush", _ensure_units_before_flush):
        event.listen(Session, "before_flush", _ensure_units_before_flush)


def units_by_order(db: DbSession, order_pks: Sequence[int]) -> dict[int, int]:
    """How many unit rows each of these orders has, in one query."""
    from sqlalchemy import func

    if not order_pks:
        return {}
    rows = db.execute(
        select(OrderUnit.order_pk, func.count())
        .where(OrderUnit.order_pk.in_(list(order_pks)))
        .group_by(OrderUnit.order_pk)
    ).all()
    return {int(pk): int(count) for pk, count in rows}


# --- the order page's timeline, as a unit row keeps it --------------------------
def timeline_steps(value) -> list[dict] | None:
    """A unit's timeline as ``[{"status": str, "date": str | None}, …]``, or None.

    The read-back writes a list of such dicts. Rows written before 26 Oct 2026
    hold the list's Python text instead ("[{'status': 'Order Confirmed', …}]"),
    because every observed field used to be turned into text on the way in;
    both are read here, so the order dialog shows the steps either way and
    never one step per character.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        import ast
        import json

        try:
            value = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            try:
                value = json.loads(text)
            except ValueError:
                return None
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        return None
    steps: list[dict] = []
    for step in value:
        if isinstance(step, dict):
            status, date = step.get("status"), step.get("date")
        elif isinstance(step, (list, tuple)) and len(step) == 2:
            status, date = step
        else:
            continue
        status = str(status).strip() if status is not None else ""
        if not status:
            continue
        if isinstance(date, datetime):
            date = date.isoformat()
        elif date is not None:
            date = str(date).strip() or None
        steps.append({"status": status, "date": date})
    return steps or None


# --- out for delivery ------------------------------------------------------------
#: Flipkart's wording for a unit on the courier's van, matched whatever its
#: case ("Out for delivery", "Out For Delivery").
OUT_FOR_DELIVERY_LIKE = "%out for delivery%"


def out_for_delivery_rows(db: DbSession) -> list:
    """``(unit, order, account email)`` for every unit of a placed order that
    is out for delivery as of its last read-back — Flipkart's wording says so,
    or the page showed a delivery code on a unit still on its way (the code is
    shown only from that moment). Newest read-back first. What the dashboard's
    card and the timer's follow-ups read (app.api.stats, app.jobs.schedule)."""
    from sqlalchemy import and_, func, or_

    from app.db.models import Account, OrderStatus

    wording = func.lower(func.coalesce(OrderUnit.platform_status, ""))
    on_its_way = or_(OrderUnit.delivery_state.is_(None), OrderUnit.delivery_state.in_(("in_transit", "unknown")))
    return db.execute(
        select(OrderUnit, Order, Account.email)
        .join(Order, Order.id == OrderUnit.order_pk)
        .outerjoin(Account, Account.id == Order.account_id)
        .where(
            Order.status == OrderStatus.placed,
            or_(wording.like(OUT_FOR_DELIVERY_LIKE), and_(OrderUnit.delivery_otp.is_not(None), on_its_way)),
        )
        .order_by(OrderUnit.synced_at.desc().nulls_last(), OrderUnit.id.desc())
    ).all()


def unit_state_for_order(unit: OrderUnit) -> str:
    """A unit's state for deciding what its order row says: its wording's,
    except that a refunded cancellation is a cancellation. Both a cancelled
    card order and a return read "Refund Completed"; the cancelled one's own
    page still shows the cancellation, and nothing says it was delivered
    (``cancellations.shows_cancellation``) — so the order is failed, as the CSV
    and the change feed already call that unit cancelled (found by review,
    27 Oct 2026). A real return, delivered first, stays a return."""
    from app.core import cancellations  # local: it imports this module
    from app.core.order_status import CANCELLED, RETURNED

    state = classify(unit.platform_status)
    if state == RETURNED and cancellations.shows_cancellation(unit):
        return CANCELLED
    return state


def every_read_unit_cancelled(order: Order) -> bool:
    """Whether the order no longer stands: every unit the platform has shown
    reads cancelled (a refunded cancellation and an order never placed
    included), and at least one has been shown. The read-back's rule for
    marking an order failed (app.jobs.sync_tasks._mirror_from_units), kept
    here so start-up applies the same one when a reading changes."""
    from app.core.order_status import CANCELLED

    observed = [unit_state_for_order(unit) for unit in order.units if unit.synced_at is not None]
    return bool(observed) and all(state == CANCELLED for state in observed)


def sync_unit_readings(batch: int = 500) -> dict[str, int]:
    """Bring stored readings in line with the current code, at start-up.

    Re-derives ``delivery_state`` wherever the shared reading of the
    platform's wording has changed since the row was written ("Delivery
    expected" became in transit on 26 Oct 2026; the read-back only rewrites a
    status whose wording changed, so such rows would otherwise keep the old
    reading for ever), and turns timelines stored as text back into lists.
    Only rows that differ are written. Returns how many of each changed.

    An order that stood as placed only because of the old reading follows:
    when a unit's wording now reads cancelled ("Order Not Placed" read as on
    its way until 11 Nov 2026) and every unit the platform has shown reads so,
    the order is marked failed here — the read-back's own rule
    (:func:`every_read_unit_cancelled`) — instead of waiting for a read-back
    that an account signed out since would never give it.

    It writes nothing to the change feed (app.core.unit_events): a new reading
    of the same wording is the code changing, not the platform, and nothing
    happened to the unit that the operator needs to hear about.
    """
    from app.core.order_status import CANCELLED, classify
    from app.db.base import SessionLocal

    changed = {"states": 0, "timelines": 0, "orders": 0}
    with SessionLocal() as db:
        now_cancelled: set[int] = set()
        for model in (OrderUnit, Order):
            last = 0
            while True:
                rows = db.scalars(
                    select(model)
                    .where(model.id > last, model.platform_status.is_not(None))
                    .order_by(model.id)
                    .limit(batch)
                ).all()
                if not rows:
                    break
                for row in rows:
                    last = row.id
                    state = classify(row.platform_status)
                    if row.delivery_state != state:
                        row.delivery_state = state
                        changed["states"] += 1
                        if state == CANCELLED:
                            now_cancelled.add(row.order_pk if model is OrderUnit else row.id)
                db.commit()
        pks = sorted(now_cancelled)
        for start in range(0, len(pks), batch):
            orders = db.scalars(
                select(Order).where(Order.id.in_(pks[start : start + batch]), Order.status == OrderStatus.placed)
            ).all()
            for order in orders:
                if order.order_id and every_read_unit_cancelled(order):
                    order.status = OrderStatus.failed
                    changed["orders"] += 1
            db.commit()
        last = 0
        while True:
            rows = db.scalars(
                select(OrderUnit).where(OrderUnit.id > last, OrderUnit.timeline.is_not(None)).order_by(OrderUnit.id).limit(batch)
            ).all()
            if not rows:
                break
            for row in rows:
                last = row.id
                if isinstance(row.timeline, str):
                    row.timeline = timeline_steps(row.timeline)
                    changed["timelines"] += 1
            db.commit()
    return changed
