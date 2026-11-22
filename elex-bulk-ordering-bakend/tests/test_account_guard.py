"""One account, one run (app.jobs.account_guard, 26 Oct 2026).

An order run and a read-back never work one account at once. The order goes
first: a read-back passes over an account an order run is using, one already
reading stops at its next page when an order starts there, and the order
waits — briefly — for that page to end.
"""

import asyncio
import contextlib
from datetime import timedelta

import pytest

from app.automation import sync
from app.automation.ordering import PAYMENT_COD
from app.core import invoices
from app.core import selectors as S
from app.db.base import SessionLocal
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Gst,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
    naive_utcnow,
)
from app.jobs import account_guard, schedule, sync_tasks, tasks
from tests.fakes import FakeContext, FakePage
from tests.test_sync_scrape import HREF_0, HREF_1, _detail_url, _page


# --- helpers -----------------------------------------------------------------
def _account(db, email):
    account = Account(email=email, status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": email}], valid=True))
    db.commit()
    return account


def _run(db, job_type, account, *, item=JobItemStatus.running, status=JobStatus.running):
    """A run of ``job_type`` whose item for ``account`` is in state ``item``."""
    job = Job(type=job_type, status=status)
    db.add(job)
    db.commit()
    row = JobItem(job_id=job.id, account_id=account.id, status=item)
    db.add(row)
    db.commit()
    return job, row


def _queued(db, job_type):
    job = Job(type=job_type, status=JobStatus.queued)
    db.add(job)
    db.commit()
    return job


def _items(db, job_id):
    db.expire_all()
    return {row.account_id: row for row in db.query(JobItem).filter(JobItem.job_id == job_id)}


def _set(model, pk, **values):
    """Change a row from outside the test's session, as another run would."""
    with SessionLocal() as other:
        row = other.get(model, pk)
        for name, value in values.items():
            setattr(row, name, value)
        other.commit()


def _status_of(model, pk):
    with SessionLocal() as other:
        return other.get(model, pk).status


def _fake_browser(opened):
    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        opened.append(kwargs)
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    return fake_browser_page


@pytest.fixture
def fast_wait(monkeypatch):
    monkeypatch.setattr(account_guard, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(account_guard, "READ_BACK_WAIT_SECONDS", 5.0)


@pytest.fixture
def address_book(db):
    gst = Gst(label="ACME", gstin="29ABCDE1234F1Z5", business_name="Acme Traders", is_default=True)
    db.add(gst)
    db.commit()
    db.add(Address(label="works", name="Acme Traders", line1="1 Industrial Area", pincode="560001", gst_id=gst.id, is_default=True))
    db.commit()


# --- who is working an account -------------------------------------------------
def test_run_on_account_names_only_a_run_working_that_account_now(db):
    mine = _account(db, "mine@test.com")
    other = _account(db, "other@test.com")
    order_run, item = _run(db, JobType.order, mine)

    assert account_guard.run_on_account(mine.id, JobType.order) == order_run.id
    assert account_guard.run_on_account(other.id, JobType.order) is None
    assert account_guard.run_on_account(mine.id, JobType.sync) is None

    _set(JobItem, item.id, status=JobItemStatus.done)
    assert account_guard.run_on_account(mine.id, JobType.order) is None

    # A run that ended with an item still marked running (a restart cut it
    # short) is working nothing.
    _run(db, JobType.order, mine, status=JobStatus.failed)
    assert account_guard.run_on_account(mine.id, JobType.order) is None


# --- the read-back gives way -------------------------------------------------
def test_a_read_back_passes_over_an_account_an_order_run_is_using(db, monkeypatch):
    account = _account(db, "busy@test.com")
    order_run, _item = _run(db, JobType.order, account)
    read_back = _queued(db, JobType.sync)
    opened: list = []
    monkeypatch.setattr(sync_tasks, "browser_page", _fake_browser(opened))

    async def never(*args, **kwargs):
        raise AssertionError("an account an order run is using must not be read")

    monkeypatch.setattr(sync_tasks, "scrape_orders", never)

    asyncio.run(sync_tasks.run_sync_job(read_back.id, [account.id]))

    item = _items(db, read_back.id)[account.id]
    assert item.status == JobItemStatus.skipped
    assert f"order run #{order_run.id} is using this account" in item.message
    assert opened == []


def test_a_read_back_stops_at_its_next_page_when_an_order_starts_on_the_account(db, monkeypatch):
    account = _account(db, "mid@test.com")
    order = Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD1")
    db.add(order)
    db.commit()
    read_back = _queued(db, JobType.sync)
    monkeypatch.setattr(sync_tasks, "browser_page", _fake_browser([]))
    started: list[int] = []

    async def scrape_orders(page, *, limit=20, open_details=True, should_stop=None):
        read = [sync.ScrapedOrder(order_id="OD1", unit_id="U1", platform_status="Shipped")]
        assert should_stop() is False  # nothing else on the account yet
        order_run, _item = _run(db, JobType.order, account)  # the owner starts an order
        started.append(order_run.id)
        assert should_stop() is True
        return read

    fetched: list = []

    async def fetch_missing(context, account_id, scraped):
        fetched.append(account_id)
        return {}

    monkeypatch.setattr(sync_tasks, "scrape_orders", scrape_orders)
    monkeypatch.setattr(invoices, "fetch_missing", fetch_missing)

    asyncio.run(sync_tasks.run_sync_job(read_back.id, [account.id]))

    item = _items(db, read_back.id)[account.id]
    assert item.status == JobItemStatus.skipped
    assert f"stopped early so order run #{started[0]} could use this account" in item.message
    assert "1 matched" in item.message  # what was read stands
    db.expire_all()
    assert db.get(Order, order.id).synced_at is not None
    assert fetched == []  # no invoice is fetched on an account given over


def test_scrape_orders_opens_no_further_page_once_told_to_stop():
    page = _page()
    orders = asyncio.run(sync.scrape_orders(page, should_stop=lambda: True))

    # The list's rows are kept; no order page is opened, and the browser is
    # left where it is rather than taken back to the list.
    assert [o.order_id for o in orders] == ["OD111", "OD222", "OD333"]
    assert page.goto_urls == [S.ORDERS_URL]


def test_scrape_orders_stops_between_pages():
    page = _page()
    answers = iter([False, True])
    asyncio.run(sync.scrape_orders(page, should_stop=lambda: next(answers)))

    assert _detail_url(HREF_0) in page.goto_urls
    assert _detail_url(HREF_1) not in page.goto_urls


def test_a_read_back_by_id_stops_between_orders_too(monkeypatch):
    asked: list[str] = []

    async def scrape_order(page, order_id):
        asked.append(order_id)
        return []

    monkeypatch.setattr(sync_tasks, "scrape_order", scrape_order)
    answers = iter([False, True])
    asyncio.run(sync_tasks._scrape_listed(None, ["OD1", "OD2"], should_stop=lambda: next(answers)))

    assert asked == ["OD1"]


def test_a_passed_over_account_is_still_to_be_read_back(db):
    account = _account(db, "later@test.com")
    db.add(Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD9"))
    db.commit()
    _run(db, JobType.sync, account, item=JobItemStatus.skipped, status=JobStatus.completed)
    with SessionLocal() as other:
        other.query(JobItem).update({"finished_at": naive_utcnow() - timedelta(minutes=1)})
        other.commit()

    assert schedule._last_read_backs(db, [account.id]) == {}
    assert schedule.tracking_due(db)[0] == [account.id]

    _run(db, JobType.sync, account, item=JobItemStatus.done, status=JobStatus.completed)
    with SessionLocal() as other:
        other.query(JobItem).filter(JobItem.status == JobItemStatus.done).update({"finished_at": naive_utcnow()})
        other.commit()
    assert account.id in schedule._last_read_backs(db, [account.id])
    assert schedule.tracking_due(db)[0] == []


# --- the order waits for the page to end ----------------------------------------
def _order_flow(opened_with: list):
    async def run_order(page, *, product_link, address, gst, card=None, qty=1, target=None, payment_mode=None, **_extra):
        return {"status": OrderStatus.placed, "amount": 500.0, "target": target, "order_id": "OD-NEW", "qty": qty, "payment_mode": payment_mode}

    return run_order


def _order_run(db, monkeypatch, account, opened):
    job = _queued(db, JobType.order)

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        # What the account looked like the moment the order's browser opened.
        opened.append(account_guard.run_on_account(account.id, JobType.sync))
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_order", _order_flow(opened))
    return job, tasks.run_order_job(job.id, [account.id], product_link="https://www.flipkart.com/p", target=1000.0, payment_mode=PAYMENT_COD)


async def _once_the_order_is_running(job_id, then):
    for _ in range(500):
        with SessionLocal() as other:
            item = other.query(JobItem).filter(JobItem.job_id == job_id).one_or_none()
            if item is not None and item.status == JobItemStatus.running:
                break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.03)
    then()


def _log(job_id):
    with SessionLocal() as other:
        return other.get(Job, job_id).log or ""


def test_an_order_waits_for_a_read_back_reading_its_account(db, monkeypatch, fast_wait, address_book):
    account = _account(db, "wait@test.com")
    read_back, reading = _run(db, JobType.sync, account)
    opened: list = []
    job, run = _order_run(db, monkeypatch, account, opened)

    async def scenario():
        finish = lambda: _set(JobItem, reading.id, status=JobItemStatus.done, finished_at=naive_utcnow())
        await asyncio.gather(run, _once_the_order_is_running(job.id, finish))

    asyncio.run(scenario())

    assert opened == [None]  # the browser opened only once the read-back had let go
    assert f"for read-back #{read_back.id} to stop reading it first" in _log(job.id)
    assert _items(db, job.id)[account.id].status == JobItemStatus.done


def test_an_order_goes_ahead_when_a_read_back_will_not_let_go(db, monkeypatch, address_book):
    monkeypatch.setattr(account_guard, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(account_guard, "READ_BACK_WAIT_SECONDS", 0.05)
    account = _account(db, "stuck@test.com")
    read_back, _reading = _run(db, JobType.sync, account)
    opened: list = []
    job, run = _order_run(db, monkeypatch, account, opened)

    asyncio.run(run)

    assert opened == [read_back.id]  # still reading, and the order went ahead
    assert "ordered anyway" in _log(job.id)
    assert _items(db, job.id)[account.id].status == JobItemStatus.done


def test_a_run_cancelled_while_it_waits_orders_nothing(db, monkeypatch, fast_wait, address_book):
    account = _account(db, "cancel@test.com")
    _run(db, JobType.sync, account)
    opened: list = []
    job, run = _order_run(db, monkeypatch, account, opened)

    async def scenario():
        await asyncio.gather(run, _once_the_order_is_running(job.id, lambda: _set(Job, job.id, cancel_requested=True)))

    asyncio.run(scenario())

    item = _items(db, job.id)[account.id]
    assert item.status == JobItemStatus.skipped
    assert item.message == "cancelled"
    assert opened == []
    assert db.query(Order).count() == 0


def test_an_order_with_nothing_reading_its_account_does_not_wait(db, monkeypatch, address_book):
    monkeypatch.setattr(account_guard, "POLL_SECONDS", 30.0)  # a wait would hang the test
    account = _account(db, "free@test.com")
    opened: list = []
    job, run = _order_run(db, monkeypatch, account, opened)

    asyncio.run(run)

    assert opened == [None]
    assert "read-back" not in _log(job.id)
