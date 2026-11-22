"""Tests for JobTracker: progress persistence, cancellation, live events."""

import asyncio

from app.db.models import (
    Account,
    AccountStatus,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
)
from app.jobs.events import EventBus
from app.jobs.tracker import JobTracker


def _make_job_and_accounts(db, count=2, job_type=JobType.coupon):
    job = Job(type=job_type, status=JobStatus.queued)
    db.add(job)
    accounts = [
        Account(email=f"a{i}@test.com", status=AccountStatus.new) for i in range(count)
    ]
    db.add_all(accounts)
    db.commit()
    # Commit releases the write lock before the tracker opens its own sessions.
    return job, [a.id for a in accounts]


def _items(db, job_id):
    db.expire_all()  # the tracker committed from another session
    return (
        db.query(JobItem)
        .filter(JobItem.job_id == job_id)
        .order_by(JobItem.id)
        .all()
    )


def _reload(db, job):
    db.expire_all()
    return db.get(Job, job.id)


# --- start -------------------------------------------------------------------
def test_start_creates_one_item_per_account(db):
    job, account_ids = _make_job_and_accounts(db, count=3)

    JobTracker(job.id).start(account_ids)

    job = _reload(db, job)
    assert job.status == JobStatus.running
    assert job.total_items == 3
    assert job.done_items == 0
    assert job.failed_items == 0
    assert job.progress == 0

    items = _items(db, job.id)
    assert [i.account_id for i in items] == account_ids
    assert all(i.status == JobItemStatus.pending for i in items)


def test_start_is_idempotent(db):
    job, account_ids = _make_job_and_accounts(db, count=2)
    tracker = JobTracker(job.id)

    tracker.start(account_ids)
    tracker.start(account_ids)  # a re-run must not double the items

    assert len(_items(db, job.id)) == 2
    assert _reload(db, job).total_items == 2


# --- per-item progress -------------------------------------------------------
def test_an_unconfirmed_item_is_counted_apart_and_does_not_fail_the_job(db):
    """An order sent but never confirmed may exist. It must not read as a
    failure (which says retry — and retrying buys it twice) nor as a success."""
    job, account_ids = _make_job_and_accounts(db, count=1, job_type=JobType.order)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    asyncio.run(
        tracker.item_finished(
            account_ids[0], ok=False, message="unconfirmed: check the account", unconfirmed=True
        )
    )
    asyncio.run(tracker.finish())

    job = _reload(db, job)
    assert _items(db, job.id)[0].status == JobItemStatus.unconfirmed
    assert (job.done_items, job.failed_items, job.unconfirmed_items) == (1, 0, 1)
    assert job.status == JobStatus.completed
    assert "1 unconfirmed" in (job.log or "")


def test_item_finished_updates_counts_and_progress(db):
    job, account_ids = _make_job_and_accounts(db, count=4)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    asyncio.run(tracker.item_started(account_ids[0]))
    assert _items(db, job.id)[0].status == JobItemStatus.running

    asyncio.run(tracker.item_finished(account_ids[0], ok=True, message="session saved"))

    job = _reload(db, job)
    assert job.done_items == 1
    assert job.failed_items == 0
    assert job.progress == 25  # 1 of 4

    item = _items(db, job.id)[0]
    assert item.status == JobItemStatus.done
    assert item.message == "session saved"
    assert "session saved" in (job.log or "")

    asyncio.run(tracker.item_finished(account_ids[1], ok=False, message="boom"))
    job = _reload(db, job)
    assert (job.done_items, job.failed_items, job.progress) == (2, 1, 50)
    assert _items(db, job.id)[1].status == JobItemStatus.failed


def test_skipped_item_is_recorded_as_skipped(db):
    job, account_ids = _make_job_and_accounts(db, count=2)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    asyncio.run(
        tracker.item_finished(
            account_ids[0], ok=False, message="no session", skipped=True
        )
    )

    item = _items(db, job.id)[0]
    assert item.status == JobItemStatus.skipped
    assert item.message == "no session"
    reloaded = _reload(db, job)
    assert reloaded.done_items == 1
    # A skip is not a failure — it must not inflate failed_items, which the UI
    # renders in critical red.
    assert reloaded.skipped_items == 1
    assert reloaded.failed_items == 0


def test_all_skipped_batch_completes_rather_than_failing(db):
    """Nothing went wrong — there was simply nothing to do."""
    job, account_ids = _make_job_and_accounts(db, count=3)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    async def run():
        for account_id in account_ids:
            await tracker.item_finished(
                account_id, ok=False, message="no session", skipped=True
            )
        await tracker.finish()

    asyncio.run(run())

    reloaded = _reload(db, job)
    assert reloaded.skipped_items == 3
    assert reloaded.failed_items == 0
    assert reloaded.status == JobStatus.completed


def test_real_failures_with_no_successes_still_fail_the_job(db):
    job, account_ids = _make_job_and_accounts(db, count=2)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    async def run():
        await tracker.item_finished(account_ids[0], ok=False, message="boom")
        await tracker.item_finished(
            account_ids[1], ok=False, message="no session", skipped=True
        )
        await tracker.finish()

    asyncio.run(run())

    reloaded = _reload(db, job)
    assert reloaded.failed_items == 1
    assert reloaded.skipped_items == 1
    assert reloaded.status == JobStatus.failed  # one real failure, nothing succeeded


def test_progress_survives_a_zero_item_job(db):
    job, _ = _make_job_and_accounts(db, count=0)
    tracker = JobTracker(job.id)
    tracker.start([])

    asyncio.run(tracker.finish())

    job = _reload(db, job)
    # No items => nothing failed => completed, not a ZeroDivisionError.
    assert job.status == JobStatus.completed
    assert job.progress == 100


# --- finish ------------------------------------------------------------------
def test_finish_marks_job_failed_when_every_item_failed(db):
    job, account_ids = _make_job_and_accounts(db, count=2)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    for account_id in account_ids:
        asyncio.run(tracker.item_finished(account_id, ok=False, message="nope"))
    asyncio.run(tracker.finish())

    job = _reload(db, job)
    assert job.status == JobStatus.failed
    assert job.progress == 100
    assert job.failed_items == 2
    assert job.finished_at is not None


def test_finish_marks_job_completed_when_mixed(db):
    job, account_ids = _make_job_and_accounts(db, count=2)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    asyncio.run(tracker.item_finished(account_ids[0], ok=True))
    asyncio.run(tracker.item_finished(account_ids[1], ok=False, message="nope"))
    asyncio.run(tracker.finish())

    job = _reload(db, job)
    assert job.status == JobStatus.completed
    assert (job.done_items, job.failed_items, job.progress) == (2, 1, 100)


def test_finish_accepts_an_explicit_status(db):
    job, account_ids = _make_job_and_accounts(db, count=1)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    asyncio.run(tracker.finish(JobStatus.failed))

    assert _reload(db, job).status == JobStatus.failed


# --- timing (plan §6) ----------------------------------------------------------
def test_an_item_is_timed_from_its_start_to_its_finish(db):
    """The run history shows seconds per account: stamped at start and at
    finish, carried on the finish event, and absent for an account that was
    written off without ever being started."""
    job, account_ids = _make_job_and_accounts(db, count=2, job_type=JobType.order)
    job_id = job.id

    async def scenario():
        bus = EventBus()
        tracker = JobTracker(job_id, bus=bus)
        stream = bus.subscribe(job_id)

        tracker.start(account_ids)
        await tracker.item_started(account_ids[0])
        await asyncio.sleep(0.05)
        await tracker.item_finished(account_ids[0], ok=True, message="placed")
        await tracker.item_finished(
            account_ids[1], ok=False, message="no session", skipped=True
        )

        received = [await asyncio.wait_for(stream.__anext__(), timeout=1) for _ in range(4)]
        await stream.aclose()
        return received

    events = asyncio.run(scenario())

    timed, written_off = _items(db, job_id)
    assert timed.started_at is not None and timed.finished_at is not None
    assert timed.finished_at >= timed.started_at
    assert timed.seconds is not None and timed.seconds >= 0.0
    assert written_off.started_at is None and written_off.finished_at is not None
    assert written_off.seconds is None

    finished = [e for e in events if e["type"] == "item_finished"]
    assert finished[0]["seconds"] == timed.seconds
    assert finished[1]["seconds"] is None


def test_a_restarted_item_times_only_its_latest_attempt(db):
    job, account_ids = _make_job_and_accounts(db, count=1)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    asyncio.run(tracker.item_started(account_ids[0]))
    asyncio.run(tracker.item_finished(account_ids[0], ok=False, message="boom"))
    first_end = _items(db, job.id)[0].finished_at
    assert first_end is not None

    asyncio.run(tracker.item_started(account_ids[0]))

    item = _items(db, job.id)[0]
    assert item.finished_at is None  # the stale end goes with the new start
    assert item.started_at is not None and item.started_at >= first_end
    assert item.seconds is None


def test_item_seconds_is_rounded_and_never_negative():
    from datetime import datetime, timedelta

    from app.db.models import item_seconds

    start = datetime(2026, 10, 26, 10, 0, 0)
    assert item_seconds(start, start + timedelta(seconds=14.26)) == 14.3
    assert item_seconds(start, start - timedelta(seconds=5)) == 0.0
    assert item_seconds(None, start) is None
    assert item_seconds(start, None) is None


def test_a_skipped_account_carries_no_time_even_if_it_was_started(db):
    """Every worker marks an account started before its gates; one passed over
    there (no session, no card) is shown with no time, not "0s"."""
    job, account_ids = _make_job_and_accounts(db, count=1, job_type=JobType.order)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    asyncio.run(tracker.item_started(account_ids[0]))
    asyncio.run(tracker.item_finished(account_ids[0], ok=False, message="no session", skipped=True))

    item = _items(db, job.id)[0]
    assert item.status == JobItemStatus.skipped
    assert item.started_at is None and item.finished_at is not None
    assert item.seconds is None


def test_the_stamps_are_written_as_naive_utc():
    """PostgreSQL stores an aware stamp in a plain timestamp column as the
    session's wall-clock time, a naive one as written; so both ends are stamped
    naive UTC and their difference holds under any database zone."""
    from app.db.models import naive_utcnow, utcnow

    stamp = naive_utcnow()
    assert stamp.tzinfo is None
    assert abs((utcnow().replace(tzinfo=None) - stamp).total_seconds()) < 5


# --- cancellation ------------------------------------------------------------
def test_is_cancel_requested_reflects_a_later_db_change(db):
    job, account_ids = _make_job_and_accounts(db, count=2)
    tracker = JobTracker(job.id)
    tracker.start(account_ids)

    assert tracker.is_cancel_requested() is False

    # The API sets this mid-run from its own session.
    job = _reload(db, job)
    job.cancel_requested = True
    db.commit()

    assert tracker.is_cancel_requested() is True


# --- events ------------------------------------------------------------------
def test_events_are_published_in_order(db):
    job, account_ids = _make_job_and_accounts(db, count=2)
    job_id = job.id

    async def scenario():
        bus = EventBus()
        tracker = JobTracker(job_id, bus=bus)
        stream = bus.subscribe(job_id)

        tracker.start(account_ids)
        await tracker.item_started(account_ids[0])
        await tracker.item_finished(account_ids[0], ok=True, message="done")
        await tracker.item_finished(
            account_ids[1], ok=False, message="no session", skipped=True
        )
        await tracker.finish()

        received = []
        for _ in range(5):
            received.append(await asyncio.wait_for(stream.__anext__(), timeout=1))
        await stream.aclose()
        assert bus.subscriber_count(job_id) == 0  # cleaned up on close
        return received

    events = asyncio.run(scenario())

    assert [e["type"] for e in events] == [
        "job_started",
        "item_started",
        "item_finished",
        "item_finished",
        "job_finished",
    ]
    assert events[0]["total_items"] == 2
    assert all(e["job_id"] == job_id for e in events)

    first_item = events[2]
    assert first_item["account_id"] == account_ids[0]
    assert first_item["ok"] is True
    assert first_item["message"] == "done"
    assert first_item["progress"] == 50

    assert events[3]["skipped"] is True
    assert events[4]["status"] == JobStatus.completed.value
    assert events[4]["progress"] == 100


def test_publishing_with_no_subscribers_is_a_no_op(db):
    job, account_ids = _make_job_and_accounts(db, count=1)
    bus = EventBus()
    tracker = JobTracker(job.id, bus=bus)

    # start() publishes from a sync method with no loop running at all.
    tracker.start(account_ids)
    asyncio.run(tracker.item_finished(account_ids[0], ok=True))

    assert _reload(db, job).done_items == 1


def test_two_subscribers_both_receive_events(db):
    job, account_ids = _make_job_and_accounts(db, count=1)
    job_id = job.id

    async def scenario():
        bus = EventBus()
        tracker = JobTracker(job_id, bus=bus)
        first = bus.subscribe(job_id)
        second = bus.subscribe(job_id)
        other_job = bus.subscribe(job_id + 999)
        assert bus.subscriber_count(job_id) == 2

        tracker.start(account_ids)
        a = await asyncio.wait_for(first.__anext__(), timeout=1)
        b = await asyncio.wait_for(second.__anext__(), timeout=1)
        assert bus.subscriber_count(job_id + 999) == 1
        await first.aclose()
        await second.aclose()
        await other_job.aclose()
        return a, b

    a, b = asyncio.run(scenario())
    assert a["type"] == b["type"] == "job_started"
