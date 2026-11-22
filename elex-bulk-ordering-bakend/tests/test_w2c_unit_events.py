"""The change feed's writer (app.core.unit_events): a read-back that moves a
unit's delivery state writes one event, a re-read that moves nothing writes
none, a unit's first reading is news only for an order this tool placed, and
the start-up re-derivation writes nothing at all."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.automation.sync import ScrapedOrder
from app.core import unit_events
from app.core.order_units import apply_observations, sync_unit_readings
from app.db.models import (
    Account,
    AccountStatus,
    Job,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    OrderUnit,
    UnitEvent,
    naive_utcnow,
)
from app.jobs import sync_tasks


def _account(db, email: str = "events@test.com") -> Account:
    row = Account(email=email, status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    return row


def _order(db, account: Account, *, placed_by_run: bool = True, qty: int = 1, order_id: str = "OD100000000001") -> Order:
    """An order as a run places it (with its run), or as an id handed in to track (without)."""
    job_id = None
    if placed_by_run:
        job = Job(type=JobType.order, status=JobStatus.completed)
        db.add(job)
        db.commit()
        job_id = job.id
    order = Order(
        account_id=account.id,
        job_id=job_id,
        qty=qty,
        model="vivo T5x 5G",
        status=OrderStatus.placed,
        order_id=order_id,
        placed_at=naive_utcnow(),
    )
    db.add(order)
    db.commit()
    return order


def _read(db, order: Order, *pages: dict, now: datetime | None = None) -> None:
    """One read-back pass of ``order``: one observation per dict."""
    seen = [ScrapedOrder(order_id=order.order_id, **page) for page in pages]
    apply_observations(db, order, seen, now=now)
    db.commit()


def _events(db) -> list[tuple[str, str | None, str]]:
    return [(e.kind, e.from_state, e.to_state) for e in db.scalars(select(UnitEvent).order_by(UnitEvent.id))]


# --- the reading -----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("wording", "code", "state"),
    [
        ("Delivered", None, "delivered"),
        ("Out For Delivery", None, "out_for_delivery"),
        ("out for delivery", None, "out_for_delivery"),
        # The code is shown only once the unit is on the van.
        ("Shipped", "4827", "out_for_delivery"),
        ("Delivery expected by Nov 01", None, "in_transit"),
        # A settled reading wins over a code the unit kept.
        ("Delivered", "4827", "delivered"),
        ("Cancelled", "4827", "cancelled"),
        ("Return requested", None, "returned"),
        ("Undelivered", None, "failed"),
        (None, None, "unknown"),
    ],
)
def test_the_reading(wording, code, state):
    assert unit_events.reading(wording, code) == state


# --- a change, and no change --------------------------------------------------------------
def test_a_change_writes_one_event_and_a_re_read_writes_none(db):
    account = _account(db)
    order = _order(db, account)

    _read(db, order, {"unit_id": "u1", "platform_status": "Shipped"})
    assert _events(db) == []  # a first reading, and "in transit" is not news anyway

    _read(db, order, {"unit_id": "u1", "platform_status": "Out For Delivery"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Out For Delivery"})  # nothing moved
    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})  # nor here

    assert _events(db) == [
        ("out_for_delivery", "in_transit", "out_for_delivery"),
        ("delivered", "out_for_delivery", "delivered"),
    ]
    event = db.scalars(select(UnitEvent).order_by(UnitEvent.id.desc())).first()
    unit = order.units[0]
    assert (event.unit_pk, event.order_pk, event.account_id) == (unit.id, order.id, account.id)


def test_the_delivery_code_appearing_is_out_for_delivery(db):
    order = _order(db, _account(db))
    _read(db, order, {"unit_id": "u1", "platform_status": "Shipped"})

    _read(db, order, {"unit_id": "u1", "delivery_otp": "4827"})

    assert _events(db) == [("out_for_delivery", "in_transit", "out_for_delivery")]


def test_moving_into_transit_or_unknown_is_not_news(db):
    order = _order(db, _account(db))
    _read(db, order, {"unit_id": "u1", "platform_status": "Order Confirmed"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Shipped"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Partially delivered"})  # unrecognised on purpose

    assert _events(db) == []


def test_a_unit_back_in_the_state_it_was_last_recorded_in_is_not_news_twice(db):
    """"Out for delivery", then "Arriving today", then "Out for delivery" again
    — the list and the order page can word one unit differently."""
    order = _order(db, _account(db))
    _read(db, order, {"unit_id": "u1", "platform_status": "Shipped"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Out for delivery"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Arriving today"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Out for delivery"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})

    assert [kind for kind, _from, _to in _events(db)] == ["out_for_delivery", "delivered"]


def test_a_refund_on_a_cancelled_order_is_not_a_return(db):
    """The portal reads "Refund completed" as returned; on a cancelled card
    order no phone came back, and the cancellation was already the news."""
    order = _order(db, _account(db))
    _read(db, order, {"unit_id": "u1", "platform_status": "Shipped"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Cancelled"})

    _read(db, order, {"unit_id": "u1", "platform_status": "Refund Completed"})

    assert _events(db) == [("cancelled", "in_transit", "cancelled")]


def test_a_return_after_a_delivery_is_news(db):
    order = _order(db, _account(db))
    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})

    _read(db, order, {"unit_id": "u1", "platform_status": "Return requested"})

    assert _events(db) == [("delivered", None, "delivered"), ("returned", "delivered", "returned")]


def test_each_unit_of_an_order_has_its_own_events(db):
    order = _order(db, _account(db), qty=2)
    _read(db, order, {"unit_id": "u1", "platform_status": "Shipped"}, {"unit_id": "u2", "platform_status": "Shipped"})

    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"}, {"unit_id": "u2", "platform_status": "Cancelled"})

    by_unit = {(e.unit_pk, e.kind) for e in db.scalars(select(UnitEvent))}
    first, second = sorted(order.units, key=lambda unit: unit.position)
    assert by_unit == {(first.id, "delivered"), (second.id, "cancelled")}


# --- a unit's first reading ---------------------------------------------------------------
def test_an_order_this_tool_placed_first_seen_cancelled_is_news(db):
    """As Flipkart did with the three Cash-on-Delivery orders of 24–26 Oct 2026:
    cancelled before the first read-back. The operator must hear of it."""
    order = _order(db, _account(db))

    _read(db, order, {"unit_id": "u1", "platform_status": "Cancelled"})

    assert _events(db) == [("cancelled", None, "cancelled")]


def test_an_order_this_tool_placed_first_seen_delivered_is_news(db):
    order = _order(db, _account(db))

    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})

    assert _events(db) == [("delivered", None, "delivered")]


def test_an_id_handed_in_to_track_is_not_news_at_its_first_reading_but_is_after(db):
    """A list of ids is mostly orders delivered weeks ago: filling those in is
    not a change. What happens to them afterwards is."""
    account = _account(db)
    old = _order(db, account, placed_by_run=False, order_id="OD100000000001")
    open_one = _order(db, account, placed_by_run=False, order_id="OD100000000002")

    _read(db, old, {"unit_id": "u1", "platform_status": "Delivered"})
    _read(db, open_one, {"unit_id": "u2", "platform_status": "Shipped"})
    assert _events(db) == []

    _read(db, open_one, {"unit_id": "u2", "platform_status": "Delivered"})
    assert _events(db) == [("delivered", "in_transit", "delivered")]


def test_a_further_unit_of_an_order_this_tool_placed_is_news_at_its_first_reading(db):
    order = _order(db, _account(db), qty=1)

    _read(db, order, {"unit_id": "u1", "platform_status": "Shipped"}, {"unit_id": "u2", "platform_status": "Delivered"})

    assert _events(db) == [("delivered", None, "delivered")]
    assert db.scalar(select(UnitEvent.unit_pk)) == max(order.units, key=lambda unit: unit.position).id


# --- a refunded cancellation (review of 26 Oct 2026) -----------------------------------------
#: Flipkart's own reason, read back live on 24 and 25 Oct 2026.
SELLER_POLICY = "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand."
#: The steps of a cancelled order's page once refunded; dates made up.
CANCELLED_STEPS = [
    {"status": "Order Confirmed", "date": "2026-10-24T00:00:00"},
    {"status": "Cancelled", "date": "2026-10-24T00:00:00"},
    {"status": "Refunded", "date": "2026-10-26T00:00:00"},
]
#: A delivered unit's page with a cancellation line on it all the same: a
#: replacement asked for and then cancelled, the money refunded instead.
DELIVERED_STEPS = [
    {"status": "Order Confirmed", "date": "2026-10-20T00:00:00"},
    {"status": "Delivered", "date": "2026-10-22T00:00:00"},
    {"status": "Cancelled", "date": "2026-10-24T00:00:00"},
    {"status": "Refunded", "date": "2026-10-26T00:00:00"},
]
REPLACEMENT_CANCELLED = "Your replacement request has been cancelled."


def test_a_run_order_first_read_refunded_after_its_cancellation_is_a_cancellation(db):
    """Cancelled under seller policy and refunded before the first read-back
    got to it (the laptop asleep, a fast refund): the list reads "Refund
    Completed", which the portal reads as returned. No phone came back; the
    news is the cancellation the page gives a reason for."""
    order = _order(db, _account(db))

    _read(db, order, {"unit_id": "u1", "platform_status": "Refund Completed", "cancel_reason": SELLER_POLICY})

    assert _events(db) == [("cancelled", None, "cancelled")]


def test_a_cancelled_step_alone_shows_the_cancellation(db):
    """A refunded page prints "Refund completed" under its "Cancelled, <date>"
    step, so no reason is read; the step itself still shows the cancellation."""
    order = _order(db, _account(db))

    _read(db, order, {"unit_id": "u1", "platform_status": "Refund Completed", "timeline": CANCELLED_STEPS})

    assert _events(db) == [("cancelled", None, "cancelled")]


def test_a_cancellation_and_its_refund_between_two_read_backs_is_one_cancellation(db):
    """Seen on its way, then cancelled and refunded before the next look: a
    cancellation from "in transit" — and read again, refunded or cancelled,
    the same news, not written twice."""
    order = _order(db, _account(db))
    _read(db, order, {"unit_id": "u1", "platform_status": "Order Confirmed"})

    _read(db, order, {"unit_id": "u1", "platform_status": "Refund Completed", "cancel_reason": SELLER_POLICY})
    _read(db, order, {"unit_id": "u1", "platform_status": "Refund Completed", "timeline": CANCELLED_STEPS})
    _read(db, order, {"unit_id": "u1", "platform_status": "Cancelled"})

    assert _events(db) == [("cancelled", "in_transit", "cancelled")]


def test_a_refund_after_a_delivery_stays_a_return(db):
    """Delivered, then refunded: a return, whatever cancellation line the page
    also carries — from a delivery the feed saw, or one only the page's
    timeline shows (a run's unit read back for the first time)."""
    order = _order(db, _account(db))
    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})
    _read(db, order, {"unit_id": "u1", "platform_status": "Refund Completed", "cancel_reason": REPLACEMENT_CANCELLED})
    other = _order(db, _account(db, "events2@test.com"), order_id="OD100000000002")
    refunded = {"platform_status": "Refund Completed", "cancel_reason": REPLACEMENT_CANCELLED}
    _read(db, other, {"unit_id": "u2", **refunded, "timeline": DELIVERED_STEPS})

    # The order history names the dated delivery step "Delivery", which reads
    # as nothing on its own: the delivery day the read-back took from it is
    # what says the unit was delivered.
    third = _order(db, _account(db, "events3@test.com"), order_id="OD100000000003")
    history = [
        {"status": "Order Confirmed", "date": "2026-10-20T00:00:00"},
        {"status": "Delivery", "date": "2026-10-22T00:00:00"},
        {"status": "Cancelled", "date": "2026-10-24T00:00:00"},
    ]
    _read(db, third, {"unit_id": "u3", **refunded, "timeline": history, "delivered_date": datetime(2026, 10, 22)})

    assert _events(db) == [
        ("delivered", None, "delivered"),
        ("returned", "delivered", "returned"),
        ("returned", None, "returned"),
        ("returned", None, "returned"),
    ]


def test_a_tracked_orders_refunded_cancellation_read_twice_writes_nothing(db):
    """An order handed in to track (no run): its first reading is never news,
    and its second, the same refunded cancellation, is no change either — the
    reading before the second look is a cancellation too, not a return."""
    order = _order(db, _account(db), placed_by_run=False)
    page = {"unit_id": "u1", "platform_status": "Refund Completed", "cancel_reason": SELLER_POLICY}

    _read(db, order, page)
    _read(db, order, page)

    assert _events(db) == []


def test_a_run_order_first_read_refunded_with_no_sign_of_a_cancellation_is_a_return(db):
    """A run's unit can be delivered and sent back before its first successful
    read-back (the laptop off, a lapsed sign-in): with nothing on its page
    saying it was cancelled, the refund is a return, and still news."""
    order = _order(db, _account(db))

    _read(db, order, {"unit_id": "u1", "platform_status": "Refund Completed"})

    assert _events(db) == [("returned", None, "returned")]


def test_the_read_back_writes_a_refunded_cancellation_once(db):
    """Through the read-back's own fold, twice, as the timer runs it: the
    first pass writes the cancellation, the second sees nothing new."""
    account = _account(db)
    order = _order(db, account, order_id="OD338980309549917101")
    page = [
        ScrapedOrder(
            order_id="OD338980309549917101",
            unit_id="u1",
            platform_status="Refund Completed",
            cancel_reason=SELLER_POLICY,
            timeline=CANCELLED_STEPS,
        )
    ]

    sync_tasks._reconcile(account.id, page, import_unknown=False)
    sync_tasks._reconcile(account.id, page, import_unknown=False)

    db.expire_all()
    assert _events(db) == [("cancelled", None, "cancelled")]
    # What the read-back stores is still Flipkart's wording, read as returned.
    unit = db.get(Order, order.id).units[0]
    assert (unit.platform_status, unit.delivery_state) == ("Refund Completed", "returned")


# --- the whole read-back ---------------------------------------------------------------------
def test_the_read_back_writes_its_events_in_its_own_transaction(db):
    """Through the read-back's own fold (app.jobs.sync_tasks._reconcile), twice:
    the second pass sees nothing new and writes nothing."""
    account = _account(db)
    order = _order(db, account, order_id="OD338980309549917100")
    page = [ScrapedOrder(order_id="od338980309549917100 ", unit_id="u1", platform_status="Out For Delivery", delivery_otp="482713")]

    sync_tasks._reconcile(account.id, page, import_unknown=False)
    sync_tasks._reconcile(account.id, page, import_unknown=False)

    db.expire_all()
    assert _events(db) == [("out_for_delivery", None, "out_for_delivery")]
    event = db.scalar(select(UnitEvent))
    assert (event.order_pk, event.account_id) == (order.id, account.id)


def test_orders_an_import_pulls_in_are_not_news(db):
    """The account's own history, imported on request, arrives already delivered."""
    account = _account(db)
    page = [ScrapedOrder(order_id="OD111111111111111111", unit_id="h1", platform_status="Delivered", model="Old purchase")]

    sync_tasks._reconcile(account.id, page, import_unknown=True)

    assert db.scalar(select(Order.id).where(Order.order_id == "OD111111111111111111")) is not None
    assert _events(db) == []


def test_the_event_carries_the_read_backs_time_as_naive_utc(db):
    order = _order(db, _account(db))
    _read(db, order, {"unit_id": "u1", "platform_status": "Shipped"})
    ist = timezone(timedelta(hours=5, minutes=30))

    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"}, now=datetime(2026, 10, 26, 10, 0, tzinfo=ist))

    assert db.scalar(select(UnitEvent.at)) == datetime(2026, 10, 26, 4, 30)


# --- nothing else writes ------------------------------------------------------------------------
def _stale(db, order: Order) -> None:
    """Make the unit's stored reading disagree with its wording, as a row
    derived by older code does."""
    db.execute(update(OrderUnit).where(OrderUnit.id == order.units[0].id).values(delivery_state="unknown"))
    db.commit()


def test_the_start_up_re_derivation_writes_nothing(db):
    """An order the tool did not place, so its first reading writes nothing;
    its stored reading gone stale is rewritten at start-up — the code
    changing, not Flipkart — and that writes no event, nor does the next
    read-back of the same wording."""
    order = _order(db, _account(db), placed_by_run=False)
    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})
    assert _events(db) == []
    _stale(db, order)

    assert sync_unit_readings()["states"] >= 1
    assert _events(db) == []
    db.expire_all()
    order = db.get(Order, order.id)
    assert order.units[0].delivery_state == "delivered"
    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})

    assert _events(db) == []


def test_a_stale_stored_reading_never_looks_like_a_change(db):
    """The reading before a read-back comes from the wording, never the stored
    column: with the column stale and not yet re-derived, the same wording
    read again is no change."""
    order = _order(db, _account(db), placed_by_run=False)
    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})
    _stale(db, order)
    db.expire_all()
    order = db.get(Order, order.id)

    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})

    assert _events(db) == []


def test_deleting_an_order_takes_its_events_with_it(db):
    """Nothing deletes orders today; when something does, the events must not
    stand in its way (SQLite enforces the foreign keys here, as PostgreSQL does)."""
    order = _order(db, _account(db))
    _read(db, order, {"unit_id": "u1", "platform_status": "Delivered"})
    assert _events(db)

    db.delete(order)
    db.commit()

    assert _events(db) == []
    assert db.scalar(select(OrderUnit.id)) is None


# --- the window ----------------------------------------------------------------------------------
def test_the_window_without_a_mark_is_the_last_seven_days():
    now = datetime(2026, 10, 26, 12, 0)
    assert unit_events.window_start(None, now) == (now - timedelta(days=7), False)


def test_the_window_starts_at_the_mark():
    now = datetime(2026, 10, 26, 12, 0)
    mark = now - timedelta(hours=3)
    assert unit_events.window_start(mark, now) == (mark, False)


def test_the_window_never_reaches_back_more_than_thirty_days():
    now = datetime(2026, 10, 26, 12, 0)
    assert unit_events.window_start(now - timedelta(days=60), now) == (now - timedelta(days=30), True)


def test_the_window_reads_a_zoned_stamp_as_utc():
    now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    mark = datetime(2026, 10, 26, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert unit_events.window_start(mark, now) == (datetime(2026, 10, 26, 10, 0), False)
