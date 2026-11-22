"""The promised and the actual delivery date kept apart, and how each unit was
paid (wave 3, package C, 26 Oct 2026).

``delivery_date`` holds the promise while a unit is on its way and the
read-back overwrote it with the delivery day, so "was it late?" had no answer.
The read-back now hands over ``promised_date``, ``delivered_date``,
``payment_method`` and ``amount_paid`` (app.automation.sync, read live), and the
unit keeps all four: the ledger's API, its CSV and the order dialog show them.
The first promise is kept apart too (``first_promised_date``, revision 0007):
Flipkart moves its promise when a delivery slips, and the lateness is counted
from the day first promised.
"""

from __future__ import annotations

import csv
import importlib.util
import io
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from app.api import order_details
from app.api import orders as orders_api
from app.automation.sync import ScrapedOrder
from app.core.order_status import days_late
from app.core.order_units import PAYMENT_METHOD_MAX, apply_observations, amount_paid_value, payment_method_text
from app.core.security import create_access_token
from app.db import migrations
from app.db.models import Account, AccountStatus, Order, OrderStatus
from app.io import orders_csv
from app.schemas.order import OrderOut

NOV_01 = datetime(2026, 11, 1)
NOV_03 = datetime(2026, 11, 3)
NOV_04 = datetime(2026, 11, 4)
NOV_05 = datetime(2026, 11, 5)


@pytest.fixture
def account(db) -> Account:
    row = Account(email="dates@test.com", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    # The Order Details API, which writes the same two days in its own format.
    application.include_router(order_details.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def _order(db, account: Account, order_id: str = "OD338967210239333300") -> Order:
    row = Order(account_id=account.id, status=OrderStatus.placed, qty=1, model="vivo T5 Lite 44W 5G", order_id=order_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _read(db, order: Order, **page) -> None:
    """One read-back of the order's one unit, as the scraper hands it over."""
    apply_observations(db, order, [ScrapedOrder(order_id=order.order_id, unit_id="u1", **page)])
    db.commit()
    db.refresh(order)


# --- what the unit keeps ---------------------------------------------------------------
def test_a_promise_is_kept_through_delivery_and_the_lateness_follows(db, account) -> None:
    order = _order(db, account)
    _read(db, order, platform_status="Delivery expected by Nov 01", delivery_date=NOV_01, promised_date=NOV_01)
    # Delivered two days late: the delivered page shows no promise any more.
    _read(db, order, platform_status="Delivered", delivery_date=NOV_03, delivered_date=NOV_03)

    unit = order.units[0]
    assert (unit.promised_date, unit.delivered_date, unit.delivery_date) == (NOV_01, NOV_03, NOV_03)
    # One promise, never moved: the first promise is that one.
    assert unit.first_promised_date == NOV_01
    out = OrderOut.from_order(order)
    assert (out.promised_date, out.first_promised_date, out.delivered_date, out.days_late) == (NOV_01, NOV_01, NOV_03, 2)


def test_a_later_promise_replaces_an_earlier_one(db, account) -> None:
    order = _order(db, account)
    _read(db, order, platform_status="Delivery expected by Nov 01", promised_date=NOV_01)
    _read(db, order, platform_status="Delivery expected by Nov 04", promised_date=NOV_04)

    assert order.units[0].promised_date == NOV_04
    # …while the first is kept apart, written once.
    assert order.units[0].first_promised_date == NOV_01


@pytest.mark.parametrize(
    ("moved_to", "delivered", "late"),
    [
        (NOV_04, NOV_05, 4),  # moved, then late even against the moved day: counted from the first
        (NOV_05, NOV_05, 4),  # delivered on the moved day: late by the move, not "on time"
    ],
)
def test_a_moved_promise_counts_lateness_from_the_first(db, account, api, moved_to, delivered, late) -> None:
    """Flipkart moves its promise when a delivery slips, and the read-back
    follows it; the lateness is counted from the day first promised, in the
    API, the CSV's Estimated Delivery beside it and its Days Late."""
    order = _order(db, account)
    _read(db, order, platform_status="Delivery expected by Nov 01", delivery_date=NOV_01, promised_date=NOV_01)
    _read(db, order, platform_status=f"Delivery expected by Nov {moved_to.day:02d}", delivery_date=moved_to, promised_date=moved_to)
    _read(db, order, platform_status="Delivered", delivery_date=delivered, delivered_date=delivered)

    unit = order.units[0]
    assert (unit.first_promised_date, unit.promised_date, unit.delivered_date) == (NOV_01, moved_to, delivered)
    out = OrderOut.from_order(order)
    assert (out.first_promised_date, out.promised_date, out.days_late) == (NOV_01, moved_to, late)
    row = _csv(api)["OD338967210239333300"]
    assert (row["Estimated Delivery"], row["Actual Delivery Date"], row["Days Late"]) == (
        "01-11-2026",
        delivered.strftime("%d-%m-%Y"),
        str(late),
    )


def test_the_first_promise_is_kept_however_often_the_promise_moves(db, account, api) -> None:
    """Moved twice (Nov 01, then 04, then 05) and delivered on Nov 06: five
    days late against the day first promised — not two, against the second."""
    order = _order(db, account)
    for day in (NOV_01, NOV_04, NOV_05):
        _read(db, order, platform_status=f"Delivery expected by Nov {day.day:02d}", delivery_date=day, promised_date=day)
    nov_06 = datetime(2026, 11, 6)
    _read(db, order, platform_status="Delivered", delivery_date=nov_06, delivered_date=nov_06)

    unit = order.units[0]
    assert (unit.first_promised_date, unit.promised_date) == (NOV_01, NOV_05)
    assert OrderOut.from_order(order).days_late == 5
    assert _csv(api)["OD338967210239333300"]["Days Late"] == "5"


def test_reading_the_same_promise_again_changes_nothing(db, account) -> None:
    """The first promise is written once: a read-back that finds the same page
    again finds nothing to update."""
    order = _order(db, account)
    page = [ScrapedOrder(order_id=order.order_id, unit_id="u1", platform_status="Delivery expected by Nov 01", promised_date=NOV_01)]

    first = apply_observations(db, order, page)
    db.commit()
    again = apply_observations(db, order, page)
    db.commit()

    assert (first.updated, again.updated) == (1, 0)
    assert order.units[0].first_promised_date == NOV_01


def test_a_unit_on_its_way_shows_the_day_it_is_now_promised_for(db, account, api) -> None:
    """Until it is delivered, the date to expect is the latest promise: the
    CSV's Estimated Delivery and the owner's order-data sheet show the moved
    day, while the first promise waits for the lateness."""
    order = _order(db, account)
    _read(db, order, platform_status="Delivery expected by Nov 01", delivery_date=NOV_01, promised_date=NOV_01)
    _read(db, order, platform_status="Delivery expected by Nov 05", delivery_date=NOV_05, promised_date=NOV_05)

    assert _csv(api)["OD338967210239333300"]["Estimated Delivery"] == "05-11-2026"
    response = api.get("/orders/export.csv", params={"sheet": "order_data"})
    (sheet_row,) = csv.DictReader(io.StringIO(response.text.lstrip("﻿")))
    assert sheet_row["Estimated Delivery Date"] == "05/11/26"
    assert OrderOut.from_order(order).first_promised_date == NOV_01


def test_a_unit_read_before_the_first_promise_was_kept_keeps_its_stored_one(db, account) -> None:
    """A row read back before ``first_promised_date`` existed holds its promise
    in ``promised_date`` alone. When the next read brings a later promise, the
    stored one — the earlier — becomes the first, not the new one."""
    order = _order(db, account)
    order.units[0].platform_status = "Delivery expected by Nov 01"
    order.units[0].promised_date = NOV_01  # as the read-back left it, before this column
    db.commit()
    assert OrderOut.from_order(order).first_promised_date is None

    _read(db, order, platform_status="Delivery expected by Nov 05", promised_date=NOV_05)
    _read(db, order, platform_status="Delivered", delivery_date=NOV_05, delivered_date=NOV_05)

    unit = order.units[0]
    assert (unit.first_promised_date, unit.promised_date) == (NOV_01, NOV_05)
    assert OrderOut.from_order(order).days_late == 4


def test_a_page_that_does_not_show_them_keeps_what_was_read(db, account) -> None:
    order = _order(db, account)
    _read(db, order, platform_status="Shipped", promised_date=NOV_01, payment_method="Cash On Delivery", amount_paid=235.0)
    _read(db, order, platform_status="Out For Delivery")

    unit = order.units[0]
    assert (unit.promised_date, unit.payment_method, unit.amount_paid) == (NOV_01, "Cash On Delivery", 235.0)


def test_how_it_was_paid_is_tidied_to_the_column(db, account) -> None:
    order = _order(db, account)
    _read(db, order, payment_method="  Credit  card\n ending 4242 " + "x" * 200, amount_paid="235.50")

    unit = order.units[0]
    assert unit.payment_method.startswith("Credit card ending 4242 x")
    assert len(unit.payment_method) == PAYMENT_METHOD_MAX
    assert unit.amount_paid == 235.5
    assert payment_method_text("   ") is None and payment_method_text(None) is None
    for nonsense in (-1, float("nan"), float("inf"), "abc", True):
        assert amount_paid_value(nonsense) is None


def test_a_cancelled_unit_gains_nothing_and_shows_no_dates(db, account, api) -> None:
    """Cancelled before delivery: no delivery, no lateness — and a promise read
    while it was on its way is not a date the CSV calls an estimate."""
    order = _order(db, account)
    _read(db, order, platform_status="Delivery expected by Nov 01", promised_date=NOV_01)
    _read(db, order, platform_status="Cancelled", delivery_date=datetime(2026, 10, 30))

    unit = order.units[0]
    assert (unit.promised_date, unit.delivered_date) == (NOV_01, None)
    assert OrderOut.from_order(order).days_late is None
    row = _csv(api)["OD338967210239333300"]
    assert (row["Estimated Delivery"], row["Actual Delivery Date"], row["Days Late"]) == ("", "", "")


def _three_surfaces(api, order: Order) -> dict:
    """The promise, the delivery day and how late, as the ledger page, the CSV
    and the Order Details API each give them for the order's one unit."""
    (item,) = [i for i in api.get("/orders").json()["items"] if i["order_id"] == order.order_id]
    row = _csv(api)[order.order_id]
    data = api.post("/orders/orderDetails", json={"orderId": order.order_id}).json()["data"]
    (detail,) = data["items"]
    return {
        "page": (item["first_promised_date"], item["delivered_date"], item["days_late"]),
        "csv": (row["Estimated Delivery"], row["Actual Delivery Date"], row["Days Late"]),
        "api": (
            [(s["estimatedDelivery"], s["deliveredOn"]) for s in data["shipments"]],
            detail["deliveryDate"],
        ),
    }


@pytest.mark.parametrize("sent_back", ["Return Requested", "Return Cancelled"])
def test_a_unit_delivered_and_then_sent_back_keeps_its_delivery_day_everywhere(db, account, api, sent_back) -> None:
    """Promised Nov 01, delivered Nov 03, then a return — asked for, or asked
    for and withdrawn ("Return Cancelled": the customer kept it). The ledger
    page kept the delivery day and the two days late; the CSV and the Order
    Details API dropped both (review of 27 Oct 2026). ``delivered_date`` is
    only ever written from a delivery, so all three now say the same. The
    return's own date, which the row may carry, is no delivery day."""
    order = _order(db, account)
    _read(db, order, platform_status="Delivery expected by Nov 01", delivery_date=NOV_01, promised_date=NOV_01)
    _read(db, order, platform_status="Delivered", delivery_date=NOV_03, delivered_date=NOV_03)
    _read(db, order, platform_status=sent_back, delivery_date=datetime(2026, 11, 6))
    assert order.units[0].delivery_state == "returned"

    assert _three_surfaces(api, order) == {
        "page": ("2026-11-01T00:00:00", "2026-11-03T00:00:00", 2),
        "csv": ("01-11-2026", "03-11-2026", "2"),
        "api": ([("2026-11-01", "2026-11-03")], "2026-11-03"),
    }

    # A promise that moved before the delivery: the estimate is the FIRST
    # promise everywhere, and the lateness is counted from it (review of
    # 27 Oct 2026: the branch for a returned unit must not take the latest).
    moved = _order(db, account, "OD338967210239333302")
    for page in (
        {"platform_status": "Delivery expected by Nov 01", "delivery_date": NOV_01, "promised_date": NOV_01},
        {"platform_status": "Delivery expected by Nov 04", "delivery_date": datetime(2026, 11, 4), "promised_date": datetime(2026, 11, 4)},
        {"platform_status": "Delivered", "delivery_date": datetime(2026, 11, 5), "delivered_date": datetime(2026, 11, 5)},
        {"platform_status": sent_back, "delivery_date": datetime(2026, 11, 8)},
    ):
        apply_observations(db, moved, [ScrapedOrder(order_id=moved.order_id, unit_id="u3", **page)])
        db.commit()
    surfaces = _three_surfaces(api, moved)
    assert surfaces["csv"] == ("01-11-2026", "05-11-2026", "4")
    assert surfaces["api"] == ([("2026-11-01", "2026-11-05")], "2026-11-05")
    assert surfaces["page"] == ("2026-11-01T00:00:00", "2026-11-05T00:00:00", 4)

    # A unit cancelled on its way was never delivered: none of the three gives
    # it a delivery day or a lateness. (The page keeps the promise it read; the
    # CSV and the API give no estimate for a unit that will not come, as before.)
    cancelled = _order(db, account, "OD338967210239333301")
    for page in (
        {"platform_status": "Delivery expected by Nov 01", "promised_date": NOV_01},
        {"platform_status": "Cancelled", "delivery_date": datetime(2026, 10, 30)},
    ):
        apply_observations(db, cancelled, [ScrapedOrder(order_id=cancelled.order_id, unit_id="u2", **page)])
        db.commit()
    assert _three_surfaces(api, cancelled) == {"page": ("2026-11-01T00:00:00", None, None), "csv": ("", "", ""), "api": ([], None)}


# --- how late -------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("promised", "delivered", "late"),
    [
        (NOV_01, NOV_03, 2),
        (NOV_01, datetime(2026, 11, 1, 21, 30), 0),  # the promised day, late in the evening: on time
        (NOV_03, NOV_01, 0),  # early is on time
        (date(2026, 11, 1), date(2026, 11, 2), 1),
        (None, NOV_03, None),
        (NOV_01, None, None),
    ],
)
def test_days_late_is_counted_by_calendar_day(promised, delivered, late) -> None:
    assert days_late(promised, delivered) == late


# --- the ledger's API and CSV -------------------------------------------------------------
def _csv(api) -> dict[str, dict]:
    response = api.get("/orders/export.csv")
    assert response.status_code == 200
    body = response.text.lstrip("﻿")
    header = next(csv.reader(io.StringIO(body)))
    assert header == orders_csv.HEADERS
    # The page's "Total amount": paid, or to be paid on delivery, or never
    # collected — so not called "Amount Paid" (its name until 26 Oct 2026, in
    # the same place, so no column moves).
    assert header[-3:] == ["Paid By", "Order Total (Flipkart)", "Days Late"]
    return {row["Order ID"]: row for row in csv.DictReader(io.StringIO(body))}


def test_the_ledger_rows_and_the_csv_carry_them(db, account, api) -> None:
    order = _order(db, account)
    _read(db, order, platform_status="Delivery expected by Nov 01", promised_date=NOV_01)
    _read(
        db,
        order,
        platform_status="Delivered",
        delivery_date=NOV_03,
        delivered_date=NOV_03,
        payment_method="Cash On Delivery",
        amount_paid=235.0,
    )

    (item,) = api.get("/orders").json()["items"]
    assert (item["promised_date"], item["delivered_date"], item["days_late"]) == (
        "2026-11-01T00:00:00",
        "2026-11-03T00:00:00",
        2,
    )
    assert (item["payment_method"], item["amount_paid"]) == ("Cash On Delivery", 235.0)

    row = _csv(api)["OD338967210239333300"]
    assert (row["Estimated Delivery"], row["Actual Delivery Date"]) == ("01-11-2026", "03-11-2026")
    assert (row["Paid By"], row["Order Total (Flipkart)"], row["Days Late"]) == ("Cash On Delivery", "235.00", "2")


def test_a_row_read_back_before_the_dates_were_apart_still_shows_its_one_date(db, account, api) -> None:
    in_transit = _order(db, account, "OD338967210239333310")
    in_transit.units[0].platform_status = "Shipped"
    in_transit.units[0].delivery_date = NOV_01
    delivered = _order(db, account, "OD338967210239333311")
    delivered.units[0].platform_status = "Delivered"
    delivered.units[0].delivery_date = NOV_03
    db.commit()

    rows = _csv(api)
    assert (rows["OD338967210239333310"]["Estimated Delivery"], rows["OD338967210239333310"]["Actual Delivery Date"]) == (
        "01-11-2026",
        "",
    )
    assert (rows["OD338967210239333311"]["Estimated Delivery"], rows["OD338967210239333311"]["Actual Delivery Date"]) == (
        "",
        "03-11-2026",
    )
    # No promise known: the lateness is not guessed.
    assert rows["OD338967210239333311"]["Days Late"] == ""


def test_the_payment_method_is_kept_as_text_in_the_csv(db, account, api) -> None:
    order = _order(db, account)
    _read(db, order, payment_method='=HYPERLINK("http://x.test","pay")')
    assert _csv(api)["OD338967210239333300"]["Paid By"] == ' =HYPERLINK("http://x.test","pay")'


# --- the migration -----------------------------------------------------------------------
def _alembic_installed() -> bool:
    try:
        return importlib.util.find_spec("alembic.command") is not None
    except ImportError:
        return False


needs_alembic = pytest.mark.skipif(not _alembic_installed(), reason="alembic is not installed")
NEW_COLUMNS = {"promised_date", "delivered_date", "payment_method", "amount_paid"}


def _alembic(engine, name: str, revision: str) -> None:
    from alembic import command

    with engine.begin() as connection:
        getattr(command, name)(migrations.alembic_config(connection), revision)


def _unit_columns(engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("order_units")}


@needs_alembic
def test_revision_0006_adds_the_four_columns_and_takes_them_away(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", poolclass=NullPool)
    try:
        _alembic(engine, "upgrade", "0005")
        assert not NEW_COLUMNS & _unit_columns(engine)
        _alembic(engine, "upgrade", "0006")
        assert NEW_COLUMNS <= _unit_columns(engine)
        widths = {c["name"]: getattr(c["type"], "length", None) for c in inspect(engine).get_columns("order_units")}
        assert widths["payment_method"] == 100
        _alembic(engine, "downgrade", "0005")
        assert not NEW_COLUMNS & _unit_columns(engine)
    finally:
        engine.dispose()


@needs_alembic
def test_revision_0006_leaves_columns_init_db_already_added(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", poolclass=NullPool)
    try:
        _alembic(engine, "upgrade", "0005")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE order_units ADD COLUMN promised_date DATETIME"))
            conn.execute(text("ALTER TABLE order_units ADD COLUMN payment_method VARCHAR(100)"))
        _alembic(engine, "upgrade", "0006")
        assert NEW_COLUMNS <= _unit_columns(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0006"
    finally:
        engine.dispose()


@needs_alembic
def test_revision_0006_does_nothing_where_there_is_no_order_units_table(tmp_path) -> None:
    from alembic import command

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", poolclass=NullPool)
    try:
        with engine.begin() as connection:
            command.stamp(migrations.alembic_config(connection), "0005")
        _alembic(engine, "upgrade", "0006")
        _alembic(engine, "downgrade", "0005")
        assert "order_units" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


# --- revision 0007: the first promise ------------------------------------------------------
FIRST_PROMISE = "first_promised_date"
STORED_NOV_05 = "2026-11-05 00:00:00.000000"
STORED_NOV_03 = "2026-11-03 00:00:00.000000"
STORED_NOV_01 = "2026-11-01 00:00:00.000000"


def _insert_units(engine) -> None:
    """One order's three units as revision 0006 left them: two read with a
    promise (the latest, the only one 0006 kept), one never promised."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (id, email, status, created_at, updated_at) "
                "VALUES (1, 'promise@test.com', 'new', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(text("INSERT INTO orders (id, account_id, payment_mode, status) VALUES (1, 1, 'cod', 'placed')"))
        conn.execute(
            text(
                "INSERT INTO order_units (id, order_pk, platform, position, source, promised_date, created_at, updated_at) "
                "VALUES (1, 1, 'flipkart', 1, 'run', :nov_05, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "(2, 1, 'flipkart', 2, 'run', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "(3, 1, 'flipkart', 3, 'run', :nov_03, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"nov_05": STORED_NOV_05, "nov_03": STORED_NOV_03},
        )


def _first_promises(engine) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text(f"SELECT id, {FIRST_PROMISE} FROM order_units ORDER BY id"))]


@needs_alembic
def test_revision_0007_adds_the_first_promise_filled_from_the_promise_and_takes_it_away(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", poolclass=NullPool)
    try:
        _alembic(engine, "upgrade", "0006")
        _insert_units(engine)
        _alembic(engine, "upgrade", "0007")
        assert FIRST_PROMISE in _unit_columns(engine)
        # The best there is for a row read before: the promise it holds.
        assert _first_promises(engine) == [(1, STORED_NOV_05), (2, None), (3, STORED_NOV_03)]
        _alembic(engine, "downgrade", "0006")
        assert FIRST_PROMISE not in _unit_columns(engine)
    finally:
        engine.dispose()


@needs_alembic
def test_revision_0007_fills_the_column_init_db_already_added_and_keeps_a_first_promise(tmp_path) -> None:
    """``init_db`` adds the column at start-up, empty, before Alembic runs; a
    read-back may then have written a unit's first promise. The revision adds
    nothing again, fills the empty ones and leaves a first promise as it is."""
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", poolclass=NullPool)
    try:
        _alembic(engine, "upgrade", "0006")
        _insert_units(engine)
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE order_units ADD COLUMN {FIRST_PROMISE} DATETIME"))
            conn.execute(text(f"UPDATE order_units SET {FIRST_PROMISE} = :nov_01 WHERE id = 1"), {"nov_01": STORED_NOV_01})
        _alembic(engine, "upgrade", "0007")
        assert _first_promises(engine) == [(1, STORED_NOV_01), (2, None), (3, STORED_NOV_03)]
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0007"
    finally:
        engine.dispose()


@needs_alembic
def test_revision_0007_does_nothing_where_there_is_no_order_units_table(tmp_path) -> None:
    from alembic import command

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", poolclass=NullPool)
    try:
        with engine.begin() as connection:
            command.stamp(migrations.alembic_config(connection), "0006")
        _alembic(engine, "upgrade", "0007")
        _alembic(engine, "downgrade", "0006")
        assert "order_units" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
