"""Orders Flipkart cancelled, told apart from the tool's own failures.

``Order.status`` reads "failed" in two different cases. A run that stopped
before anything was sent to Flipkart records a failed attempt: the tool's own
failure, and the only kind the dashboard's "Failed" tile means ("Stopped before
anything was sent to Flipkart"). An order Flipkart took and later cancelled is
marked failed too, by the read-back (app.jobs.sync_tasks: once every unit reads
cancelled the order no longer stands, and its units stop counting against the
unit limits). Until 26 Oct 2026 both showed the same red "Failed".

Flipkart's order id is what tells them apart. A run records one only once
Flipkart has confirmed the order, and no run marks an order failed after that
(app.automation.ordering: from the moment an order may exist, nothing says
"failed"); so a failed order WITH an order id is one Flipkart took and then
cancelled. The dashboard, the alerts and the ledger all ask this one question
(the ledger page puts it to the rows it shows the same way, in
frontend/src/lib/orderOutcome.ts).

Who cancelled: the user — the account that bought — when the tool holds a
cancel request for the unit that is open or was marked done (spec FR-020: a
person records it here and does it on Flipkart by hand), when the unit's
status or reason says so in the platform's own words
(app.core.order_status.cancelled_by_user), or when an order report said so
(``OrderUnit.cancelled_by_user``, spec FR-022); otherwise Flipkart.

Whether one unit was cancelled is asked here too (:func:`unit_was_cancelled`),
because its latest wording does not always say so: Flipkart words a card order
it cancelled "Refund Completed" once the money is back, which the portal reads
as returned. The unit's own page still shows the cancellation
(:func:`shows_cancellation`), and the change feed reads it the same way.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, and_, exists, select
from sqlalchemy.orm import Session as DbSession

from app.core.order_status import CANCELLED, DELIVERED, IN_TRANSIT, cancelled_by_user, classify
from app.core.unit_requests import KIND_CANCEL, STATUS_DONE, STATUS_OPEN
from app.db.models import Order, OrderStatus, OrderUnit, UnitRequest

#: Who cancelled an order the platform took, as the API and the alerts name it.
BY_PLATFORM = "flipkart"
BY_USER = "user"
#: The ledger page's Outcome for such an order, by who cancelled it — the values
#: of frontend/src/lib/orderOutcome.ts, which the exports write as well.
CANCELLED_OUTCOME: dict[str, str] = {BY_PLATFORM: "cancelled_by_flipkart", BY_USER: "cancelled_by_user"}

#: A cancel request still open, or one a person marked done, is someone asking
#: for the unit's cancellation. A dropped one was withdrawn and says nothing.
CANCEL_ASKED: tuple[str, ...] = (STATUS_OPEN, STATUS_DONE)
#: Ids per IN clause: SQLite's bound-parameter ceiling is 999 on older builds.
_CHUNK = 500


def placed_then_cancelled(order: Order) -> bool:
    """Flipkart took this order and later cancelled it (see the module docstring)."""
    return order.status == OrderStatus.failed and bool(order.order_id)


def placed_then_cancelled_sql() -> ColumnElement[bool]:
    """:func:`placed_then_cancelled` as SQL, for counting. The order id is
    stored trimmed and upper-cased, an empty one as NULL (the model's
    validator), so "has an order id" is a plain NOT NULL."""
    return and_(Order.status == OrderStatus.failed, Order.order_id.is_not(None))


def never_sent_sql() -> ColumnElement[bool]:
    """SQL for the tool's own failure: a failed attempt Flipkart never took."""
    return and_(Order.status == OrderStatus.failed, Order.order_id.is_(None))


def cancel_requested_sql() -> ColumnElement[bool]:
    """Whether the query's OrderUnit carries a cancel request, open or done:
    an EXISTS correlated with each row, so any number of rows is one query."""
    return (
        exists()
        .where(
            UnitRequest.unit_pk == OrderUnit.id,
            UnitRequest.kind == KIND_CANCEL,
            UnitRequest.status.in_(CANCEL_ASKED),
        )
        .correlate(OrderUnit)
    )


def units_with_cancel_request(db: DbSession, unit_pks: Iterable[int]) -> set[int]:
    """Which of these units carry a cancel request, open or done — one query
    per chunk of ids, never one per unit."""
    wanted = sorted({int(pk) for pk in unit_pks})
    found: set[int] = set()
    for start in range(0, len(wanted), _CHUNK):
        found.update(
            int(pk)
            for pk in db.scalars(
                select(UnitRequest.unit_pk)
                .where(
                    UnitRequest.unit_pk.in_(wanted[start : start + _CHUNK]),
                    UnitRequest.kind == KIND_CANCEL,
                    UnitRequest.status.in_(CANCEL_ASKED),
                )
                .distinct()
            )
        )
    return found


def _reported(unit: OrderUnit) -> bool | None:
    """What an order report said of who cancelled the unit
    (``OrderUnit.cancelled_by_user``): True, False, or None when none said."""
    return getattr(unit, "cancelled_by_user", None)


def _by_user(status: str | None, reason: str | None, requested: bool, reported: bool | None) -> bool:
    """The one rule for "the user cancelled", from what a unit says: a cancel
    request (open or done), Flipkart's wording, or an order report's "Yes".

    A report's "No" never outweighs the other two: a request recorded here,
    or Flipkart's own words, say more than a sheet (the read-back wins
    wherever a report meets it)."""
    return requested or reported is True or cancelled_by_user(status, reason)


def unit_cancelled_by_user(unit: OrderUnit, *, requested: bool = False) -> bool:
    """Whether the user cancelled this unit: someone asked the tool for its
    cancellation (``requested``, from :func:`units_with_cancel_request` or
    :func:`cancel_requested_sql`), its status or reason says so, or an order
    report said so.

    The report's word was read past until 27 Oct 2026: a report saying the
    user had cancelled left the row's Outcome "cancelled by Flipkart", the
    opposite (spec FR-022)."""
    return _by_user(unit.platform_status, unit.cancel_reason, requested, _reported(unit))


def _says_nothing(status: str | None, reason: str | None, requested: bool, reported: bool | None = None) -> bool:
    return not (status or reason or requested or reported is not None)


def says_nothing(unit: OrderUnit, *, requested: bool = False) -> bool:
    """Whether this unit says nothing of who cancelled it: no wording, no
    reason, no cancel request and no order report's word — a requested unit
    the platform never listed. The same test :func:`cancel_readings` puts to
    every unit, so the ledger, the dashboard and the alerts pass over the same
    units."""
    return _says_nothing(unit.platform_status, unit.cancel_reason, requested, _reported(unit))


def cancelled_by(
    order: Order, unit: OrderUnit, *, requested: bool = False, reading: CancelReading | None = None
) -> str | None:
    """Who cancelled ``order``, as one of its unit rows tells it: :data:`BY_USER`
    when someone asked the tool to cancel that unit (``requested``) or its
    wording says the user did, :data:`BY_PLATFORM` otherwise — and None when
    the order is not one the platform took and then cancelled.

    Read off the unit in hand, so a page of the ledger costs no query per
    row. A unit that says nothing (:func:`says_nothing`) is the exception:
    given ``reading``, its order's :class:`CancelReading`, it takes the
    order's verdict, the one the dashboard and the alerts give — the account's
    when every unit that says anything says the account's. Without one it
    reads as Flipkart's cancellation.
    """
    if not placed_then_cancelled(order):
        return None
    if reading is not None and says_nothing(unit, requested=requested):
        return BY_USER if reading.user_cancelled else BY_PLATFORM
    return BY_USER if unit_cancelled_by_user(unit, requested=requested) else BY_PLATFORM


def outcome_value(
    order: Order, unit: OrderUnit, *, requested: bool = False, reading: CancelReading | None = None
) -> str:
    """The ledger's Outcome for one unit row, as the exports write it: who
    cancelled an order the platform took and then cancelled
    (:data:`CANCELLED_OUTCOME`, the page's own values) — never the "failed" of
    a run that sent nothing — and the stored status's value otherwise
    ("placed", "failed", "failed_over_target"…). ``requested`` and ``reading``
    as for :func:`cancelled_by`: a unit someone asked to cancel is the user's,
    and a unit that says nothing takes its order's verdict, as on the page."""
    who = cancelled_by(order, unit, requested=requested, reading=reading)
    if who is not None:
        return CANCELLED_OUTCOME[who]
    return order.status.value if order.status is not None else ""


def shows_cancellation(unit: OrderUnit) -> bool:
    """Whether the unit's own order page, as last read back, shows a
    cancellation: Flipkart gave a reason for cancelling it, or its timeline
    has a "Cancelled" step — and nothing says it was delivered.

    What tells a refunded cancellation from a return. Both read as returned
    ("Refund Completed"), but a cancelled order's page keeps its "Cancelled,
    <date>" step. The reason alone would miss some: on a refunded page the
    line under that step is "Refund completed", which is no reason, so none
    is read (app.automation.sync.parse_cancel_reason).
    """
    # Local: order_units imports the change feed, which imports this module.
    from app.core.order_units import timeline_steps

    steps = {classify(step["status"]) for step in timeline_steps(unit.timeline) or ()}
    # A delivered unit is no cancellation, whatever else its page says. Its
    # delivery day says so even where no step reads as delivered: the order
    # history names that dated step "Delivery", which on its own reads as
    # nothing (app.automation.sync.parse_history).
    if DELIVERED in steps or getattr(unit, "delivered_date", None) is not None:
        return False
    return bool(unit.cancel_reason) or CANCELLED in steps


def unit_was_cancelled(order: Order, unit: OrderUnit) -> bool:
    """Whether this unit was cancelled, whatever its latest wording says.

    It was when its reading is cancelled; or when it is neither delivered nor
    on its way, and its order is one the platform took and then cancelled
    (:func:`placed_then_cancelled`) or its own page shows a cancellation
    (:func:`shows_cancellation`). That keeps a cancelled card order cancelled
    once Flipkart words it "Refund Completed". A refund reads returned, and the
    read-back puts a failed order back to placed only when a unit it reads for
    the first time is on its way or delivered (app.jobs.sync_tasks._revive),
    so a refund leaves the order failed: the ledger page, the dashboard, the
    alerts and the Outcome filter go on calling it cancelled, and the CSV must
    agree. An order put back reads placed everywhere, and this then asks the
    unit's own page (:func:`shows_cancellation`). A unit the platform never
    listed, of an order it cancelled, went with the order.
    """
    state = unit.delivery_state or classify(unit.platform_status)
    if state == CANCELLED:
        return True
    if state in (DELIVERED, IN_TRANSIT):
        return False
    return placed_then_cancelled(order) or shows_cancellation(unit)


@dataclass
class CancelReading:
    """One cancelled order, read unit by unit: whose each unit's cancellation
    was, and the reasons given, each once, in unit order."""

    #: Units the user cancelled, and units whose wording says otherwise. A unit
    #: with no wording and no request (one the platform never listed) is in
    #: neither: it says nothing either way.
    by_user: int = 0
    by_platform: int = 0
    user_reasons: list[str] = field(default_factory=list)
    #: The reasons of every unit that is not the user's cancellation.
    other_reasons: list[str] = field(default_factory=list)

    @property
    def user_cancelled(self) -> bool:
        """Every unit that says anything says the user. A mix is read as
        Flipkart's cancellation — the one a person should look at."""
        return self.by_user > 0 and self.by_platform == 0


def cancel_readings(db: DbSession, order_pks: Sequence[int]) -> dict[int, CancelReading]:
    """``{order: its CancelReading}`` for these orders — the alerts' and the
    dashboard's one verdict. One query per chunk of orders for the units, and
    one per chunk of units for the cancel requests; never one per order."""
    wanted = sorted({int(pk) for pk in order_pks})
    units: list[tuple[int, int, str | None, str | None, bool | None]] = []
    for start in range(0, len(wanted), _CHUNK):
        units.extend(
            (int(order_pk), int(unit_pk), status, reason, reported)
            for unit_pk, order_pk, status, reason, reported in db.execute(
                select(
                    OrderUnit.id,
                    OrderUnit.order_pk,
                    OrderUnit.platform_status,
                    OrderUnit.cancel_reason,
                    OrderUnit.cancelled_by_user,
                )
                .where(OrderUnit.order_pk.in_(wanted[start : start + _CHUNK]))
                .order_by(OrderUnit.order_pk, OrderUnit.position, OrderUnit.id)
            )
        )
    asked = units_with_cancel_request(db, [unit[1] for unit in units])
    readings: dict[int, CancelReading] = {}
    for order_pk, unit_pk, status, reason, reported in units:
        reading = readings.setdefault(order_pk, CancelReading())
        requested = unit_pk in asked
        if _says_nothing(status, reason, requested, reported):
            continue
        by_user = _by_user(status, reason, requested, reported)
        if by_user:
            reading.by_user += 1
        else:
            reading.by_platform += 1
        text = (reason or "").strip()
        reasons = reading.user_reasons if by_user else reading.other_reasons
        if text and text not in reasons:
            reasons.append(text)
    return readings
