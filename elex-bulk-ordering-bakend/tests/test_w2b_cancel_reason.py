"""Why Flipkart cancelled an order, and its cancellations told apart from the
tool's own failures (wave 2, package B, 26 Oct 2026).

The read-back keeps the line Flipkart's order page prints under "Cancelled,
<date>" on the unit row (``OrderUnit.cancel_reason``). A failed order that
carries Flipkart's order id is one Flipkart took and then cancelled; the
ledger's API, the dashboard's counters, the alert and the CSV now say so,
instead of the red "Failed" that means a run stopped before anything reached
Flipkart. Live wording from 24–25 Oct 2026; GSTINs and ids are made up.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import importlib.util
import io
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.pool import NullPool

from app.api import orders as orders_api
from app.api import stats as stats_api
from app.core import alerts, cancellations
from app.core.order_status import cancelled_by_user
from app.core.order_units import CANCEL_REASON_MAX, apply_observations, cancel_reason_text
from app.core.security import create_access_token
from app.db import migrations
from app.db.models import (
    Account,
    AccountStatus,
    Alert,
    AlertSeverity,
    Job,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    OrderUnit,
    Session as SessionModel,
    UnitRequest,
)
from app.io import orders_csv
from app.schemas.order import OrderOut
from tests.fakes import FakeContext, FakePage

#: Flipkart's own reason, read back live on 24 and 25 Oct 2026.
SELLER_POLICY = "Sorry, your order was cancelled as per seller policy restrictions applicable to this brand."
#: How an order page words a cancellation the account asked for.
USER_ASKED = "You requested a cancellation because you changed your mind about this product."


class _Seen:
    """A read-back observation carrying a cancellation reason, shaped like the
    scraper's ``ScrapedOrder``: every field optional, read with ``getattr``."""

    def __init__(self, **fields):
        self.order_id = None
        self.unit_id = None
        self.item_id = None
        self.platform_status = None
        self.cancel_reason = None
        self.__dict__.update(fields)


@pytest.fixture
def account(db) -> Account:
    row = Account(email="cancel@test.com", status=AccountStatus.new)
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    application.include_router(stats_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def _order(db, account: Account, **fields) -> Order:
    row = Order(account_id=account.id, **{"status": OrderStatus.placed, "qty": 1, "model": "vivo T5 Lite 44W 5G", **fields})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _cancelled(db, account: Account, order_id: str, *, reason: str | None, status: str = "Cancelled") -> Order:
    """An order Flipkart took (it has an order id) whose one unit the read-back
    saw cancelled — the order marked failed, as the read-back marks it."""
    order = _order(db, account, order_id=order_id, status=OrderStatus.failed)
    unit = order.units[0]
    unit.platform_status = status
    unit.cancel_reason = reason
    unit.synced_at = datetime(2026, 10, 25, 9, 0)
    db.commit()
    db.refresh(order)
    return order


# --- who cancelled, from the platform's own words --------------------------------
@pytest.mark.parametrize(
    "wordings",
    [
        ("Cancelled by user - Order shipped", None),
        ("Cancelled by user - Order confirmed but not shipped", None),
        ("Cancelled", USER_ASKED),
        ("Cancelled", "Your order has been cancelled as per your request."),
        ("CANCELLED BY USER", None),
    ],
)
def test_the_spec_and_page_wordings_for_a_users_own_cancellation_are_read_as_such(wordings) -> None:
    assert cancelled_by_user(*wordings)


@pytest.mark.parametrize(
    "wordings",
    [
        ("Cancelled", SELLER_POLICY),  # Flipkart's own, live 24–25 Oct 2026
        ("Cancelled - Order shipped", None),
        ("Cancelled - Order confirmed but not shipped", None),
        ("Cancelled", None),
        (None, None),
        ("", "  "),
    ],
)
def test_flipkarts_own_cancellation_and_blank_wording_are_not_the_users(wordings) -> None:
    assert not cancelled_by_user(*wordings)


# --- storage: the unit keeps the reason -------------------------------------------
def test_the_reason_lands_on_the_unit_and_a_page_without_one_keeps_it(db, account) -> None:
    order = _order(db, account, order_id="OD338967210239333100")
    seen = [_Seen(order_id="OD338967210239333100", unit_id="u1", platform_status="Cancelled", cancel_reason=SELLER_POLICY)]

    changes = apply_observations(db, order, seen)
    db.commit()
    db.refresh(order)
    assert changes.updated == 1
    assert order.units[0].cancel_reason == SELLER_POLICY

    # A later look that read no reason never blanks the one we hold.
    again = [_Seen(order_id="OD338967210239333100", unit_id="u1", platform_status="Cancelled")]
    assert not apply_observations(db, order, again).changed
    # The same sentence laid out differently is the same reason: nothing moves.
    spaced = "  " + "  \n ".join(SELLER_POLICY.split(" ")) + "\t "
    assert spaced != SELLER_POLICY and cancel_reason_text(spaced) == SELLER_POLICY
    assert not apply_observations(db, order, [_Seen(order_id="OD338967210239333100", unit_id="u1", cancel_reason=spaced)]).changed
    db.commit()
    db.refresh(order)
    assert order.units[0].cancel_reason == SELLER_POLICY


def test_a_reason_longer_than_the_column_is_cut_to_it(db, account) -> None:
    order = _order(db, account, order_id="OD338967210239333101")
    long_reason = "Sorry, your order was cancelled because " + "of a very long explanation " * 20
    apply_observations(db, order, [_Seen(order_id="OD338967210239333101", unit_id="u1", cancel_reason=long_reason)])
    db.commit()
    db.expire_all()

    stored = db.get(Order, order.id).units[0].cancel_reason
    assert len(stored) <= CANCEL_REASON_MAX == 255
    assert stored == long_reason[:255].rstrip()
    assert cancel_reason_text("   ") is None and cancel_reason_text(None) is None


def test_the_read_back_stores_the_reason_and_the_alert_says_flipkart_cancelled(db, account, monkeypatch) -> None:
    """End to end through the read-back's own run, with the browser and the
    scraper swapped out as the job's tests do it: the reason arrives as the
    scraper hands it over (padded), is stored trimmed, and the alert raised at
    the end of the run gives it."""
    from app.jobs import sync_tasks

    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": "x"}], valid=True))
    order = _order(db, account, order_id="OD338967210239333102")
    job = Job(type=JobType.sync, status=JobStatus.queued)
    db.add(job)
    db.commit()

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def fake_scrape_orders(page, *, limit=20, open_details=True, should_stop=None):
        return [
            _Seen(
                order_id="OD338967210239333102",
                unit_id="u1",
                platform_status="Cancelled",
                cancel_reason=f"  {SELLER_POLICY}  ",
            )
        ]

    monkeypatch.setattr(sync_tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(sync_tasks, "scrape_orders", fake_scrape_orders)
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    db.expire_all()
    row = db.get(Order, order.id)
    assert row.status == OrderStatus.failed and row.order_id == "OD338967210239333102"
    assert row.units[0].cancel_reason == SELLER_POLICY
    alert = db.scalar(select(Alert).where(Alert.key == f"order_failed:order:{order.id}"))
    assert alert is not None and not alert.resolved
    assert alert.message == (
        f"Order OD338967210239333102 (vivo T5 Lite 44W 5G) for cancel@test.com was cancelled by Flipkart: {SELLER_POLICY}"
    )


# --- the API ------------------------------------------------------------------------
def test_the_ledger_rows_carry_the_reason_and_who_cancelled(db, account, api) -> None:
    never_sent = _order(db, account, status=OrderStatus.failed, model="HP Backpack")
    by_flipkart = _cancelled(db, account, "OD338967210239333110", reason=SELLER_POLICY)
    by_user = _cancelled(db, account, "OD338967210239333111", reason=USER_ASKED)
    by_user_status = _cancelled(db, account, "OD338967210239333112", reason=None, status="Cancelled by user - Order shipped")
    placed = _order(db, account, order_id="OD338967210239333113")

    items = api.get("/orders").json()["items"]
    by_row = {item["order_row_id"]: item for item in items}
    assert {key: by_row[by_flipkart.id][key] for key in ("status", "cancel_reason", "cancelled_by")} == {
        "status": "failed",
        "cancel_reason": SELLER_POLICY,
        "cancelled_by": "flipkart",
    }
    assert (by_row[by_user.id]["cancelled_by"], by_row[by_user.id]["cancel_reason"]) == ("user", USER_ASKED)
    assert by_row[by_user_status.id]["cancelled_by"] == "user"
    # The run's own failure, before anything reached Flipkart: no one cancelled it.
    assert (by_row[never_sent.id]["cancelled_by"], by_row[never_sent.id]["cancel_reason"]) == (None, None)
    assert by_row[placed.id]["cancelled_by"] is None


def test_from_order_carries_them_too(db, account) -> None:
    order = _cancelled(db, account, "OD338967210239333120", reason=SELLER_POLICY)
    out = OrderOut.from_order(order, "cancel@test.com")
    assert (out.cancel_reason, out.cancelled_by, out.status) == (SELLER_POLICY, "flipkart", OrderStatus.failed)
    # The order page's timeline still reaches the row as steps.
    order.units[0].timeline = [{"status": "Order Received", "date": "2026-10-24T00:00:00"}]
    db.commit()
    assert OrderOut.from_order(order).timeline == [{"status": "Order Received", "date": "2026-10-24T00:00:00"}]


def test_a_unit_flipkart_never_listed_takes_its_orders_verdict(db, account, api) -> None:
    """Two units requested, one listed and cancelled at the user's request: the
    order is failed, and the unit the platform never showed carries no wording
    and no request — it says nothing of who cancelled. Read off the unit alone
    that would be Flipkart; the ledger gives it its order's verdict, the
    account's, which is what the dashboard and the alert say (review of
    26 Oct 2026)."""
    order = _order(db, account, order_id="OD338967210239333121", qty=2, status=OrderStatus.failed)
    first, second = order.units
    first.platform_status, first.cancel_reason = "Cancelled", USER_ASKED
    db.commit()
    assert cancellations.cancelled_by(order, first) == cancellations.BY_USER
    assert cancellations.says_nothing(second) and not cancellations.says_nothing(first)
    reading = cancellations.cancel_readings(db, [order.id])[order.id]
    assert cancellations.cancelled_by(order, second, reading=reading) == cancellations.BY_USER
    # A unit that does say something keeps its own word, whatever the order's.
    flipkarts = cancellations.CancelReading(by_platform=1)
    assert cancellations.cancelled_by(order, first, reading=flipkarts) == cancellations.BY_USER

    rows = {item["unit_position"]: item["cancelled_by"] for item in api.get("/orders").json()["items"]}
    assert rows == {1: "user", 2: "user"}


def _read_back(account: Account, *seen: _Seen) -> None:
    """One read-back of the account's recent orders, through the read-back's
    own fold (app.jobs.sync_tasks._reconcile) as a timed read-back runs it."""
    from app.jobs import sync_tasks

    sync_tasks._reconcile(account.id, list(seen), import_unknown=False)


def _export_lines(api, path: str, **params) -> list[dict]:
    response = api.get(path, params=params)
    assert response.status_code == 200
    return list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))


def _workbook_lines(api, **params) -> list[dict]:
    from openpyxl import load_workbook

    response = api.get("/orders/export", params=params)
    assert response.status_code == 200
    sheet = load_workbook(io.BytesIO(response.content)).active
    header, *lines = [[cell.value for cell in line] for line in sheet.iter_rows()]
    return [dict(zip(header, line)) for line in lines]


@pytest.mark.parametrize("asked_here", [False, True])
def test_the_exports_say_what_the_page_says_of_a_unit_flipkart_never_listed(db, account, api, asked_here) -> None:
    """A 2-unit order the account cancelled, of which Flipkart listed one unit:
    the other says nothing itself and takes its order's verdict on the page —
    and in the CSV, the workbook and both owner sheets alike. The account's
    word is its reason, or (asked_here) a cancel request done in the tool
    beside Flipkart's own seller-policy line."""
    order = _order(db, account, order_id="OD338967210239333125", qty=2, payment_mode="card")
    if asked_here:
        db.add(
            UnitRequest(unit_pk=order.units[0].id, kind="cancel", reason="x", status="done", requested_by="o@test.com")
        )
        db.commit()
    reason = SELLER_POLICY if asked_here else USER_ASKED
    _read_back(account, _Seen(order_id=order.order_id, unit_id="u1", platform_status="Cancelled", cancel_reason=reason))
    db.expire_all()
    assert db.get(Order, order.id).status == OrderStatus.failed

    page = {item["unit_position"]: item["cancelled_by"] for item in api.get("/orders").json()["items"]}
    full = {line["Unit No"]: (line["Outcome"], line["Cancelled"], line["Cancelled by User"]) for line in _export_lines(api, "/orders/export.csv")}
    book = {line["Unit No"]: line["Status"] for line in _workbook_lines(api)}
    sheets = {
        name: sorted(line["Status"] for line in _export_lines(api, "/orders/export.csv", sheet=name))
        for name in ("out_for_delivery", "order_data")
    }

    assert page == {1: "user", 2: "user"}
    assert full == {"1": ("cancelled_by_user", "Yes", "Yes"), "2": ("cancelled_by_user", "Yes", "Yes")}
    assert book == {1: "cancelled_by_user", 2: "cancelled_by_user"}
    assert sheets == {"out_for_delivery": ["Cancelled", "Cancelled by user"], "order_data": ["Cancelled", "Cancelled by user"]}


def test_the_ledger_the_dashboard_and_the_alert_agree_on_a_unit_flipkart_never_listed(db, account, api) -> None:
    """The review's case, from the read-back on: units asked for that Flipkart
    never lists, of orders whose listed units it cancelled — at the account's
    request, under its seller policy, or one of each. Each unlisted unit reads
    as its order does, on the ledger as on the dashboard and in the alert."""
    by_user = _order(db, account, order_id="OD338967210239333122", qty=2)
    by_flipkart = _order(db, account, order_id="OD338967210239333123", qty=2)
    mixed = _order(db, account, order_id="OD338967210239333124", qty=3)

    _read_back(
        account,
        _Seen(order_id=by_user.order_id, unit_id="u1", platform_status="Cancelled", cancel_reason=USER_ASKED),
        _Seen(order_id=by_flipkart.order_id, unit_id="u2", platform_status="Cancelled", cancel_reason=SELLER_POLICY),
        _Seen(order_id=mixed.order_id, unit_id="u3", platform_status="Cancelled", cancel_reason=USER_ASKED),
        _Seen(order_id=mixed.order_id, unit_id="u4", platform_status="Cancelled", cancel_reason=SELLER_POLICY),
    )

    db.expire_all()
    for order in (by_user, by_flipkart, mixed):
        row = db.get(Order, order.id)
        assert row.status == OrderStatus.failed  # every unit Flipkart listed is cancelled
        assert row.units[-1].synced_at is None and row.units[-1].platform_status is None  # never listed
    ledger: dict[int, dict[int, str]] = {}
    for item in api.get("/orders").json()["items"]:
        ledger.setdefault(item["order_row_id"], {})[item["unit_position"]] = item["cancelled_by"]
    assert ledger == {
        by_user.id: {1: "user", 2: "user"},
        by_flipkart.id: {1: "flipkart", 2: "flipkart"},
        mixed.id: {1: "user", 2: "flipkart", 3: "flipkart"},  # a mix is Flipkart's
    }
    stats = api.get("/stats").json()
    assert (stats["orders_cancelled"], stats["orders_cancelled_by_user"]) == (2, 1)
    alerts.evaluate(db)
    assert {order.id: _alert(db, order).details["cancelled_by"] for order in (by_user, by_flipkart, mixed)} == {
        by_user.id: "user",
        by_flipkart.id: "flipkart",
        mixed.id: "flipkart",
    }
    assert _alert(db, by_user).severity == AlertSeverity.info


# --- the dashboard -------------------------------------------------------------------
def test_failed_counts_only_what_never_reached_flipkart(db, account, api) -> None:
    _order(db, account, status=OrderStatus.failed, model="HP Backpack")  # the run stopped before sending
    _order(db, account, status=OrderStatus.failed, model="Realme 2")
    _cancelled(db, account, "OD338967210239333130", reason=SELLER_POLICY)
    _cancelled(db, account, "OD338967210239333131", reason=None)
    _cancelled(db, account, "OD338967210239333132", reason=USER_ASKED)  # placed, then cancelled there too
    _order(db, account, order_id="OD338967210239333133")  # placed

    stats = api.get("/stats").json()
    assert (stats["orders_failed"], stats["orders_cancelled"], stats["orders_placed"]) == (2, 2, 1)
    # The one the account asked to cancel is counted apart from Flipkart's.
    assert stats["orders_cancelled_by_user"] == 1


# --- the alert ---------------------------------------------------------------------
def _alert(db, order: Order) -> Alert:
    rows = db.scalars(select(Alert).where(Alert.key == f"order_failed:order:{order.id}")).all()
    assert len(rows) == 1  # one problem, one row
    return rows[0]


def test_the_alert_says_flipkart_cancelled_it_and_why(db, account) -> None:
    order = _cancelled(db, account, "OD338967210239333140", reason=SELLER_POLICY)
    alerts.evaluate(db)

    row = _alert(db, order)
    assert row.kind == "order_failed" and not row.resolved
    assert row.severity == AlertSeverity.warning
    assert row.message == (
        f"Order OD338967210239333140 (vivo T5 Lite 44W 5G) for cancel@test.com was cancelled by Flipkart: {SELLER_POLICY}"
    )
    assert "failed" not in row.message
    assert row.details["cancelled_by"] == "flipkart"
    assert row.details["cancel_reason"] == SELLER_POLICY
    assert row.details["order_id"] == "OD338967210239333140"


def test_a_run_that_never_reached_flipkart_is_no_cancellation(db, account) -> None:
    """Said as the run's own failure, never as Flipkart's cancellation; the
    run's reason, when it left one, is pinned in test_r1_not_placed_and_alert_reasons."""
    order = _order(db, account, status=OrderStatus.failed, model="HP Backpack")
    alerts.evaluate(db)
    row = _alert(db, order)
    assert row.message == (
        "The order (HP Backpack) for cancel@test.com was not placed: "
        "the run stopped before anything was sent to Flipkart"
    )
    assert "cancelled" not in row.message
    assert row.details == {"model": "HP Backpack", "product_link": None, "reason": None}


def test_without_a_reason_read_the_alert_says_none_was_recorded(db, account) -> None:
    order = _cancelled(db, account, "OD338967210239333141", reason=None)
    alerts.evaluate(db)
    assert _alert(db, order).message.endswith("was cancelled by Flipkart; no reason recorded")


def test_a_cancellation_the_account_asked_for_says_so_and_is_info(db, account) -> None:
    order = _cancelled(db, account, "OD338967210239333142", reason=USER_ASKED)
    alerts.evaluate(db)
    row = _alert(db, order)
    assert row.message.endswith(f"was cancelled at the account's request: {USER_ASKED}")
    assert row.severity == AlertSeverity.info
    assert row.details["cancelled_by"] == "user"


def test_an_open_alert_from_before_is_reworded_where_it_stands(db, account) -> None:
    """Raised as "Order N failed" before the change: the same key is updated
    in place — no second row, still open, reworded."""
    order = _cancelled(db, account, "OD338967210239333143", reason=SELLER_POLICY)
    key = f"order_failed:order:{order.id}"
    alerts.raise_alert(
        db,
        key=key,
        kind="order_failed",
        message=f"Order {order.id} (vivo T5 Lite 44W 5G) failed for cancel@test.com",
        details={"model": "vivo T5 Lite 44W 5G", "product_link": None},
    )

    counts = alerts.evaluate(db)

    row = _alert(db, order)
    assert counts["order_failed"] == 1
    assert not row.resolved
    assert "was cancelled by Flipkart" in row.message and SELLER_POLICY in row.message
    assert len(db.scalars(select(Alert)).all()) == 1  # reworded, not raised again


def test_an_alert_a_person_accepted_stays_accepted(db, account) -> None:
    order = _cancelled(db, account, "OD338967210239333144", reason=SELLER_POLICY)
    alerts.evaluate(db)
    key = f"order_failed:order:{order.id}"
    assert alerts.resolve_alert(db, key, "Flipkart's seller policy; nothing to do", who="owner@test.com")

    alerts.evaluate(db)
    row = _alert(db, order)
    assert row.resolved and row.resolved_by == alerts.RESOLVED_BY_USER
    assert row.resolution_note == "Flipkart's seller policy; nothing to do"


# --- the CSV -----------------------------------------------------------------------
def _csv_rows(api, **params) -> dict[str, dict]:
    response = api.get("/orders/export.csv", params=params)
    assert response.status_code == 200
    body = response.text.lstrip("﻿")
    header = next(csv.reader(io.StringIO(body)))
    assert header == orders_csv.HEADERS
    assert header[header.index("Product Link") + 1] == "Cancellation Reason"
    assert header[header.index("Cancelled") + 1] == "Cancelled by User"
    return {row["Order ID"]: row for row in csv.DictReader(io.StringIO(body))}


def test_the_csv_says_who_cancelled_and_why(db, account, api) -> None:
    _cancelled(db, account, "OD338967210239333150", reason=SELLER_POLICY)
    _cancelled(db, account, "OD338967210239333151", reason=USER_ASKED)
    _cancelled(db, account, "OD338967210239333152", reason=None, status="Cancelled by user - Order shipped")
    _cancelled(db, account, "OD338967210239333153", reason=None)
    delivered = _order(db, account, order_id="OD338967210239333154")
    delivered.units[0].platform_status = "Delivered"
    db.commit()

    rows = _csv_rows(api)
    cells = {
        order_id[-3:]: (row["Cancelled"], row["Cancelled by User"], row["Cancellation Reason"])
        for order_id, row in rows.items()
    }
    assert cells == {
        "150": ("Yes", "No", SELLER_POLICY),  # Flipkart's own
        "151": ("Yes", "Yes", USER_ASKED),
        "152": ("Yes", "Yes", ""),  # the status says so
        "153": ("Yes", "", ""),  # cancelled, and nothing says by whom
        "154": ("No", "No", ""),
    }


def test_a_reason_that_looks_like_a_formula_is_kept_as_text(db, account, api) -> None:
    _cancelled(db, account, "OD338967210239333160", reason='=HYPERLINK("http://x.test","click")')
    row = _csv_rows(api)["OD338967210239333160"]
    assert row["Cancellation Reason"] == ' =HYPERLINK("http://x.test","click")'


#: A refunded cancellation's page: the "Cancelled, <date>" step stays.
_CANCELLED_STEP = [{"status": "Order Confirmed", "date": None}, {"status": "Cancelled", "date": None}]


@pytest.mark.parametrize(
    ("fields", "shows"),
    [
        ({"cancel_reason": SELLER_POLICY}, True),
        ({"timeline": _CANCELLED_STEP}, True),
        # A row read back before 26 Oct 2026 holds its timeline as text.
        ({"timeline": "[{'status': 'Cancelled', 'date': '2026-10-24T00:00:00'}]"}, True),
        ({}, False),
        ({"timeline": [{"status": "Order Confirmed", "date": None}]}, False),
        # A page that shows the delivery was no cancellation, whatever else it
        # says — a replacement asked for and cancelled, say.
        (
            {
                "cancel_reason": "Your replacement request has been cancelled.",
                "timeline": [{"status": "Delivered", "date": None}, {"status": "Cancelled", "date": None}],
            },
            False,
        ),
        # The order history's dated delivery step is named "Delivery", which
        # reads as nothing; the delivery day the read-back took from it says it.
        (
            {
                "cancel_reason": "Your replacement request has been cancelled.",
                "timeline": [
                    {"status": "Delivery", "date": "2026-10-29T00:00:00"},
                    {"status": "Cancelled", "date": "2026-10-30T00:00:00"},
                ],
                "delivered_date": datetime(2026, 10, 29),
            },
            False,
        ),
    ],
)
def test_what_shows_a_cancellation_on_a_units_own_page(fields, shows) -> None:
    unit = OrderUnit(platform="flipkart", position=1, platform_status="Refund Completed", **fields)
    assert cancellations.shows_cancellation(unit) is shows


@pytest.mark.parametrize(
    ("order_fields", "unit_fields", "cancelled"),
    [
        ({}, {"platform_status": "Cancelled"}, True),
        ({}, {"platform_status": "Refund Completed", "cancel_reason": USER_ASKED}, True),
        ({}, {"platform_status": "Refund Completed", "timeline": _CANCELLED_STEP}, True),
        ({}, {"platform_status": "Refund Completed"}, False),  # a return, as far as anything says
        ({"status": OrderStatus.failed}, {"platform_status": "Refund Completed"}, True),  # its order was cancelled
        ({"status": OrderStatus.failed}, {}, True),  # a unit Flipkart never listed went with its order
        ({"status": OrderStatus.failed, "order_id": None}, {}, False),  # a run that sent nothing
        ({}, {}, False),
        ({"status": OrderStatus.failed}, {"platform_status": "Delivered"}, False),
        ({}, {"platform_status": "Out for delivery", "cancel_reason": USER_ASKED}, False),  # on its way
    ],
)
def test_whether_a_unit_was_cancelled(order_fields, unit_fields, cancelled) -> None:
    order = Order(**{"status": OrderStatus.placed, "order_id": "OD338967210239333260", **order_fields})
    unit = OrderUnit(platform="flipkart", position=1, **unit_fields)
    assert cancellations.unit_was_cancelled(order, unit) is cancelled


@pytest.mark.parametrize(
    ("reason", "request_status", "by_user"),
    [
        (USER_ASKED, None, "Yes"),
        (USER_ASKED, "done", "Yes"),
        (SELLER_POLICY, None, "No"),
        (SELLER_POLICY, "done", "Yes"),  # asked for here, done on Flipkart by hand
    ],
)
def test_a_refunded_cancellation_still_reads_cancelled_in_the_csv(db, account, api, reason, request_status, by_user) -> None:
    """Flipkart words a card order it cancelled "Refund Completed" once the
    money is back, which the portal reads as returned. The read-back keeps
    that wording and the order stays cancelled on the page, the dashboard and
    the Outcome filter — so the CSV still says the unit was cancelled, and by
    whom, and its Refund Status says the money is back (review of 26 Oct 2026)."""
    order = _order(db, account, order_id="OD338967210239333220", payment_mode="card")
    if request_status:
        _ask_to_cancel(db, order, status=request_status)

    _read_back(account, _Seen(order_id=order.order_id, unit_id="u1", platform_status="Cancelled", cancel_reason=reason))
    _read_back(account, _Seen(order_id=order.order_id, unit_id="u1", platform_status="Refund Completed"))

    db.expire_all()
    stored = db.get(Order, order.id)
    assert stored.status == OrderStatus.failed  # never turned back
    # What the read-back stores is Flipkart's wording, as before.
    unit = stored.units[0]
    assert (unit.platform_status, unit.delivery_state, unit.cancel_reason) == ("Refund Completed", "returned", reason)

    (item,) = api.get("/orders", params={"status": "cancelled"}).json()["items"]
    row = _csv_rows(api, status="cancelled")[order.order_id]
    assert (row["Cancelled"], row["Cancelled by User"], row["Refund Status"]) == ("Yes", by_user, "Refund Completed")
    assert item["cancelled_by"] == ("user" if by_user == "Yes" else "flipkart")  # the page's own verdict
    assert (row["Status"], row["Delivery State"], row["Cancellation Reason"]) == ("Refund Completed", "returned", reason)


def test_a_refund_reads_as_a_cancellation_only_on_a_unit_that_was_cancelled(db, account, api) -> None:
    """Unit by unit, from each unit's own page, on an order that stays placed:
    a unit cancelled at the account's request and refunded, one whose
    refunded page shows only its "Cancelled" step (no reason is read under
    it then), and one delivered and sent back for a refund — a return."""
    order = _order(db, account, order_id="OD338967210239333230", qty=3, payment_mode="card")
    oid = order.order_id
    apply_observations(
        db,
        order,
        [
            _Seen(order_id=oid, unit_id="u1", platform_status="Cancelled", cancel_reason=USER_ASKED),
            _Seen(order_id=oid, unit_id="u2", platform_status="Shipped"),
            _Seen(order_id=oid, unit_id="u3", platform_status="Delivered"),
        ],
    )
    db.commit()
    apply_observations(
        db,
        order,
        [
            _Seen(order_id=oid, unit_id="u1", platform_status="Refund Completed"),
            _Seen(order_id=oid, unit_id="u2", platform_status="Refund Completed", timeline=_CANCELLED_STEP),
            _Seen(
                order_id=oid,
                unit_id="u3",
                platform_status="Refund Completed",
                timeline=[
                    {"status": "Delivered", "date": "2026-10-24T00:00:00"},
                    {"status": "Returned", "date": "2026-10-26T00:00:00"},
                ],
            ),
        ],
    )
    db.commit()
    assert db.get(Order, order.id).status == OrderStatus.placed

    response = api.get("/orders/export.csv")
    rows = {row["Unit ID"]: row for row in csv.DictReader(io.StringIO(response.text.lstrip("﻿")))}
    cells = {unit_id: (row["Cancelled"], row["Cancelled by User"], row["Refund Status"]) for unit_id, row in rows.items()}
    assert cells == {
        "U1": ("Yes", "Yes", "Refund Completed"),
        "U2": ("Yes", "", "Refund Completed"),  # cancelled, and nothing says by whom
        "U3": ("No", "No", ""),  # a return: no cancellation's refund
    }


def test_the_exports_never_call_an_order_flipkart_cancelled_failed(db, account, api) -> None:
    """Exported under the Outcome filter "Cancelled on Flipkart", every row
    says who cancelled it, as the page's Outcome does — the CSV's Outcome and
    the workbook's Status alike. "failed" is left to a run that sent nothing."""
    from openpyxl import load_workbook

    _order(db, account, status=OrderStatus.failed, model="HP Backpack")  # never sent: no order id
    _cancelled(db, account, "OD338967210239333240", reason=SELLER_POLICY)
    _cancelled(db, account, "OD338967210239333241", reason=USER_ASKED)
    asked = _cancelled(db, account, "OD338967210239333242", reason=SELLER_POLICY)
    _ask_to_cancel(db, asked, status="done")  # the page says the user's, whatever the wording

    def workbook_status(**params) -> dict[str | None, str]:
        sheet = load_workbook(io.BytesIO(api.get("/orders/export", params=params).content)).active
        header, *lines = [[cell.value for cell in line] for line in sheet.iter_rows()]
        return {line[header.index("Order ID")]: line[header.index("Status")] for line in lines}

    page = {item["order_id"]: item for item in api.get("/orders", params={"status": "cancelled"}).json()["items"]}
    expected = {order_id: f"cancelled_by_{item['cancelled_by']}" for order_id, item in page.items()}
    assert expected == {
        "OD338967210239333240": "cancelled_by_flipkart",
        "OD338967210239333241": "cancelled_by_user",
        "OD338967210239333242": "cancelled_by_user",
    }
    assert {order_id: row["Outcome"] for order_id, row in _csv_rows(api, status="cancelled").items()} == expected
    assert workbook_status(status="cancelled") == expected

    # A run that never reached Flipkart (it has no order id) still says so.
    assert {order_id: row["Outcome"] for order_id, row in _csv_rows(api, status="failed").items()} == {"": "failed"}
    assert workbook_status(status="failed") == {None: "failed"}


def test_a_unit_flipkart_never_listed_never_reads_failed_in_the_owners_sheets(db, account, api) -> None:
    """The owner's two sheets write Flipkart's wording, and the tool's outcome
    for a unit with none. For a unit Flipkart never listed, of an order it
    took and then cancelled, that outcome is the cancellation — never
    "Failed", which only a run that sent nothing says."""
    order = _order(db, account, order_id="OD338967210239333250", qty=2, status=OrderStatus.failed)
    order.units[0].platform_status, order.units[0].cancel_reason = "Cancelled", SELLER_POLICY
    db.commit()
    asked = _order(db, account, order_id="OD338967210239333251", qty=2, status=OrderStatus.failed)
    asked.units[0].platform_status, asked.units[0].cancel_reason = "Cancelled", SELLER_POLICY
    db.commit()
    _ask_to_cancel(db, asked, position=1)  # someone asked to cancel the unlisted unit
    _order(db, account, status=OrderStatus.failed, model="HP Backpack")  # never sent

    for sheet in ("out_for_delivery", "order_data"):
        response = api.get("/orders/export.csv", params={"sheet": sheet, "status": "cancelled"})
        words: dict[str, list[str]] = {}
        for row in csv.DictReader(io.StringIO(response.text.lstrip("﻿"))):
            words.setdefault(row["Order ID"], []).append(row["Status"])
        assert words == {
            "OD338967210239333250": ["Cancelled", "Cancelled by Flipkart"],
            "OD338967210239333251": ["Cancelled", "Cancelled by user"],
        }, sheet
        failed = api.get("/orders/export.csv", params={"sheet": sheet, "status": "failed"})
        assert [row["Status"] for row in csv.DictReader(io.StringIO(failed.text.lstrip("﻿")))] == ["Failed"]


# --- the migration -------------------------------------------------------------------
def _alembic_installed() -> bool:
    # The migrations folder is itself called "alembic" (see test_w1c_migrations).
    try:
        return importlib.util.find_spec("alembic.command") is not None
    except ImportError:
        return False


needs_alembic = pytest.mark.skipif(not _alembic_installed(), reason="alembic is not installed")


def _upgrade_or_downgrade(engine, name: str, revision: str) -> None:
    from alembic import command

    with engine.begin() as connection:
        getattr(command, name)(migrations.alembic_config(connection), revision)


def _unit_columns(engine) -> list[str]:
    return [column["name"] for column in inspect(engine).get_columns("order_units")]


@needs_alembic
def test_revision_0003_adds_the_column_and_takes_it_away_again(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", poolclass=NullPool)
    try:
        _upgrade_or_downgrade(engine, "upgrade", "0002")
        assert "cancel_reason" not in _unit_columns(engine)
        _upgrade_or_downgrade(engine, "upgrade", "0003")
        assert "cancel_reason" in _unit_columns(engine)
        column = next(c for c in inspect(engine).get_columns("order_units") if c["name"] == "cancel_reason")
        assert column["nullable"] and getattr(column["type"], "length", None) == 255
        _upgrade_or_downgrade(engine, "downgrade", "0002")
        assert "cancel_reason" not in _unit_columns(engine)
    finally:
        engine.dispose()


@needs_alembic
def test_revision_0003_leaves_a_column_init_db_already_added(tmp_path) -> None:
    """A new API started before the migration ran adds the nullable column
    itself (init_db's sync_missing_columns); the revision then finds it there
    and adds nothing, instead of stopping on "already exists"."""
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", poolclass=NullPool)
    try:
        _upgrade_or_downgrade(engine, "upgrade", "0002")
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE order_units ADD COLUMN cancel_reason VARCHAR(255)"))
        _upgrade_or_downgrade(engine, "upgrade", "0003")
        assert _unit_columns(engine).count("cancel_reason") == 1
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0003"
    finally:
        engine.dispose()


@needs_alembic
def test_revision_0003_does_nothing_where_there_is_no_order_units_table(tmp_path) -> None:
    """As revision 0002 does for compiled_archives: init_db creates the table
    with the column, so there is nothing to add — and nothing to take away."""
    from alembic import command

    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", poolclass=NullPool)
    try:
        with engine.begin() as connection:
            command.stamp(migrations.alembic_config(connection), "0002")
        _upgrade_or_downgrade(engine, "upgrade", "0003")
        assert "order_units" not in inspect(engine).get_table_names()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0003"
        _upgrade_or_downgrade(engine, "downgrade", "0002")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002"
    finally:
        engine.dispose()


# --- the tool's own record of a cancel request (spec FR-020) ---------------------------
def _ask_to_cancel(db, order: Order, *, status: str = "open", position: int = 0) -> None:
    db.add(
        UnitRequest(
            unit_pk=order.units[position].id,
            kind="cancel",
            reason="changed our mind",
            status=status,
            requested_by="owner@test.com",
        )
    )
    db.commit()


@pytest.mark.parametrize(("request_status", "who"), [("open", "user"), ("done", "user"), ("dropped", "flipkart")])
def test_a_cancel_request_the_tool_holds_makes_it_the_users_cancellation(db, account, api, request_status, who) -> None:
    """Asked for here and done on Flipkart by hand, a cancellation can still
    read like Flipkart's own on the order page; the request says whose it
    was. A dropped request was withdrawn and says nothing."""
    order = _cancelled(db, account, "OD338967210239333170", reason=SELLER_POLICY)
    _ask_to_cancel(db, order, status=request_status)

    (item,) = [row for row in api.get("/orders").json()["items"] if row["order_row_id"] == order.id]
    assert item["cancelled_by"] == who
    assert OrderOut.from_order(db.get(Order, order.id)).cancelled_by == who

    cell = _csv_rows(api)["OD338967210239333170"]["Cancelled by User"]
    assert cell == ("Yes" if who == "user" else "No")

    alerts.evaluate(db)
    row = _alert(db, order)
    assert row.details["cancelled_by"] == who
    stats = api.get("/stats").json()
    assert (stats["orders_cancelled"], stats["orders_cancelled_by_user"]) == ((0, 1) if who == "user" else (1, 0))


def test_the_ledger_reads_cancel_requests_in_one_query_for_the_page(db, account, api) -> None:
    from sqlalchemy import event

    from app.db.base import engine

    for number in range(5):
        order = _cancelled(db, account, f"OD33896721023933318{number}", reason=SELLER_POLICY)
        _ask_to_cancel(db, order)
    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        items = api.get("/orders").json()["items"]
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert {item["cancelled_by"] for item in items} == {"user"}
    assert sum(1 for statement in statements if "FROM unit_requests" in statement) == 2  # open kinds, and asked


def test_units_flipkart_never_listed_cost_one_lookup_for_the_page_not_one_per_row(db, account, api) -> None:
    """A page with units that say nothing reads their orders' verdicts in one
    go; a page with none reads nothing more (the test above)."""
    from sqlalchemy import event

    from app.db.base import engine

    for number in range(5):
        order = _order(db, account, order_id=f"OD33896721023933326{number}", qty=2, status=OrderStatus.failed)
        order.units[0].platform_status, order.units[0].cancel_reason = "Cancelled", USER_ASKED
        db.commit()
    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        items = api.get("/orders").json()["items"]
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert len(items) == 10 and {item["cancelled_by"] for item in items} == {"user"}
    # Open kinds, asked, and the five orders' readings together.
    assert sum(1 for statement in statements if "FROM unit_requests" in statement) == 3


def test_the_csv_says_no_only_when_the_wording_is_flipkarts_or_the_sellers(db, account, api) -> None:
    _cancelled(db, account, "OD338967210239333190", reason="Your order was cancelled.")
    _cancelled(db, account, "OD338967210239333191", reason="Cancelled by the seller as the item is out of stock")
    _cancelled(db, account, "OD338967210239333192", reason=None, status="Cancelled")

    cells = {order_id[-3:]: row["Cancelled by User"] for order_id, row in _csv_rows(api).items()}

    assert cells == {"190": "", "191": "No", "192": ""}


def test_a_mixed_cancellation_quotes_only_flipkarts_reasons_and_says_how_many_were_the_accounts(db, account) -> None:
    order = _order(db, account, order_id="OD338967210239333200", qty=3, status=OrderStatus.failed)
    first, second, third = order.units
    first.platform_status, first.cancel_reason = "Cancelled", USER_ASKED
    second.platform_status, second.cancel_reason = "Cancelled", SELLER_POLICY
    third.platform_status = "Cancelled"
    db.commit()

    alerts.evaluate(db)

    row = _alert(db, order)
    assert row.message == (
        "Order OD338967210239333200 (vivo T5 Lite 44W 5G) for cancel@test.com was cancelled by Flipkart "
        f"(one unit at the account's request): {SELLER_POLICY}"
    )
    assert USER_ASKED not in row.message
    assert row.severity == AlertSeverity.warning
    assert row.details["cancelled_by"] == "flipkart" and row.details["cancel_reason"] == SELLER_POLICY


def test_two_units_asked_for_are_counted_as_such(db, account) -> None:
    order = _order(db, account, order_id="OD338967210239333201", qty=3, status=OrderStatus.failed)
    for unit in order.units:
        unit.platform_status, unit.cancel_reason = "Cancelled", SELLER_POLICY
    db.commit()
    _ask_to_cancel(db, order, position=0)
    _ask_to_cancel(db, order, position=1)

    alerts.evaluate(db)

    assert "(2 units at the account's request)" in _alert(db, order).message


# --- the ledger's Outcome filter -------------------------------------------------------
def test_failed_means_never_sent_and_cancelled_is_its_own_outcome(db, account, api) -> None:
    never_sent = _order(db, account, status=OrderStatus.failed, model="HP Backpack")
    by_flipkart = _cancelled(db, account, "OD338967210239333210", reason=SELLER_POLICY)
    by_user = _cancelled(db, account, "OD338967210239333211", reason=USER_ASKED)
    placed = _order(db, account, order_id="OD338967210239333212")

    def rows(*outcomes: str) -> set[int]:
        query = "&".join(f"status={outcome}" for outcome in outcomes)
        response = api.get(f"/orders?{query}")
        assert response.status_code == 200, response.text
        return {item["order_row_id"] for item in response.json()["items"]}

    assert rows("failed") == {never_sent.id}
    assert rows("cancelled") == {by_flipkart.id, by_user.id}
    assert rows("failed", "cancelled") == {never_sent.id, by_flipkart.id, by_user.id}
    assert rows("placed") == {placed.id}
    assert api.get("/orders?status=refunded").status_code == 422

    counts = {entry["value"]: entry["count"] for entry in api.get("/orders/filter-options").json()["statuses"]}
    assert (counts["failed"], counts["cancelled"], counts["placed"]) == (1, 2, 1)


def test_a_saved_view_keeps_the_cancelled_outcome(db, account, api) -> None:
    response = api.put("/orders/view", json={"columns": [], "filters": {"status": "cancelled"}})
    assert response.status_code == 200, response.text
    assert api.get("/orders/view").json()["filters"]["status"] == "cancelled"
