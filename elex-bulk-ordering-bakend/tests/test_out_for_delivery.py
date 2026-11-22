"""Out for delivery and the delivery code, followed on their own (the owner's
ask of 26 Oct 2026): the timeline kept as steps, "Delivery expected" read as
on its way, the code read in more of its forms, the dashboard's list, and the
timer that reads back only the accounts with an order still on its way."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api import jobs as jobs_api
from app.api import stats as stats_api
from app.automation import sync
from app.automation.sync import ScrapedOrder
from app.config import get_settings
from app.core import app_settings, order_status
from app.core import selectors as S
from app.core.order_units import out_for_delivery_rows, sync_unit_readings, timeline_steps
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    OrderUnit,
    naive_utcnow,
)
from app.jobs import schedule, sync_tasks
from app.schemas.order import OrderOut


def _account(db, email: str, status: AccountStatus = AccountStatus.session_valid) -> Account:
    row = Account(email=email, status=status)
    db.add(row)
    db.commit()
    return row


def _order(db, account: Account, *, status=OrderStatus.placed, placed_at=None, order_id="OD338980309549917100", **unit):
    order = Order(
        account_id=account.id,
        product_link="https://www.flipkart.com/p",
        qty=1,
        model="Sa Rasa Kurta",
        amount=707.0,
        status=status,
        order_id=order_id,
        placed_at=placed_at or naive_utcnow(),
    )
    db.add(order)
    db.commit()
    first = order.units[0]
    for name, value in unit.items():
        setattr(first, name, value)
    db.commit()
    return order, first


def _read_back(db, account: Account, *, minutes_ago: int) -> None:
    job = Job(type=JobType.sync, status=JobStatus.completed, payload={"tracking": True})
    db.add(job)
    db.commit()
    db.add(
        JobItem(
            job_id=job.id,
            account_id=account.id,
            status=JobItemStatus.done,
            finished_at=naive_utcnow() - timedelta(minutes=minutes_ago),
        )
    )
    db.commit()


# --- the timeline, kept as steps ----------------------------------------------------
def test_timeline_steps_reads_lists_and_the_text_older_rows_hold():
    steps = [{"status": "Order Confirmed", "date": None}, {"status": "Shipped", "date": "2026-10-26T00:00:00"}]
    assert timeline_steps(steps) == steps
    # What rows read back before 26 Oct 2026 hold: the list's Python text.
    assert timeline_steps(str(steps)) == steps
    assert timeline_steps("not a list") is None
    assert timeline_steps(None) is None and timeline_steps([]) is None
    assert timeline_steps([{"status": ""}, "x", 3]) is None


def test_the_read_back_keeps_the_timeline_as_steps_not_text():
    observation = ScrapedOrder(order_id="OD1", timeline=[{"status": "Shipped", "date": None}])
    assert sync_tasks._observed(observation, "timeline") == [{"status": "Shipped", "date": None}]


def test_a_timeline_stored_as_text_reaches_the_api_as_steps(db):
    account = _account(db, "text@test.com")
    order, unit = _order(db, account)
    unit.timeline = "[{'status': 'Order Confirmed', 'date': None}]"
    db.commit()

    out = OrderOut.from_unit(unit, order, account.email)

    assert out.timeline == [{"status": "Order Confirmed", "date": None}]


def test_start_up_brings_old_readings_in_line(db):
    account = _account(db, "old@test.com")
    _order_row, unit = _order(db, account, platform_status="Delivery expected")
    # As written before 26 Oct 2026: "unknown", and the timeline as text.
    db.execute(update(OrderUnit).where(OrderUnit.id == unit.id).values(delivery_state="unknown"))
    db.commit()
    db.expire_all()
    unit = db.get(OrderUnit, unit.id)
    unit.timeline = "[{'status': 'Shipped', 'date': None}]"
    db.commit()

    changed = sync_unit_readings()

    assert changed["states"] >= 1 and changed["timelines"] == 1
    db.expire_all()
    unit = db.get(OrderUnit, unit.id)
    assert unit.delivery_state == "in_transit"
    assert unit.timeline == [{"status": "Shipped", "date": None}]
    assert sync_unit_readings() == {"states": 0, "timelines": 0, "orders": 0}  # nothing left to change


# --- Flipkart's wording -------------------------------------------------------------
@pytest.mark.parametrize("wording", ["Delivery expected", "delivery expected", "Expected delivery"])
def test_delivery_expected_is_on_its_way(wording):
    """"Delivery expected by Nov 01" is the live row wording of an order on its way."""
    assert order_status.classify(wording) == order_status.IN_TRANSIT


def test_the_other_readings_are_unchanged():
    assert order_status.classify("Delivered") == order_status.DELIVERED
    assert order_status.classify("Out For Delivery") == order_status.IN_TRANSIT
    assert order_status.classify("Cancelled") == order_status.CANCELLED
    assert order_status.classify("Undelivered") == order_status.FAILED


def test_the_timeline_matches_whatever_the_capitals():
    assert "translate(" in S.ORDER_TIMELINE_STEPS
    assert '"out for delivery"' in S.ORDER_TIMELINE_STEPS
    assert '"order received"' in S.ORDER_TIMELINE_STEPS


# --- the delivery code -------------------------------------------------------------
def test_a_code_drawn_one_digit_to_a_box_is_read():
    assert sync.parse_delivery_otp("Delivery OTP 4 8 2 7 Share it with the agent") == "4827"


def test_the_units_own_pincode_is_never_taken_for_the_code():
    text = "Delivery OTP will be shown here. Deliver to 226013"
    assert sync.parse_delivery_otp(text) == "226013"  # what it took before
    assert sync.parse_delivery_otp(text, exclude=frozenset({"226013"})) is None


def test_the_negatives_still_say_nothing():
    assert sync.parse_delivery_otp("Delivered on Oct 31, 2018 Return policy 568901") is None
    assert sync.parse_delivery_otp("Total ₹9891 Seller: Flashtech Retail") is None
    assert sync.parse_delivery_otp("OTP ₹ 1 2 3 4") is None


def test_an_out_for_delivery_page_with_no_readable_code_is_kept_for_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "DIAGNOSTICS_DIR", str(tmp_path))
    sync._keep_page_text(ScrapedOrder(order_id="OD123"), "Out for delivery today")

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == "Out for delivery today"
    assert "out-for-delivery-OD123" in files[0].name


# --- what is out for delivery -------------------------------------------------------
def test_out_for_delivery_rows(db):
    account = _account(db, "ofd@test.com")
    wording, _ = _order(db, account, order_id="OD100000000001", platform_status="Out For Delivery")
    code, _ = _order(db, account, order_id="OD100000000002", platform_status="Shipped", delivery_otp="4827")
    _order(db, account, order_id="OD100000000003", platform_status="Delivered", delivery_otp="9911")
    _order(db, account, order_id="OD100000000004", status=OrderStatus.failed, platform_status="Out for delivery")
    _order(db, account, order_id="OD100000000005", platform_status="Shipped")

    assert {order.id for _unit, order, _email in out_for_delivery_rows(db)} == {wording.id, code.id}


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(stats_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_the_dashboard_list_has_the_code_and_the_accounts_to_read_back(api, db):
    first = _account(db, "first@test.com")
    second = _account(db, "second@test.com")
    _order(db, first, order_id="OD100000000001", platform_status="Out For Delivery", delivery_otp="4827", synced_at=naive_utcnow())
    _order(db, second, order_id="OD100000000002", platform_status="Shipped")

    body = api.get("/stats/out-for-delivery").json()

    assert body["total"] == 1
    assert body["units"][0]["delivery_otp"] == "4827"
    assert body["units"][0]["account_email"] == "first@test.com"
    assert body["account_ids"] == [first.id]
    assert body["order_ids"] == ["OD100000000001"]
    assert body["last_read_back"] is not None


def test_the_dashboard_list_needs_a_token():
    application = FastAPI()
    application.include_router(stats_api.router)
    assert TestClient(application).get("/stats/out-for-delivery").status_code in (401, 403)


# --- following orders until they arrive ------------------------------------------------
def test_no_order_on_its_way_means_no_tracking(db):
    _account(db, "idle@test.com")
    assert schedule.tracking_due(db) == ([], "no order is on its way")


def test_an_order_never_read_back_is_due_at_once(db):
    account = _account(db, "new@test.com")
    _order(db, account)

    accounts, why = schedule.tracking_due(db)

    assert accounts == [account.id]
    assert "1 account(s)" in why


def test_an_order_read_back_within_the_hour_is_not_due_again(db):
    account = _account(db, "recent@test.com")
    _order(db, account, platform_status="Shipped", synced_at=naive_utcnow())
    _read_back(db, account, minutes_ago=20)

    assert schedule.tracking_due(db)[0] == []
    assert schedule.tracking_due(db, now=naive_utcnow() + timedelta(minutes=45))[0] == [account.id]


def test_out_for_delivery_is_read_back_every_half_hour(db):
    account = _account(db, "van@test.com")
    _order(db, account, platform_status="Out For Delivery", synced_at=naive_utcnow())
    _read_back(db, account, minutes_ago=35)

    accounts, why = schedule.tracking_due(db)

    assert accounts == [account.id]
    assert "(1 out for delivery)" in why


def test_delivered_cancelled_and_old_orders_are_not_followed(db):
    account = _account(db, "done@test.com")
    _order(db, account, order_id="OD100000000001", platform_status="Delivered", synced_at=naive_utcnow())
    _order(db, account, order_id="OD100000000002", status=OrderStatus.failed, platform_status="Cancelled", synced_at=naive_utcnow())
    _order(db, account, order_id="OD100000000003", placed_at=naive_utcnow() - timedelta(days=60))

    assert schedule.tracking_due(db)[0] == []


def test_a_capped_quantitys_placeholders_do_not_keep_an_order_open(db):
    """Three units asked for, Flipkart listed one and delivered it: the two
    placeholders it never listed decide nothing, as in the order row's own reading."""
    account = _account(db, "capped@test.com")
    order = Order(account_id=account.id, qty=3, status=OrderStatus.placed, order_id="OD100000000009", placed_at=naive_utcnow())
    db.add(order)
    db.commit()
    first = sorted(order.units, key=lambda unit: unit.position)[0]
    first.platform_status = "Delivered"
    first.synced_at = naive_utcnow()
    db.commit()

    assert schedule.tracking_due(db)[0] == []


def test_an_unconfirmed_order_is_followed_for_a_week(db):
    account = _account(db, "unsure@test.com")
    _order(db, account, status=OrderStatus.unconfirmed, order_id=None)
    assert schedule.tracking_due(db)[0] == [account.id]

    stale = _account(db, "stale@test.com")
    _order(db, stale, status=OrderStatus.unconfirmed, order_id=None, placed_at=naive_utcnow() - timedelta(days=10))
    assert schedule.tracking_due(db)[0] == [account.id]


def test_accounts_without_a_session_are_not_read(db):
    account = _account(db, "out@test.com", AccountStatus.session_invalid)
    _order(db, account)
    assert schedule.tracking_due(db) == ([], "no account with an order on its way holds a session")


def test_nothing_starts_while_a_run_is_going(db):
    account = _account(db, "busy@test.com")
    _order(db, account)
    running = Job(type=JobType.order, status=JobStatus.running)
    db.add(running)
    db.commit()

    assert schedule.tracking_due(db) == ([], f"run #{running.id} is still going")


def test_tracking_can_be_switched_off(db, monkeypatch):
    account = _account(db, "off@test.com")
    _order(db, account)
    settings = get_settings()
    monkeypatch.setattr(settings, "track_every_minutes", 0)
    monkeypatch.setattr(settings, "out_for_delivery_minutes", 0)

    assert schedule.tracking_due(db) == ([], "order tracking is off")


def test_a_tracking_read_back_does_not_put_the_full_one_off(db):
    app_settings.set_overrides(db, {"sync_every_hours": 6})
    db.add(Job(type=JobType.sync, status=JobStatus.completed, finished_at=naive_utcnow() - timedelta(hours=7)))
    db.commit()
    db.add(Job(type=JobType.sync, status=JobStatus.completed, finished_at=naive_utcnow(), payload={"tracking": True}))
    db.commit()

    assert schedule.read_back_due(db)[0] is True


def test_a_tick_reads_back_only_the_accounts_with_an_order_on_its_way(db, monkeypatch):
    with_order = _account(db, "with@test.com")
    _account(db, "without@test.com")
    _order(db, with_order)
    calls: list = []

    async def fake_run(job_id, account_ids, **kwargs):
        calls.append((job_id, list(account_ids), kwargs))

    def spawn(factory, job_id):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(factory())
        else:
            loop.create_task(factory())

    monkeypatch.setattr("app.jobs.sync_tasks.run_sync_job", fake_run)
    monkeypatch.setattr(jobs_api, "_spawn", spawn)

    async def tick():
        started = await schedule.tick_once()
        await asyncio.sleep(0.01)
        return started

    job_id = asyncio.run(tick())

    db.expire_all()
    job = db.get(Job, job_id)
    assert job.type == JobType.sync
    assert job.payload["tracking"] is True and job.payload["started_by"] == schedule.STARTED_BY
    assert calls == [
        (
            job_id,
            [with_order.id],
            {"limit_per_account": job.payload["limit_per_account"], "import_unknown": False, "order_ids": None, "follow_open": True},
        )
    ]
