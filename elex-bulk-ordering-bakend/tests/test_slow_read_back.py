"""A slow read-back says so on its item (26 Oct 2026: one took 19 minutes,
the usual being about one, and only the server's terminal said why)."""

import asyncio
import contextlib

from app.automation import sync
from app.db.models import Account, AccountStatus, Job, JobItem, JobStatus, JobType, Session as SessionModel
from app.jobs import sync_tasks
from tests.fakes import FakeContext, FakePage


def _read(db, monkeypatch):
    account = Account(email="slow@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": "slow"}], valid=True))
    job = Job(type=JobType.sync, status=JobStatus.queued)
    db.add(job)
    db.commit()

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def scrape_orders(page, *, limit=20, open_details=True, should_stop=None):
        return [sync.ScrapedOrder(order_id="OD1", unit_id="U1", platform_status="Shipped")]

    monkeypatch.setattr(sync_tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(sync_tasks, "scrape_orders", scrape_orders)
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))
    db.expire_all()
    return db.query(JobItem).filter(JobItem.job_id == job.id).one().message


def test_a_read_that_took_minutes_says_so(db, monkeypatch):
    monkeypatch.setattr(sync_tasks, "SLOW_READ_SECONDS", 0)
    assert "slow: Flipkart's pages took" in _read(db, monkeypatch)


def test_a_normal_read_says_nothing_of_it(db, monkeypatch):
    assert "slow" not in _read(db, monkeypatch)
