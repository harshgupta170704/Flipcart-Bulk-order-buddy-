"""What changed since the operator last looked: one row per change in a unit's
delivery state (the owner's ask of 26 Oct 2026).

The timer reads back every account with an order on its way — hourly, every
half hour once a unit is out for delivery (app.jobs.schedule.tracking_due) —
and until these rows a read-back that found a phone delivered or cancelled said
so nowhere. The ledger showed the new state, and the operator had to scan it to
notice. Now the read-back writes a ``UnitEvent`` the moment it sees a change,
and the dashboard lists the ones the signed-in user has not marked as seen
(app.api.changes, ``User.changes_seen_at``).

**What counts as a change.** A unit's reading is the portal's one reading of
Flipkart's wording (app.core.order_status), with one addition the operator
asked for by name: a unit still on its way that Flipkart calls out for
delivery, or whose page shows the delivery code (the code appears only from
that moment), reads as ``out_for_delivery`` — the same test as the dashboard's
"Out for delivery" card (app.core.order_units.out_for_delivery_rows). An event
is written when a read-back moves the reading into one of :data:`KINDS`. A move
into "in transit" or "unknown" is not written: every order gets shipped, and
"unknown" is wording nobody recognises, not news. Nor is the refund on a unit
already cancelled, which the portal reads as returned (:data:`_NOT_NEWS`). And
a refund on a unit that was never delivered and whose own page shows a
cancellation reads as that cancellation (:func:`unit_reading`): a read-back
that first sees a cancelled card order once Flipkart has refunded it writes
"Cancelled", not "Returned".

**A unit's first reading.** A unit never read back before has no state to
change from, so its first reading is normally not a change. A list of order ids
handed in to track, or the history an import pulls in, is mostly deliveries
that happened weeks ago; writing those would bury the one that just happened.
The exception is an order this tool placed (``Order.job_id``): the operator is
waiting to hear about it, and Flipkart cancelling it before the first
read-back — as it did with all three Cash-on-Delivery orders of 24–26 Oct
2026 — is exactly the news this exists for. So a run's unit first read as
delivered, out for delivery, cancelled, returned or failed is written, with no
``from_state``.

**Nothing else writes.** Only the read-back's fold
(app.core.order_units.apply_observations) calls :func:`record`. The start-up
re-derivation (order_units.sync_unit_readings) rewrites ``delivery_state``
where the code now reads old wording differently; that is the code changing,
not the platform, so it writes nothing. And because readings here come from
the wording each time, never from the stored column, a row it rewrote cannot
look changed at its next read-back either.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session as DbSession

from app.core import cancellations, order_status
from app.db.models import Order, OrderUnit, UnitEvent

#: The reading of a unit on the courier's van. Not one of order_status's
#: states (to the rest of the portal such a unit is simply in transit).
OUT_FOR_DELIVERY = "out_for_delivery"
#: Flipkart's wording for it, whatever its case or spacing — the scraper's
#: own test (app.automation.sync).
_OUT_FOR_DELIVERY_RE = re.compile(r"out\s*for\s*delivery", re.IGNORECASE)

#: The changes worth telling the operator about, in the order the dashboard
#: lists their kinds.
KINDS: tuple[str, ...] = (
    order_status.DELIVERED,
    OUT_FOR_DELIVERY,
    order_status.CANCELLED,
    order_status.RETURNED,
    order_status.FAILED,
)

#: Moves that are money coming back on a unit already settled, not news. The
#: portal reads "Refund completed" as returned (app.core.order_status), and
#: that is how Flipkart words a cancelled card order once it has refunded it:
#: no phone came back, and the cancellation was the news. A return after a
#: delivery is still written.
_NOT_NEWS = frozenset(
    {
        (order_status.CANCELLED, order_status.RETURNED),
        (order_status.FAILED, order_status.RETURNED),
    }
)

#: How far back the feed reaches for someone who has never marked it seen.
UNSEEN_DAYS = 7
#: And never further back than this, however long ago they last looked:
#: someone back from a month away gets the last month, not the whole history.
MAX_DAYS = 30


def reading(platform_status: str | None, delivery_otp: str | None = None) -> str:
    """A unit's state as the change feed reads it: order_status's reading of
    the wording, or ``out_for_delivery`` for a unit still on its way that
    Flipkart calls out for delivery or that shows a delivery code. A settled
    reading always wins: a delivered unit keeps the code it was delivered
    with, and is delivered."""
    state = order_status.classify(platform_status)
    if state in order_status.UNDELIVERED and (
        _OUT_FOR_DELIVERY_RE.search(platform_status or "") or delivery_otp
    ):
        return OUT_FOR_DELIVERY
    return state


def unit_reading(unit: OrderUnit, before: str | None = None) -> str:
    """One unit's reading as the feed records it: :func:`reading` of its
    wording, except that a refund on a unit that was cancelled reads as
    cancelled.

    Flipkart words a card order it cancelled "Refund Completed" once the money
    is back, which the portal reads as returned. A read-back that sees only
    the refund — the cancellation and the refund both came between two
    read-backs, or before a run's first one — would write a return where no
    phone came back. So a refund on a unit that was not delivered (``before``,
    its reading until now, is not delivered, and no step of its page's
    timeline is) and whose own page shows a cancellation
    (app.core.cancellations.shows_cancellation) reads as cancelled. With no
    sign of a cancellation it stays returned, even at a first reading: a
    run's unit can be delivered and sent back before its first successful
    read-back (the laptop off, a lapsed sign-in).
    """
    state = reading(unit.platform_status, unit.delivery_otp)
    if (
        state == order_status.RETURNED
        and before != order_status.DELIVERED
        and cancellations.shows_cancellation(unit)
    ):
        return order_status.CANCELLED
    return state


def previous_reading(unit: OrderUnit) -> str | None:
    """The unit's reading before a read-back touches it (:func:`unit_reading`
    of what it holds), or None when it has none: it was never read back
    (``synced_at`` unset), or this read-back is creating the row (an imported
    order, a unit more than the order asked for), whose fields were copied
    from the order row a moment ago rather than read off a page before."""
    if unit.synced_at is None or not sa_inspect(unit).persistent:
        return None
    return unit_reading(unit)


def record(
    db: DbSession, order: Order, unit: OrderUnit, before: str | None, *, at: datetime
) -> UnitEvent | None:
    """Write the change one observation made to one unit, if it is news.

    ``before`` is :func:`previous_reading`, taken before the observation was
    folded in; the unit now holds what the page showed, and ``at`` is when the
    read-back saw it. The new reading is :func:`unit_reading`'s, the same
    question ``before`` was asked. Nothing is written when it is not one of
    :data:`KINDS` or is the same as before, for a refund on a unit already
    cancelled (:data:`_NOT_NEWS`), for the first reading of a unit this tool
    did not place (see the module docstring), or when the unit's last event
    already says the same: a page that read "Out for delivery", then
    "Arriving today", then "Out for delivery" again is one piece of news, not
    two. The event joins the read-back's own transaction.
    """
    after = unit_reading(unit, before)
    if after not in KINDS or after == before or (before, after) in _NOT_NEWS:
        return None
    if before is None and order.job_id is None:
        return None
    # Loaded only here, when there is a change to write: a unit has a handful
    # of events at most, and a new unit has none to load.
    if unit.events and unit.events[-1].kind == after:
        return None
    event = UnitEvent(
        unit=unit,
        order=order,
        account_id=order.account_id,
        at=naive_utc(at),
        kind=after,
        from_state=before,
        to_state=after,
    )
    db.add(event)
    return event


def window_start(seen_at: datetime | None, now: datetime) -> tuple[datetime, bool]:
    """Where someone's feed starts, and whether :data:`MAX_DAYS` cut it short.

    From their own mark when they have one, else :data:`UNSEEN_DAYS` back;
    never further back than :data:`MAX_DAYS`. Both stamps naive UTC, like
    ``UnitEvent.at``.
    """
    moment = naive_utc(now)
    floor = moment - timedelta(days=MAX_DAYS)
    start = naive_utc(seen_at) if seen_at is not None else moment - timedelta(days=UNSEEN_DAYS)
    if start < floor:
        return floor, True
    return start, False


def naive_utc(moment: datetime) -> datetime:
    """A stamp as the plain DateTime columns hold it: UTC, without a zone. The
    read-back's own stamp carries its zone, one read back from the database
    does not, and an offset-less stamp from the browser is UTC already (the
    API writes every stamp that way)."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)
