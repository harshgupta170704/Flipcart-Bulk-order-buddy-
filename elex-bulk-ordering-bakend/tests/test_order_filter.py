"""The ledger's filter: one query behind the page, its count and its exports."""

from datetime import datetime, timedelta

from app.core import order_filter
from app.core.order_filter import OrderFilter, normalise_many
from app.db.models import Account, AccountStatus, Order, OrderStatus


def _account(db, email) -> Account:
    row = Account(email=email, status=AccountStatus.new)
    db.add(row)
    db.commit()
    return row


def _order(db, account, **fields) -> Order:
    values = {"status": OrderStatus.placed, **fields}
    row = Order(account_id=account.id, **values)
    db.add(row)
    db.commit()
    return row


def test_multi_value_inputs_are_split_trimmed_and_normalised():
    assert normalise_many(["OD1 od2, od3\nOD1"], upper=True) == ("OD1", "OD2", "OD3")
    assert normalise_many(["A@X.com", " b@y.com "], lower=True) == ("a@x.com", "b@y.com")
    assert normalise_many(None) == ()
    assert normalise_many(["", "  "]) == ()


def test_each_filter_narrows_and_the_count_matches_the_rows(db):
    first = _account(db, "first@test.com")
    second = _account(db, "second@test.com")
    base = datetime(2026, 9, 10, 6, 0)
    a = _order(db, first, model="vivo T5 Lite 44W 5G", order_id="od1", placed_at=base,
               platform_status="Delivered", tracking_id="TRK1")
    b = _order(db, first, model="vivo T5 Lite 44W 5G", order_id="OD2", placed_at=base + timedelta(days=20),
               platform_status="Out for delivery", tracking_id="TRK2")
    c = _order(db, second, model="HP Backpack", order_id="OD3", placed_at=base + timedelta(days=40),
               platform_status="Cancelled - Order shipped")
    d = _order(db, second, model="HP Backpack", status=OrderStatus.over_target, amount=1.0)

    def ids(flt):
        # Order ids: every order here has one unit, so a row is an order.
        return [order.id for _unit, order, _email, _units in order_filter.rows(db, flt)]

    assert order_filter.count(db, OrderFilter()) == 4
    # Product text, case-insensitive substring.
    assert ids(OrderFilter(q="vivo t5")) == [b.id, a.id]
    # Outcome (the tool's own status).
    assert ids(OrderFilter(statuses=(OrderStatus.over_target,))) == [d.id]
    # Exact platform wording and the portal's reading of it.
    assert ids(OrderFilter(platform_statuses=("Out for delivery",))) == [b.id]
    assert ids(OrderFilter(delivery_states=("cancelled",))) == [c.id]
    # "unknown" also finds rows whose status was never read — including one
    # from before the column existed, which holds NULL rather than "unknown".
    from sqlalchemy import text as sql_text

    db.execute(
        sql_text("UPDATE order_units SET delivery_state = NULL WHERE order_pk = :id"), {"id": d.id}
    )
    db.commit()
    assert d.id in ids(OrderFilter(delivery_states=("unknown",)))
    # Identifiers, several at once, normalised the way they are stored.
    assert ids(OrderFilter(order_ids=("OD1", "OD3"))) == [c.id, a.id]
    assert ids(OrderFilter(tracking_ids=("TRK2",))) == [b.id]
    # Email joins the account. (d was recorded just now and never placed, so by
    # the newest-first rule it sorts above c's October order date.)
    assert ids(OrderFilter(emails=("second@test.com",))) == [d.id, c.id]
    # Date range on the chosen field: inclusive start, exclusive end.
    flt = OrderFilter(date_from=base + timedelta(days=1), date_to=base + timedelta(days=40))
    assert ids(flt) == [b.id]
    assert order_filter.count(db, flt) == 1
    # Everything is an AND.
    assert ids(OrderFilter(q="vivo", delivery_states=("delivered",))) == [a.id]


def test_a_card_filter_with_no_digits_matches_nothing(db):
    """A non-blank card filter is a filter; one with no digits in it can match
    no card, so every reading of it — rows, count, export, compilation — is
    empty rather than unfiltered."""
    account = _account(db, "card@test.com")
    paid = _order(db, account, payment_mode="card", card_number="4111111111111111")
    _order(db, account, payment_mode="cod")

    letters = OrderFilter(card="abcd")
    assert letters.is_empty() is False
    assert order_filter.count(db, letters) == 0
    assert order_filter.rows(db, letters) == []
    assert list(order_filter.export_rows(db, letters)) == []
    assert order_filter.order_pks(db, letters) == []
    # Digits, whatever sits between them, still match.
    assert order_filter.order_pks(db, OrderFilter(card="4111-1111")) == [paid.id]


def test_newest_first_uses_the_order_date_and_falls_back_to_creation(db):
    account = _account(db, "sort@test.com")
    old = _order(db, account, placed_at=datetime(2026, 1, 1))
    never_placed = _order(db, account, status=OrderStatus.failed)  # placed_at NULL
    new = _order(db, account, placed_at=datetime(2026, 10, 1))

    order = [row.id for _unit, row, _email, _units in order_filter.rows(db, OrderFilter())]
    # The never-placed attempt was created just now, so it is newer than both
    # orders by date and sorts first; then by order date.
    assert order == [never_placed.id, new.id, old.id]


def test_page_and_total_are_the_same_filter(db):
    account = _account(db, "page@test.com")
    for i in range(7):
        _order(db, account, placed_at=datetime(2026, 1, 1 + i))
    pairs, total = order_filter.page(db, OrderFilter(), page_number=2, size=3)
    assert total == 7
    assert len(pairs) == 3
    pairs, total = order_filter.page(db, OrderFilter(), page_number=3, size=3)
    assert len(pairs) == 1


def test_options_group_the_platform_wordings_by_the_portals_reading(db):
    account = _account(db, "opts@test.com")
    _order(db, account, platform_status="Delivered")
    _order(db, account, platform_status="Delivered")
    _order(db, account, platform_status="Cancelled - Order shipped")
    _order(db, account, status=OrderStatus.failed)

    opts = order_filter.options(db)
    assert opts.total == 4
    assert {"value": "Delivered", "count": 2, "state": "delivered"} in opts.platform_statuses
    assert {"value": "Cancelled - Order shipped", "count": 1, "state": "cancelled"} in opts.platform_statuses
    assert {"value": "placed", "count": 3} in opts.statuses
    assert {"value": "unknown", "count": 1} in opts.delivery_states  # never read = unknown


def test_platform_and_bob_orders_filters(db):
    """Order Units History's Platform filter and BoB Orders toggle."""
    from app.db.models import Job, JobStatus, JobType

    owner = _account(db, "bob@test.com")
    job = Job(type=JobType.order, status=JobStatus.completed, payload={})
    db.add(job)
    db.commit()
    ours = _order(db, owner, model="Ours", order_id="OD10", job_id=job.id)
    read_back = _order(db, owner, model="Read back", order_id="OD11")

    def ids(flt):
        return sorted(order.id for _unit, order, _email, _units in order_filter.rows(db, flt))

    assert ids(OrderFilter(bob=True)) == [ours.id]
    assert ids(OrderFilter(bob=False)) == [read_back.id]
    assert ids(OrderFilter(platforms=("flipkart",))) == sorted([ours.id, read_back.id])
    assert ids(OrderFilter(platforms=("amazon",))) == []
    assert not OrderFilter(bob=False).is_empty()
