"""A process that dies mid-run leaves its job ``running`` for ever — unless the
next start cleans up after it.

Jobs run as asyncio tasks inside the API process. The only things that move one
past queued/running are its own finish and its done-callback, both of which need
that process alive. After ``uvicorn --reload`` picks up a save mid-sync, or
``docker stop``'s SIGKILL, the row stays running: the dashboard counts a phantom
run and, since one sync at a time is enforced from ``Job.status`` alone, no sync
can ever be started again. Cancel cannot clear it — it only asks the (gone)
engine to stop. These tests pin the startup sweep that is the way out.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import sync as sync_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Job, JobStatus, JobType
from app.db.models import Session as SessionModel
from app.jobs.recovery import REASON, fail_orphaned_jobs, mark_failed, orphan_types
from app.main import app


def _job(db, status: JobStatus, job_type: JobType = JobType.sync, log: str | None = None) -> Job:
    job = Job(type=job_type, status=status, total_items=1, log=log)
    db.add(job)
    db.commit()
    return job


def _reload(db, job: Job) -> Job:
    db.expire_all()
    return db.get(Job, job.id)


@pytest.mark.parametrize("status", [JobStatus.queued, JobStatus.running])
def test_an_orphaned_in_process_job_is_marked_failed(db, status):
    job = _job(db, status, log="started")

    assert fail_orphaned_jobs(db, redis_url="") == [job.id]

    row = _reload(db, job)
    assert row.status == JobStatus.failed
    assert row.finished_at is not None
    # The run history says why it ended, after whatever the run itself logged.
    assert row.log == f"started\n{REASON}"


def test_finished_jobs_are_left_alone(db):
    done = _job(db, JobStatus.completed)
    failed = _job(db, JobStatus.failed, log="crashed")

    assert fail_orphaned_jobs(db, redis_url="") == []

    assert _reload(db, done).status == JobStatus.completed
    assert _reload(db, failed).log == "crashed"


def test_the_sweep_is_idempotent(db):
    _job(db, JobStatus.running)
    assert fail_orphaned_jobs(db, redis_url="")
    assert fail_orphaned_jobs(db, redis_url="") == []


def test_without_a_broker_every_type_runs_here_and_is_swept(db):
    jobs = {job_type: _job(db, JobStatus.running, job_type=job_type) for job_type in JobType}
    assert set(fail_orphaned_jobs(db, redis_url="")) == {job.id for job in jobs.values()}


def test_with_a_broker_only_a_run_the_broker_took_may_be_alive_in_a_worker(db):
    """A login or coupon job the broker took (its row says so) may be running
    in an arq worker that survived the API's restart. One that fell back to
    in-process — the broker down at start time, or the row written before the
    mark existed — died with this process like every other, and must not be
    left standing: an open row refuses every later start of its type."""
    jobs = {job_type: _job(db, JobStatus.running, job_type=job_type) for job_type in JobType}
    taken = _job(db, JobStatus.queued, job_type=JobType.login)
    taken.payload = {"dispatch": "arq"}
    db.commit()

    swept = set(fail_orphaned_jobs(db, redis_url="redis://localhost:6379/0"))

    assert swept == {job.id for job in jobs.values()}  # the unmarked login/coupon rows included
    assert _reload(db, taken).status == JobStatus.queued
    assert _reload(db, jobs[JobType.login]).status == JobStatus.failed
    assert _reload(db, jobs[JobType.coupon]).status == JobStatus.failed


def test_a_worker_closes_its_own_row_when_a_run_dies_there(db):
    """From the arq side: a body that raises, arq's timeout or a stop signal
    (CancelledError) all close the row as failed, with the reason, and the
    error still reaches arq. A row already finished is left as it is."""
    import asyncio

    from app.jobs import arq_worker

    job = _job(db, JobStatus.running, job_type=JobType.login, log="started")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("browser gone")

    original = arq_worker.run_login_job
    arq_worker.run_login_job = boom
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(arq_worker.login_job({}, job.id, [1]))
    finally:
        arq_worker.run_login_job = original
    row = _reload(db, job)
    assert row.status == JobStatus.failed
    assert "login run stopped in the worker: RuntimeError: browser gone" in row.log

    cancelled = _job(db, JobStatus.running, job_type=JobType.coupon)

    async def stopped(*_args, **_kwargs):
        raise asyncio.CancelledError()

    original = arq_worker.run_coupon_job
    arq_worker.run_coupon_job = stopped
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(arq_worker.coupon_job({}, cancelled.id, [1], "SAVE"))
    finally:
        arq_worker.run_coupon_job = original
    row = _reload(db, cancelled)
    assert row.status == JobStatus.failed
    assert "coupon run stopped in the worker: CancelledError" in row.log

    done = _job(db, JobStatus.completed, job_type=JobType.login, log="fine")
    assert mark_failed(done.id, "late") is False
    assert _reload(db, done).log == "fine"


def test_orphan_types_follow_the_broker_setting():
    assert set(orphan_types("")) == set(JobType)
    assert set(orphan_types("redis://broker")) == {JobType.order, JobType.sync, JobType.compile, JobType.balances}


# --- the lockout the sweep exists for -----------------------------------------
@pytest.fixture
def api(monkeypatch) -> TestClient:
    application = FastAPI()
    application.include_router(sync_api.router)
    # Never actually start a browser from a test: the guard is decided before
    # anything is spawned, and the allowed path only needs its 201.
    monkeypatch.setattr(sync_api, "_spawn", lambda factory, job_id: None)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def account(db) -> Account:
    row = Account(email="orphan@test.com", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    db.add(SessionModel(account_id=row.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    db.commit()
    return row


def test_the_sweep_unblocks_the_next_sync(api, db, account):
    """409 while the phantom stands, 201 once it is swept."""
    _job(db, JobStatus.running)
    assert api.post("/jobs/sync", json={"account_ids": [account.id]}).status_code == 409

    fail_orphaned_jobs(db, redis_url="")

    assert api.post("/jobs/sync", json={"account_ids": [account.id]}).status_code == 201


def test_cancelling_the_phantom_does_not_clear_it_but_a_restart_does(db):
    """Cancel only sets a flag for an engine that no longer exists; the app's
    startup is where the row is actually settled, so a restart is the cure."""
    job = _job(db, JobStatus.running)
    job.cancel_requested = True
    db.commit()
    assert _reload(db, job).status == JobStatus.running

    with TestClient(app):  # runs the lifespan: init_db, the sweep, the security check
        pass

    assert _reload(db, job).status == JobStatus.failed


def test_another_online_api_agent_protects_its_running_jobs(db):
    """When two laptops share PostgreSQL, starting one must not kill the
    other's active job — whether tagged with the other host or untagged."""
    from app.db.models import Agent, naive_utcnow

    db.add(
        Agent(
            name="api@other-macbook",
            kind="api",
            host="other-macbook",
            enabled=True,
            isolated=False,
            last_seen_at=naive_utcnow(),
        )
    )
    db.commit()

    other_tagged = _job(db, JobStatus.running, job_type=JobType.order)
    other_tagged.payload = {"agent": "api@other-macbook"}
    untagged = _job(db, JobStatus.running, job_type=JobType.login)
    mine = _job(db, JobStatus.running, job_type=JobType.order)
    mine.payload = {"agent": "api@this-laptop"}
    db.commit()

    swept = fail_orphaned_jobs(db, redis_url="", current_agent="api@this-laptop")

    assert swept == [mine.id]
    assert _reload(db, mine).status == JobStatus.failed
    assert _reload(db, other_tagged).status == JobStatus.running
    assert _reload(db, untagged).status == JobStatus.running

