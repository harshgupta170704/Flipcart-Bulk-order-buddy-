"""One row per unit: the grain Flipkart reports on and the ledger shows.

``Order`` keeps what the tool intended and paid for; ``OrderUnit`` keeps what
the platform said about each piece. These tests pin the three writers (the
flush guard for any order row, the read-back's fold, the start-up backfill)
and that the ledger counts and addresses units.
"""

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from app.api import orders as orders_api
from app.core.order_units import apply_observations, representative_unit
from app.core.security import create_access_token
from app.db.base import sync_order_units
from app.db.models import Account, AccountStatus, Order, OrderStatus, OrderUnit


class _Seen:
    """A read-back observation, shaped like the scraper's ``ScrapedOrder``."""

    def __init__(self, **fields):
        self.order_id = None
        self.unit_id = None
        self.item_id = None
        self.platform_status = None
        self.delivery_date = None
        self.seller = None
        self.tracking_id = None
        self.invoice_url = None
        self.delivery_otp = None
        self.__dict__.update(fields)


@pytest.fixture
def account(db) -> Account:
    row = Account(email="units@test.com", status=AccountStatus.new)
    db.add(row)
    db.commit()
    return row


def _order(db, account, **fields) -> Order:
    row = Order(account_id=account.id, **{"status": OrderStatus.placed, **fields})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# --- creation -------------------------------------------------------------------
def test_an_order_gets_one_placeholder_per_requested_unit(db, account):
    order = _order(db, account, qty=3, order_id="OD1")
    assert [(u.position, u.unit_id, u.source) for u in order.units] == [
        (1, None, "run"),
        (2, None, "run"),
        (3, None, "run"),
    ]


def test_a_row_written_by_any_path_has_units(db, account):
    """The flush guard, not the writer, is what guarantees it."""
    row = Order(account_id=account.id, status=OrderStatus.failed)  # no qty at all
    db.add(row)
    db.commit()
    db.refresh(row)
    assert len(row.units) == 1


def test_the_first_unit_mirrors_what_the_order_row_already_knew(db, account):
    order = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        qty=2,
        unit_id="U-OLD",
        platform_status="Delivered",
        seller="Acme",
        synced_at=datetime(2026, 10, 1),
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    first, second = order.units
    assert (first.unit_id, first.platform_status, first.delivery_state, first.seller) == (
        "U-OLD",
        "Delivered",
        "delivered",
        "Acme",
    )
    assert (second.unit_id, second.platform_status) == (None, None)


# --- the read-back's fold -------------------------------------------------------
def test_observations_fill_placeholders_in_order_and_then_add(db, account):
    order = _order(db, account, qty=2, order_id="OD2")
    seen = [
        _Seen(order_id="OD2", unit_id="u1", platform_status="Shipped"),
        _Seen(order_id="OD2", unit_id="u2", platform_status="Delivered"),
        _Seen(order_id="OD2", unit_id="u3", platform_status="Delivered"),
    ]

    changes = apply_observations(db, order, seen)
    db.commit()
    db.refresh(order)

    assert (changes.filled, changes.created, changes.updated) == (2, 1, 3)
    assert [(u.position, u.unit_id, u.delivery_state, u.source) for u in order.units] == [
        (1, "U1", "in_transit", "run"),
        (2, "U2", "delivered", "run"),
        (3, "U3", "delivered", "sync"),
    ]
    assert all(u.synced_at is not None for u in order.units)
    # The same page again: nothing moves.
    assert not apply_observations(db, order, seen).changed


def test_the_gst_the_page_shows_is_kept_on_the_unit(db, account):
    """The owner's addition of 26 Oct 2026: the read-back keeps the GST the
    order page says the order was billed under, so the alerts can hold it
    against the GST the run chose. A page without the block changes nothing."""
    from app.automation.sync import ScrapedOrder

    order = _order(db, account, qty=1, order_id="OD-GST")
    seen = [ScrapedOrder(order_id="OD-GST", unit_id="g1", billed_gstin="09AAUFT2204P1ZI", billed_business_name="TRANSPARENT DEALS")]
    apply_observations(db, order, seen)
    db.commit()
    db.refresh(order)
    unit = order.units[0]
    assert (unit.billed_gstin, unit.billed_business_name) == ("09AAUFT2204P1ZI", "TRANSPARENT DEALS")

    # Read again from a page that shows no GST block: the values stand.
    assert not apply_observations(db, order, [ScrapedOrder(order_id="OD-GST", unit_id="g1")]).changed
    db.refresh(unit)
    assert unit.billed_gstin == "09AAUFT2204P1ZI"


def test_the_order_pages_timeline_is_kept_on_the_unit(db, account):
    """The owner's ask of 26 Oct 2026: the order's history, step by step, as
    the page shows it — kept whole, replaced when the page shows more."""
    from app.automation.sync import ScrapedOrder

    order = _order(db, account, qty=1, order_id="OD-TL")
    first = [
        {"status": "Order Confirmed", "date": "2026-10-18T00:00:00"},
        {"status": "Shipped", "date": None},
        {"status": "Delivered", "date": None},
    ]
    assert apply_observations(db, order, [ScrapedOrder(order_id="OD-TL", unit_id="t1", timeline=first)]).changed
    db.commit()
    db.refresh(order)
    assert order.units[0].timeline == first

    # The same page again: nothing moves. A later page with a dated step: replaced whole.
    assert not apply_observations(db, order, [ScrapedOrder(order_id="OD-TL", unit_id="t1", timeline=list(first))]).changed
    later = [{**step, "date": "2026-10-20T00:00:00"} if step["status"] == "Shipped" else step for step in first]
    assert apply_observations(db, order, [ScrapedOrder(order_id="OD-TL", unit_id="t1", timeline=later)]).changed
    db.commit()
    db.refresh(order.units[0])
    assert order.units[0].timeline[1] == {"status": "Shipped", "date": "2026-10-20T00:00:00"}


def test_a_unit_id_another_order_holds_is_never_claimed_twice(db, account):
    first = _order(db, account, qty=1, order_id="OD-DUP")
    second = _order(db, account, qty=1, order_id="OD-DUP")  # a duplicate record
    seen = [_Seen(order_id="OD-DUP", unit_id="SAME", platform_status="Delivered")]

    apply_observations(db, first, seen)
    apply_observations(db, second, seen)
    db.commit()
    db.refresh(first)
    db.refresh(second)

    assert first.units[0].unit_id == "SAME"
    # The status still lands on the duplicate; the id, which is unique, does not.
    assert second.units[0].unit_id is None
    assert second.units[0].platform_status == "Delivered"


def test_a_field_the_platform_did_not_show_never_blanks_one_we_hold(db, account):
    order = _order(db, account, qty=1, order_id="OD3")
    apply_observations(db, order, [_Seen(order_id="OD3", unit_id="u", tracking_id="TRK9")])

    changes = apply_observations(db, order, [_Seen(order_id="OD3", unit_id="u", platform_status="Shipped")])
    db.commit()
    db.refresh(order)

    assert changes.updated == 1
    assert (order.units[0].tracking_id, order.units[0].platform_status) == ("TRK9", "Shipped")


# --- the start-up backfill ------------------------------------------------------
def test_rows_older_than_the_table_are_backfilled_at_startup(db, account):
    """An order written before order_units existed (a raw insert here, which
    no flush guard sees) gets its units from init_db's sweep; a unit id
    another row already holds is left off rather than failing the start."""
    for order_id, qty in (("OD-OLD", 2), ("OD-OLD2", 1)):
        db.execute(
            insert(Order).values(
                account_id=account.id,
                status=OrderStatus.placed,
                qty=qty,
                unit_id="LEGACY",
                order_id=order_id,
                payment_mode="cod",
            )
        )
    db.commit()

    assert sync_order_units() == 2
    assert sync_order_units() == 0  # idempotent

    db.expire_all()
    first, second = db.scalars(select(Order).order_by(Order.id)).all()[-2:]
    assert [u.unit_id for u in first.units] == ["LEGACY", None]
    assert [u.unit_id for u in second.units] == [None]


# --- the unit that stands for the order -------------------------------------------
@pytest.mark.parametrize(
    ("units", "stands"),
    [
        # (Flipkart's wording, whether a read-back has shown the unit), by position.
        pytest.param([("Cancelled", True), ("Shipped", True)], 2, id="one-on-its-way-stands-for-the-order"),
        pytest.param(
            [("Delivered", True), ("Something Flipkart invents later", True), ("Shipped", True)],
            3,
            id="on-its-way-before-anything-else",
        ),
        pytest.param(
            [("Cancelled", True), ("Something Flipkart invents later", True), ("Delivered", True)],
            2,
            id="then-unfamiliar-wording",
        ),
        pytest.param([("Cancelled", True), ("Delivered", True)], 2, id="then-a-delivered-one"),
        pytest.param([("Cancelled", True), ("Refund Completed", True)], 1, id="then-the-first-by-position"),
        pytest.param([(None, False), ("Delivered", True)], 2, id="a-unit-never-shown-decides-nothing"),
        pytest.param([(None, False), (None, False)], 1, id="none-shown-yet-every-unit-counts"),
    ],
)
def test_the_unit_that_stands_for_the_order(units, stands):
    order = Order(status=OrderStatus.placed, order_id="OD338975799606600001")
    for position, (wording, shown) in enumerate(units, start=1):
        order.units.append(
            OrderUnit(
                platform="flipkart",
                position=position,
                platform_status=wording,
                synced_at=datetime(2026, 10, 27, 9, 0) if shown else None,
            )
        )
    assert representative_unit(order).position == stands


def test_an_order_with_no_units_has_none_to_stand_for_it():
    assert representative_unit(Order(status=OrderStatus.placed)) is None


# --- the ledger ------------------------------------------------------------------
@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_the_ledger_counts_units_and_addresses_the_order_for_settling(api, db, account):
    order = _order(db, account, qty=3, order_id="OD-LEDGER", amount=100.0)

    page = api.get("/orders").json()
    rows = page["items"]
    assert page["total"] == 3
    assert [r["unit_position"] for r in rows] == [1, 2, 3]
    assert {r["units_in_order"] for r in rows} == {3}
    assert {r["order_row_id"] for r in rows} == {order.id}
    assert len({r["id"] for r in rows}) == 3
    # The CSV says the same: three lines, the order's columns repeated.
    assert api.get("/orders/export.csv").text.count("OD-LEDGER") == 3


def test_settling_answers_with_the_orders_first_unit(api, db, account):
    order = _order(db, account, qty=1, status=OrderStatus.unconfirmed)
    out = api.post(f"/orders/{order.id}/settle", json={"status": "failed"}).json()
    assert (out["order_row_id"], out["unit_position"], out["status"]) == (order.id, 1, "failed")


# --- the delivery address snapshot (spec FR-044/045) ----------------------------
def test_a_changed_address_is_stamped_but_the_first_snapshot_is_not(db, account):
    order = _order(db, account, qty=1, order_id="OD5")
    first = _Seen(order_id="OD5", unit_id="u1", delivery_address="Work · A-39 · Noida 201307", delivery_pincode="201307")
    apply_observations(db, order, [first])
    db.commit()
    db.refresh(order)
    unit = order.units[0]
    assert (unit.delivery_address, unit.delivery_pincode) == ("Work · A-39 · Noida 201307", "201307")
    assert unit.address_changed_at is None  # the first reading is not a change

    apply_observations(db, order, [first])  # the same page again
    db.commit()
    db.refresh(order)
    assert order.units[0].address_changed_at is None

    moved = _Seen(order_id="OD5", unit_id="u1", delivery_address="Home · B-7 · Lucknow 226013", delivery_pincode="226013")
    apply_observations(db, order, [moved])
    db.commit()
    db.refresh(order)
    unit = order.units[0]
    assert unit.delivery_address == "Home · B-7 · Lucknow 226013"
    assert unit.address_changed_at is not None

    # A page that shows no address never blanks the one we hold.
    apply_observations(db, order, [_Seen(order_id="OD5", unit_id="u1", platform_status="Delivered")])
    db.commit()
    db.refresh(order)
    assert order.units[0].delivery_address == "Home · B-7 · Lucknow 226013"


def test_a_new_address_brings_its_own_pincode_even_when_none_could_be_read(db, account):
    """The pincode is read out of the address text. A new address whose
    pincode could not be read must not keep the old address's pincode
    standing beside it — the mismatch rule would compare the wrong one."""
    order = _order(db, account, qty=1, order_id="OD6")
    first = _Seen(order_id="OD6", unit_id="u1", delivery_address="Work · A-39 · Noida 201307", delivery_pincode="201307")
    moved = _Seen(order_id="OD6", unit_id="u1", delivery_address="Home · B-7 · Lucknow", delivery_pincode=None)
    apply_observations(db, order, [first])
    apply_observations(db, order, [moved])
    db.commit()
    db.refresh(order)

    unit = order.units[0]
    assert (unit.delivery_address, unit.delivery_pincode) == ("Home · B-7 · Lucknow", None)
    assert unit.address_changed_at is not None


def test_the_same_address_rendered_differently_is_not_a_change(db, account):
    """Punctuation, case and spacing are the page's, not the address's: a
    re-render raises no change alert, though the text kept is the latest."""
    order = _order(db, account, qty=1, order_id="OD7")
    first = _Seen(
        order_id="OD7", unit_id="u1", delivery_pincode="201307",
        delivery_address="Work · A-39, Nucleus Software Sector 62 · Noida 201307",
    )
    again = _Seen(
        order_id="OD7", unit_id="u1", delivery_pincode="201307",
        delivery_address="Work · A-39 Nucleus Software, Sector-62 · NOIDA 201307",
    )
    apply_observations(db, order, [first])
    changes = apply_observations(db, order, [again])
    db.commit()
    db.refresh(order)

    unit = order.units[0]
    assert unit.address_changed_at is None
    assert unit.delivery_address == "Work · A-39 Nucleus Software, Sector-62 · NOIDA 201307"
    assert changes.updated == 1  # the rendering moved; the address did not
