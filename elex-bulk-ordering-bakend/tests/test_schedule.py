"""Read-backs on a timer (app.jobs.schedule): due only when the setting is on,
nothing is running and the last one is old enough; a start creates the very job
the Sync button does, over the accounts holding a session, marked as started by
the schedule."""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.api import jobs as jobs_api
from app.core import app_settings
from app.db.models import Account, AccountStatus, Job, JobStatus, JobType
from app.jobs import schedule


def _account(db, email: str, status: AccountStatus = AccountStatus.session_valid) -> Account:
    row = Account(email=email, status=status)
    db.add(row)
    db.commit()
    return row


def _run_immediately(monkeypatch, calls: list) -> None:
    """The detached run is replaced by a recorder, and "spawning" runs it at
    once — on the running loop when there is one, else on a fresh one."""

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


def test_off_by_default_and_due_once_on_with_no_read_back_ever(db):
    assert schedule.read_back_due(db) == (False, "automatic read-backs are off")

    app_settings.set_overrides(db, {"sync_every_hours": 6})

    due, why = schedule.read_back_due(db)
    assert due is True and "no read-back has run yet" in why


def test_not_due_while_one_runs_nor_until_the_hours_have_passed(db):
    app_settings.set_overrides(db, {"sync_every_hours": 6})
    running = Job(type=JobType.sync, status=JobStatus.running)
    db.add(running)
    db.commit()

    assert schedule.read_back_due(db) == (False, f"read-back #{running.id} is still running")

    running.status = JobStatus.completed
    running.finished_at = datetime(2026, 10, 26, 10, 0)
    db.commit()

    not_yet, why = schedule.read_back_due(db, now=datetime(2026, 10, 26, 15, 59))
    assert not_yet is False and "2026-10-26T16:00" in why
    assert schedule.read_back_due(db, now=datetime(2026, 10, 26, 16, 1))[0] is True


def test_not_due_while_an_order_run_is_going(db):
    """A read-back over every account would open a second browser on the
    account being ordered on; it waits for the order run, as the tracking
    read-back does."""
    app_settings.set_overrides(db, {"sync_every_hours": 6})
    ordering = Job(type=JobType.order, status=JobStatus.running)
    db.add(ordering)
    db.commit()

    assert schedule.read_back_due(db) == (False, f"order run #{ordering.id} is still going")

    ordering.status = JobStatus.completed
    db.commit()

    assert schedule.read_back_due(db)[0] is True


def test_a_scheduled_read_back_is_the_buttons_job_over_the_signed_in_accounts(db, monkeypatch):
    signed = _account(db, "in@test.com")
    _account(db, "out@test.com", AccountStatus.session_invalid)
    _account(db, "new@test.com", AccountStatus.new)
    calls: list = []
    _run_immediately(monkeypatch, calls)

    job_id = schedule.start_scheduled_read_back(db)

    job = db.get(Job, job_id)
    assert job is not None and job.type == JobType.sync
    assert job.payload["started_by"] == schedule.STARTED_BY
    assert job.payload["scheduled"] is True
    assert calls == [
        (job_id, [signed.id], {"limit_per_account": job.payload["limit_per_account"], "import_unknown": False, "order_ids": None})
    ]


def test_with_no_signed_in_account_nothing_is_started(db, monkeypatch):
    _account(db, "out@test.com", AccountStatus.session_invalid)
    calls: list = []
    _run_immediately(monkeypatch, calls)

    assert schedule.start_scheduled_read_back(db) is None
    assert calls == [] and db.query(Job).count() == 0


def test_a_tick_starts_a_read_back_only_when_due(db, monkeypatch):
    _account(db, "in@test.com")
    calls: list = []
    _run_immediately(monkeypatch, calls)

    async def tick():
        started = await schedule.tick_once()
        await asyncio.sleep(0.01)  # let the spawned recorder run
        return started

    assert asyncio.run(tick()) is None  # off

    app_settings.set_overrides(db, {"sync_every_hours": 1})
    started = asyncio.run(tick())
    assert started is not None and len(calls) == 1

    # The job the tick started is still "running" as far as the table knows:
    # the next tick waits.
    assert asyncio.run(tick()) is None


def test_the_setting_is_bounded(db):
    import pytest

    with pytest.raises(app_settings.SettingsValidationError):
        app_settings.set_overrides(db, {"sync_every_hours": -1})
    with pytest.raises(app_settings.SettingsValidationError):
        app_settings.set_overrides(db, {"sync_every_hours": 500})
    assert app_settings.set_overrides(db, {"sync_every_hours": 0})["sync_every_hours"] == 0
