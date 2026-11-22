"""Reading one order by id — for ids handed in to track, or older than the
recent list's window — and the run and endpoint that ask for it."""

import asyncio
import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import sync as sync_api
from app.automation import sync
from app.core import selectors as S
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Job, JobItem, JobStatus, JobType, Order, OrderStatus
from app.db.models import Session as SessionModel
from app.jobs import sync_tasks
from tests.fakes import FakeContext, FakePage
from tests.test_sync_scrape import DETAIL_0, DETAIL_1, _detail_url, _SyncPage

ORDER = "OD113741449375753000"
UNIT_A = "/order_details?order_id=OD113741449375753000&item_id=IT1&unit_id=UNA"
UNIT_B = "/order_details?order_id=OD113741449375753000&item_id=IT2&unit_id=UNB"


# --- the scrape ----------------------------------------------------------------------
def test_detail_url_opens_the_order_by_id_alone():
    assert sync.detail_url("OD1") == "https://www.flipkart.com/order_details?order_id=OD1"


def test_an_order_page_with_unit_links_yields_one_observation_per_unit():
    page = _SyncPage(
        {
            sync.detail_url(ORDER): DETAIL_0,
            _detail_url(UNIT_A): DETAIL_0,
            _detail_url(UNIT_B): DETAIL_1,
        }
    )
    page.unit_links = [
        _detail_url(UNIT_A),
        _detail_url(UNIT_B),
        _detail_url(UNIT_A),  # a repeat
        "https://www.flipkart.com/order_details?order_id=OTHER&unit_id=X",  # another order's
    ]
    page.counts[S.ORDER_TIMELINE_STEPS] = 3

    units = asyncio.run(sync.scrape_order(page, ORDER))

    assert [(u.order_id, u.unit_id, u.item_id) for u in units] == [
        (ORDER, "UNA", "IT1"),
        (ORDER, "UNB", "IT2"),
    ]
    assert units[0].seller == "Flashtech Retail"  # from its own page
    assert units[1].platform_status == "Shipped"  # the timeline's last dated step


def test_an_order_page_without_unit_links_is_read_as_one_unit():
    page = _SyncPage({sync.detail_url(ORDER): DETAIL_0})
    units = asyncio.run(sync.scrape_order(page, ORDER))
    assert len(units) == 1
    assert (units[0].order_id, units[0].unit_id, units[0].seller) == (ORDER, None, "Flashtech Retail")


def test_a_login_bounce_reads_nothing():
    page = _SyncPage({})
    page.redirect_on[sync.detail_url(ORDER)] = "https://www.flipkart.com/login?ret=x"
    assert asyncio.run(sync.scrape_order(page, ORDER)) == []


# --- the run ---------------------------------------------------------------------------
def _account(db, email: str) -> Account:
    row = Account(email=email, status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    db.add(SessionModel(account_id=row.id, cookies=[{"name": "SN", "value": email}], valid=True))
    db.commit()
    return row


def test_a_run_over_listed_ids_reads_only_those_under_their_accounts(db, monkeypatch):
    mine = _account(db, "mine@test.com")
    theirs = _account(db, "theirs@test.com")
    nobody = _account(db, "nobody@test.com")
    db.add_all(
        [
            Order(account_id=mine.id, order_id="OD1000000001", status=OrderStatus.placed),
            Order(account_id=mine.id, order_id="OD1000000002", status=OrderStatus.placed),
            Order(account_id=theirs.id, order_id="OD1000000003", status=OrderStatus.placed),
        ]
    )
    job = Job(type=JobType.sync, status=JobStatus.queued)
    db.add(job)
    db.commit()

    asked: list[str] = []

    async def fake_scrape_order(page, order_id):
        asked.append(order_id)
        return [sync.ScrapedOrder(order_id=order_id, unit_id=f"U{order_id[-1]}", platform_status="Delivered")]

    async def no_list(*args, **kwargs):
        raise AssertionError("the recent list must not be read for a by-id run")

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    monkeypatch.setattr(sync_tasks, "scrape_order", fake_scrape_order)
    monkeypatch.setattr(sync_tasks, "scrape_orders", no_list)
    monkeypatch.setattr(sync_tasks, "browser_page", fake_browser_page)

    asyncio.run(
        sync_tasks.run_sync_job(
            job.id,
            [mine.id, theirs.id, nobody.id],
            order_ids=[" od1000000001 ", "OD1000000003", "OD9999999999"],
        )
    )

    assert sorted(asked) == ["OD1000000001", "OD1000000003"]  # not OD…02, not the unknown one
    db.expire_all()
    one = db.query(Order).filter(Order.order_id == "OD1000000001").one()
    assert one.units[0].platform_status == "Delivered"
    skipped = db.query(JobItem).filter(JobItem.job_id == job.id, JobItem.account_id == nobody.id).one()
    assert "none of the listed orders" in (skipped.message or "")


# --- the endpoint ----------------------------------------------------------------------
@pytest.fixture
def api(monkeypatch) -> TestClient:
    application = FastAPI()
    application.include_router(sync_api.router)
    monkeypatch.setattr(sync_api, "_spawn", lambda factory, job_id: None)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_listed_ids_pick_their_own_accounts(api, db):
    account = _account(db, "byid@test.com")
    db.add(Order(account_id=account.id, order_id="OD5555555555", status=OrderStatus.placed))
    db.commit()

    response = api.post("/jobs/sync", json={"order_ids": [" od5555555555 "]})

    assert response.status_code == 201
    job = db.get(Job, response.json()["id"])
    assert job.payload["order_ids"] == ["OD5555555555"]
    assert job.payload["selected_by"] == "order_ids"
    assert job.total_items == 1


def test_ids_the_ledger_does_not_hold_are_refused(api, db):
    _account(db, "byid@test.com")
    response = api.post("/jobs/sync", json={"order_ids": ["OD0000000000"]})
    assert response.status_code == 400
    assert "upload them first" in response.json()["detail"]
