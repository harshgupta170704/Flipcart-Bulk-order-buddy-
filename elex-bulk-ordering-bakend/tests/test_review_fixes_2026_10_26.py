"""The adversarial review of 26 Oct 2026's read-back, timer and one-account
changes: seven confirmed findings, each pinned here."""

import asyncio
import contextlib
from datetime import datetime, timedelta

from app.automation import sync
from app.config import get_settings
from app.core import app_settings, invoices
from app.core import selectors as S
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
    Session as SessionModel,
    naive_utcnow,
)
from app.jobs import schedule, sync_tasks
from tests.fakes import FakeContext, FakePage
from tests.test_schedule import _run_immediately
from tests.test_sync_scrape import _page as _list_page


def _account(db, email, *, status=AccountStatus.session_valid, cookies=True):
    account = Account(email=email, status=status)
    db.add(account)
    db.commit()
    if cookies:
        db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": email}], valid=True))
        db.commit()
    return account


def _order_run_on(db, account):
    job = Job(type=JobType.order, status=JobStatus.running)
    db.add(job)
    db.commit()
    db.add(JobItem(job_id=job.id, account_id=account.id, status=JobItemStatus.running))
    db.commit()
    return job


def _fake_browser():
    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    return fake_browser_page


def _item(db, job_id):
    db.expire_all()
    return db.query(JobItem).filter(JobItem.job_id == job_id).one()


def _read_back_item(db, account, *, minutes_ago, status=JobItemStatus.done):
    job = Job(type=JobType.sync, status=JobStatus.completed, payload={"tracking": True})
    db.add(job)
    db.commit()
    db.add(JobItem(job_id=job.id, account_id=account.id, status=status, finished_at=naive_utcnow() - timedelta(minutes=minutes_ago)))
    db.commit()


# --- 1. the read-back gives way to the very end -----------------------------------------------------------
def test_an_order_that_starts_during_the_last_page_gets_the_account_before_any_download(db, monkeypatch):
    account = _account(db, "late@test.com")
    read_back = Job(type=JobType.sync, status=JobStatus.queued)
    db.add(read_back)
    db.commit()
    started: list[int] = []

    async def scrape_orders(page, *, limit=20, open_details=True, should_stop=None):
        assert should_stop() is False  # before the last page: nobody else
        started.append(_order_run_on(db, account).id)  # an order starts during it
        return [sync.ScrapedOrder(order_id="OD1", unit_id="U1", platform_status="Delivered", invoice_url="https://www.flipkart.com/invoice/OD1")]

    downloads: list = []

    async def fetch_missing(context, account_id, scraped):
        downloads.append(account_id)
        return {}

    monkeypatch.setattr(sync_tasks, "browser_page", _fake_browser())
    monkeypatch.setattr(sync_tasks, "scrape_orders", scrape_orders)
    monkeypatch.setattr(invoices, "fetch_missing", fetch_missing)

    asyncio.run(sync_tasks.run_sync_job(read_back.id, [account.id]))

    item = _item(db, read_back.id)
    assert downloads == []
    assert item.status == JobItemStatus.skipped
    assert f"order run #{started[0]} could use this account" in item.message


def test_the_list_reader_does_not_go_back_to_the_list_once_told_to_stop():
    page = _list_page()
    answers = iter([False, False, False, True])  # three pages read, then an order starts

    asyncio.run(sync.scrape_orders(page, should_stop=lambda: next(answers)))

    assert page.goto_urls[-1] != S.ORDERS_URL


# --- 2. a run's listed orders: never promised a re-read nobody makes ------------------------------------
def test_a_by_id_read_back_passed_over_says_to_start_it_again(db, monkeypatch):
    account = _account(db, "listed@test.com")
    db.add(Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD9"))
    db.commit()
    order_run = _order_run_on(db, account)
    read_back = Job(type=JobType.sync, status=JobStatus.queued)
    db.add(read_back)
    db.commit()
    monkeypatch.setattr(sync_tasks, "browser_page", _fake_browser())

    asyncio.run(sync_tasks.run_sync_job(read_back.id, [account.id], order_ids=["OD9"]))

    message = _item(db, read_back.id).message
    assert f"order run #{order_run.id} is using this account" in message
    assert "start this read-back again once that run ends" in message


def test_a_by_id_read_back_stopped_part_way_counts_what_it_left(db, monkeypatch):
    account = _account(db, "partway@test.com")
    for order_id in ("OD1", "OD2", "OD3"):
        db.add(Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id=order_id))
    db.commit()
    read_back = Job(type=JobType.sync, status=JobStatus.queued)
    db.add(read_back)
    db.commit()

    async def scrape_order(page, order_id):
        if order_id == "OD1":
            _order_run_on(db, account)  # an order starts after the first one
        return [sync.ScrapedOrder(order_id=order_id, unit_id=f"U{order_id}", platform_status="Shipped")]

    monkeypatch.setattr(sync_tasks, "browser_page", _fake_browser())
    monkeypatch.setattr(sync_tasks, "scrape_order", scrape_order)

    asyncio.run(sync_tasks.run_sync_job(read_back.id, [account.id], order_ids=["OD1", "OD2", "OD3"]))

    message = _item(db, read_back.id).message
    assert "2 listed order(s) not read — start this read-back again once that run ends" in message


# --- 3. the history's two-digit year --------------------------------------------------------------------
def test_a_two_digit_year_after_an_apostrophe_is_read():
    later = datetime(2027, 3, 1)
    assert sync.parse_date("Sun, 25th Oct \'26", today=later) == datetime(2026, 10, 25)
    assert sync.parse_date("Sat, 20th Oct \'18 - 10:33pm", today=later) == datetime(2018, 10, 20)
    assert sync.parse_date("Mon, 26th Oct ’26", today=later) == datetime(2026, 10, 26)
    assert sync.parse_date("Oct 26", today=later) == datetime(2026, 10, 26)  # a day, not a year


def test_an_old_orders_history_keeps_its_own_year():
    steps, promised, delivered = sync.parse_history(
        ["Order ConfirmedSat, 20th Oct \'18", "DeliveredWed, 31st Oct \'18"], today=datetime(2026, 10, 26)
    )
    assert steps[0] == ("Order Confirmed", datetime(2018, 10, 20))
    assert delivered == datetime(2018, 10, 31)


# --- 4. a promise is never read as last year's ---------------------------------------------------------------
def test_an_expected_date_past_new_year_is_next_years():
    end_of_year = datetime(2026, 12, 29)
    assert sync.parse_date("Sat 2nd Jan", today=end_of_year, expected=True) == datetime(2027, 1, 2)
    assert sync.parse_date("Thu 10th Dec", today=datetime(2026, 10, 26), expected=True) == datetime(2026, 12, 10)
    assert sync.parse_date("Oct 20", today=datetime(2026, 10, 26), expected=True) == datetime(2026, 10, 20)  # overdue
    # An event keeps the old rule: "Dec 28" seen in early January is last December's.
    assert sync.parse_date("Dec 28", today=datetime(2027, 1, 5)) == datetime(2026, 12, 28)


def test_the_row_and_the_history_read_promises_forward():
    end_of_year = datetime(2026, 12, 29)
    assert sync.parse_status_line("Delivery expected by Jan 02", today=end_of_year) == ("Delivery expected", datetime(2027, 1, 2))
    _, promised, _ = sync.parse_history(["DeliveryExpected By Sat 2nd Jan"], today=end_of_year)
    assert promised == datetime(2027, 1, 2)


def test_a_row_that_was_shipped_on_a_day_promises_nothing():
    class _Row:
        def __init__(self, texts):
            self.texts = texts

        async def get_attribute(self, name):
            return "/order_details?order_id=OD1&unit_id=U1"

        def locator(self, selector):
            row = self

            class _Field:
                @property
                def first(self):
                    return self

                async def inner_text(self, timeout=None):
                    text = row.texts.get(selector.removeprefix("xpath="))
                    if text is None:
                        raise TimeoutError(selector)
                    return text

            return _Field()

    shipped = asyncio.run(sync._scrape_row(_Row({S.ORDER_STATUS_IN_ROW: "Shipped on Oct 22"})))
    promised = asyncio.run(sync._scrape_row(_Row({S.ORDER_STATUS_IN_ROW: "Delivery expected by Nov 01"})))

    assert shipped.promised_date is None and shipped.delivery_date is not None
    assert promised.promised_date is not None and promised.promised_date.month == 11


# --- 5. a full read-back over the run limit ---------------------------------------------------------------------
def test_a_full_read_back_over_the_run_limit_takes_the_longest_unread_and_starts(db, monkeypatch):
    calls: list = []
    _run_immediately(monkeypatch, calls)
    monkeypatch.setattr(get_settings(), "max_accounts_per_run", 2)
    app_settings.set_overrides(db, {"sync_every_hours": 6})
    fresh, stale, never = (_account(db, f"{name}@test.com", cookies=False) for name in ("fresh", "stale", "never"))
    _read_back_item(db, fresh, minutes_ago=10)
    _read_back_item(db, stale, minutes_ago=600)

    started = asyncio.run(schedule.tick_once())

    assert started is not None
    assert calls and sorted(calls[0][1]) == sorted([never.id, stale.id])


def test_a_full_read_back_that_cannot_start_does_not_stop_the_follow_ups(db, monkeypatch):
    calls: list = []
    _run_immediately(monkeypatch, calls)
    app_settings.set_overrides(db, {"sync_every_hours": 6})
    account = _account(db, "follow@test.com", cookies=False)
    db.add(Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD5"))
    db.commit()

    def refuse(_db):
        raise RuntimeError("the full read-back could not start")

    monkeypatch.setattr(schedule, "start_scheduled_read_back", refuse)

    started = asyncio.run(schedule.tick_once())

    assert started is not None
    assert calls[0][1] == [account.id]


# --- 6. under the cap the urgent ones go first -----------------------------------------------------------------------
def test_under_the_cap_an_account_just_ordered_on_comes_first(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_accounts_per_run", 2)
    hourly = [_account(db, f"hourly{n}@test.com", cookies=False) for n in (1, 2)]
    for account in hourly:
        db.add(Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id=f"OD{account.id}"))
        db.commit()
        _read_back_item(db, account, minutes_ago=70)
    ordered = _account(db, "ordered@test.com", cookies=False)
    _read_back_item(db, ordered, minutes_ago=20)
    run = Job(type=JobType.order, status=JobStatus.completed)
    db.add(run)
    db.commit()
    order = Order(account_id=ordered.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, job_id=run.id)
    db.add(order)
    db.commit()
    order.created_at = naive_utcnow() - timedelta(minutes=5)
    db.commit()

    accounts, _ = schedule.tracking_due(db)

    assert accounts[0] == ordered.id and len(accounts) == 2


def test_under_the_cap_out_for_delivery_comes_before_the_clock(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_accounts_per_run", 1)
    hourly = _account(db, "clock@test.com", cookies=False)
    db.add(Order(account_id=hourly.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD71"))
    db.commit()
    _read_back_item(db, hourly, minutes_ago=70)
    van = _account(db, "van@test.com", cookies=False)
    order = Order(account_id=van.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD72")
    db.add(order)
    db.commit()
    unit = order.units[0]
    unit.platform_status, unit.synced_at = "Out For Delivery", naive_utcnow()
    db.commit()
    _read_back_item(db, van, minutes_ago=35)

    assert schedule.tracking_due(db)[0] == [van.id]


# --- 7. out for delivery is followed for as long as an open order, not for ever ------------------------------------
def test_a_unit_stuck_out_for_delivery_long_ago_is_not_followed(db):
    account = _account(db, "stuck@test.com", cookies=False)
    order = Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD80")
    db.add(order)
    db.commit()
    order.placed_at = naive_utcnow() - timedelta(days=90)
    unit = order.units[0]
    unit.platform_status, unit.synced_at = "Out For Delivery", naive_utcnow()
    db.commit()
    _read_back_item(db, account, minutes_ago=35)

    assert schedule.tracking_due(db) == ([], "no order is on its way")

    order.placed_at = naive_utcnow() - timedelta(days=10)
    db.commit()
    assert schedule.tracking_due(db)[0] == [account.id]
