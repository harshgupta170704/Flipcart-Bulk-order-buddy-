"""A stopping API process starts no new account in a run already going (audit F-32).

``begin_shutdown`` already refused new runs and the drain already waited for
the runs in flight — but a running batch never heard about the stop, so it
kept starting accounts, one after another, until the grace period ran out and
the last of them was cut off mid-order. Now each batch asks before every
account (``app.jobs.stopping``, answered by ``app.api.jobs``); the accounts
already started finish as before, and the rest are passed over and say why.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect

import pytest

from app.api import jobs as jobs_api
from app.automation import ordering as order_flow
from app.core import app_settings
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
    Setting,
)
from app.jobs import stopping, tasks
from app.jobs.tracker import JobTracker
from tests.fakes import FakeContext, FakePage

STOPPED = "not started — the server was stopping"


@pytest.fixture(autouse=True)
def _not_stopping(monkeypatch):
    """Every test starts with the door open, and whatever it does to the flag
    is undone afterwards (monkeypatch restores the module's own value)."""
    monkeypatch.setattr(jobs_api, "_SHUTTING_DOWN", False)


def _accounts(db, count: int) -> list[int]:
    ids = []
    for n in range(count):
        account = Account(email=f"stop{n}@test.com", phone=f"900000000{n}", status=AccountStatus.session_valid)
        db.add(account)
        db.commit()
        db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": str(n)}], valid=True))
        db.commit()
        ids.append(account.id)
    return ids


def _job(db, job_type: JobType = JobType.order) -> Job:
    job = Job(type=job_type, status=JobStatus.queued)
    db.add(job)
    db.commit()
    return job


def _items(db, job_id: int) -> dict[int, JobItem]:
    db.expire_all()
    return {item.account_id: item for item in db.query(JobItem).filter(JobItem.job_id == job_id)}


# --- the question and its answer ----------------------------------------------
def test_the_engine_hears_when_the_api_begins_to_stop():
    assert stopping.is_stopping() is False
    jobs_api.begin_shutdown()
    assert stopping.is_stopping() is True
    jobs_api.allow_runs()  # the next application's start-up
    assert stopping.is_stopping() is False


def test_the_answer_is_read_afresh_not_remembered(monkeypatch):
    """The flag is reset in place — by a start-up, by a test — so a probe
    that captured it once would go on answering the old value."""
    monkeypatch.setattr(jobs_api, "_SHUTTING_DOWN", True)
    assert stopping.is_stopping() is True
    monkeypatch.setattr(jobs_api, "_SHUTTING_DOWN", False)
    assert stopping.is_stopping() is False


def test_no_import_cycle_the_engine_never_imports_the_api_that_starts_it():
    """``app.api.jobs`` imports ``app.jobs.tasks`` to start runs; the stop
    question sits in a module that imports nothing of the app."""

    def imported(module) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    assert not {name for name in imported(stopping) if name == "app" or name.startswith("app.")}
    assert not {name for name in imported(tasks) if name.startswith("app.api.jobs")}


# --- a batch ------------------------------------------------------------------
def test_a_batch_starts_no_new_account_once_the_stop_has_begun(db):
    ids = _accounts(db, 3)
    job = _job(db)
    tracker = JobTracker(job.id)
    tracker.start(ids)
    started: list[int] = []

    async def worker(account_id: int, index: int) -> tasks._Outcome:
        started.append(account_id)
        await tracker.item_started(account_id)
        jobs_api.begin_shutdown()  # the stop arrives while this account is in flight
        await asyncio.sleep(0)
        return tasks._Outcome(True, "done")

    asyncio.run(tasks._run_batch(tracker, ids, worker, 1))

    assert started == [ids[0]]
    items = _items(db, job.id)
    assert items[ids[0]].status == JobItemStatus.done  # the one in flight finished
    for account_id in ids[1:]:
        assert items[account_id].status == JobItemStatus.skipped
        assert items[account_id].message == STOPPED


def test_a_run_the_stop_cut_short_says_so_on_the_job(db):
    """The run still ends "completed"; the job itself now says how many
    accounts were never started, not only each account's own line."""
    ids = _accounts(db, 3)
    job = _job(db)
    tracker = JobTracker(job.id)
    tracker.start(ids)

    async def worker(account_id: int, index: int) -> tasks._Outcome:
        await tracker.item_started(account_id)
        jobs_api.begin_shutdown()
        return tasks._Outcome(True, "done")

    asyncio.run(tasks._run_batch(tracker, ids, worker, 1))

    db.expire_all()
    assert "the server stopped: 2 account(s) were not started — start them again" in (db.get(Job, job.id).log or "")


def test_an_order_waiting_for_a_read_back_is_passed_over_when_the_stop_begins(db, monkeypatch):
    """The order's wait for a read-back of its account ends at the stop, and
    the account is passed over before any claim or browser."""
    ids = _accounts(db, 1)
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    job = _job(db)
    asked: list[bool] = []

    async def wait_while_a_read_back_reads(account_id, *, should_cancel=None, **_):
        jobs_api.begin_shutdown()  # the stop arrives during the wait
        asked.append(bool(should_cancel and should_cancel()))
        return "waited for a read-back of this account"

    opened: list = []
    monkeypatch.setattr(tasks, "wait_for_read_back", wait_while_a_read_back_reads)
    monkeypatch.setattr(tasks, "browser_page", lambda **kwargs: opened.append(kwargs))

    asyncio.run(
        tasks.run_order_job(job.id, ids, product_link="https://www.flipkart.com/p/itm1", payment_mode=tasks.PAYMENT_COD)
    )

    assert asked == [True]  # the wait heard the stop
    assert opened == []
    item = _items(db, job.id)[ids[0]]
    assert (item.status, item.message) == (JobItemStatus.skipped, STOPPED)
    assert "the server stopped: 1 account(s) were not started" in (db.get(Job, job.id).log or "")


def test_the_drain_cancels_the_runs_still_going_when_its_time_runs_out(monkeypatch):
    """Nothing is left running once the stop flag is reset on the way out."""

    async def scenario():
        async def a_run_that_never_ends():
            await asyncio.sleep(3600)

        task = asyncio.create_task(a_run_that_never_ends())
        monkeypatch.setattr(jobs_api, "_BACKGROUND_TASKS", {task})
        left = await jobs_api.drain_background_tasks(timeout_s=0.05)
        return left, task

    left, task = asyncio.run(scenario())

    assert left == 1
    assert task.cancelled()


def test_the_accounts_in_flight_finish_when_the_stop_begins(db):
    ids = _accounts(db, 4)
    job = _job(db)
    tracker = JobTracker(job.id)
    tracker.start(ids)

    async def scenario() -> list[int]:
        in_flight: list[int] = []
        both_running = asyncio.Event()

        async def worker(account_id: int, index: int) -> tasks._Outcome:
            in_flight.append(account_id)
            await tracker.item_started(account_id)
            if len(in_flight) == 2:
                jobs_api.begin_shutdown()
                both_running.set()
            await both_running.wait()
            await asyncio.sleep(0.01)  # still at work after the stop began
            return tasks._Outcome(True, "done")

        await tasks._run_batch(tracker, ids, worker, 2)
        return in_flight

    in_flight = asyncio.run(scenario())

    assert in_flight == ids[:2]
    items = _items(db, job.id)
    assert [items[a].status for a in ids[:2]] == [JobItemStatus.done, JobItemStatus.done]
    assert [items[a].status for a in ids[2:]] == [JobItemStatus.skipped, JobItemStatus.skipped]
    assert {items[a].message for a in ids[2:]} == {STOPPED}


def test_a_cancel_without_a_stop_still_says_cancelled(db):
    ids = _accounts(db, 2)
    job = _job(db)
    tracker = JobTracker(job.id)
    tracker.start(ids)

    async def worker(account_id: int, index: int) -> tasks._Outcome:
        await tracker.item_started(account_id)
        db.get(Job, job.id).cancel_requested = True
        db.commit()
        return tasks._Outcome(True, "done")

    asyncio.run(tasks._run_batch(tracker, ids, worker, 1))

    items = _items(db, job.id)
    assert items[ids[1]].status == JobItemStatus.skipped
    assert items[ids[1]].message == "cancelled"


# --- a whole order run --------------------------------------------------------
def test_an_order_run_places_the_order_in_flight_and_no_other(db, monkeypatch):
    """One account at a time, and the stop arrives while the first account's
    order is going through: that order is placed and recorded; the other two
    accounts open no browser and place nothing."""
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.add(Setting(key=app_settings.SETTINGS_KEY, value={"max_concurrency": 1}))
    db.commit()
    ids = _accounts(db, 3)
    job = _job(db)
    browsers: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        browsers.append(kwargs)
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def fake_run_order(page, *, address, qty=1, target=None, payment_mode=order_flow.PAYMENT_COD, **_):
        jobs_api.begin_shutdown()  # a redeploy, between Pay and the confirmation
        await asyncio.sleep(0)
        return {
            "status": OrderStatus.placed,
            "order_id": "OD338975799606623100",
            "amount": 18207.0,
            "target": target,
            "payment_mode": payment_mode,
            "qty": qty,
        }

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_order", fake_run_order)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            ids,
            product_link="https://www.flipkart.com/p/itm0",
            target=18250.0,
            payment_mode=order_flow.PAYMENT_COD,
        )
    )

    orders = db.query(Order).all()
    assert [(o.account_id, o.status, o.order_id) for o in orders] == [
        (ids[0], OrderStatus.placed, "OD338975799606623100")
    ]
    assert len(browsers) == 1
    items = _items(db, job.id)
    assert items[ids[0]].status == JobItemStatus.done
    assert [items[a].message for a in ids[1:]] == [STOPPED, STOPPED]
    db.expire_all()
    finished = db.get(Job, job.id)
    assert finished.status == JobStatus.completed  # the run ended; nothing failed
    assert (finished.done_items, finished.skipped_items, finished.failed_items) == (3, 2, 0)
