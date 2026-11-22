"""Step timing: how long each step of an order or a sign-in took."""

import asyncio
import contextlib

from app.automation.ordering import PAYMENT_COD
from app.core import step_timer
from app.core.step_timer import StepTimer, mark, timing
from app.db.models import (
    Account, AccountStatus, Address, Gst, Job, JobItem, JobStatus, JobType, OrderStatus,
    Session as SessionModel,
)
from app.jobs import tasks


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_summary_lists_each_step_and_the_total():
    clock = Clock()
    timer = StepTimer(clock)
    clock.now = 2.1
    timer.mark("open product")
    clock.now = 8.5
    timer.mark("to checkout")
    assert timer.summary() == "open product 2.1s · to checkout 6.4s · total 8.5s"
    assert timer.slowest() == ("to checkout", 6.4)


def test_finish_skips_an_empty_last_step():
    clock = Clock()
    timer = StepTimer(clock)
    clock.now = 1.0
    timer.mark("a")
    timer.finish("b")
    assert [name for name, _ in timer.steps] == ["a"]


def test_a_mark_outside_a_timed_run_does_nothing():
    mark("anything")  # no timer: no error, nothing kept
    assert step_timer._current.get() is None


def test_accounts_running_at_once_each_keep_their_own_steps():
    async def one(name):
        with timing() as timer:
            await asyncio.sleep(0)
            mark(f"{name} step")
            await asyncio.sleep(0)
            return [n for n, _ in timer.steps]

    async def both():
        return await asyncio.gather(one("a"), one("b"))

    assert asyncio.run(both()) == [["a step"], ["b step"]]


def test_an_order_row_shows_its_step_times(db, monkeypatch):
    gst = Gst(label="ACME", gstin="29ABCDE1234F1Z5", business_name="Acme", is_default=True)
    db.add(gst)
    db.commit()
    db.add(Address(label="w", name="Acme", line1="1 Road", pincode="560001", gst_id=gst.id, is_default=True))
    account = Account(email="t@t.in", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": "x"}], valid=True))
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_kwargs):
        yield None, None

    async def fake_run_order(page, **kwargs):
        mark("open product")
        mark("to checkout")
        return {"status": OrderStatus.failed, "error": "stop here", "payment_mode": PAYMENT_COD}

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_order", fake_run_order)
    monkeypatch.setattr("app.automation.ordering._snapshot", lambda *a, **k: asyncio.sleep(0))

    asyncio.run(tasks.run_order_job(job.id, [account.id], product_link="https://www.flipkart.com/p/itm1", target=1000.0, payment_mode=PAYMENT_COD))

    db.expire_all()
    (item,) = db.query(JobItem).filter(JobItem.job_id == job.id).all()
    assert "⏱ open product" in item.message and "to checkout" in item.message and "total" in item.message
