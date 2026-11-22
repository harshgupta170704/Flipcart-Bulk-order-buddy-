"""Only one sync runs at a time, and the server says so — not just the button."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import sync as sync_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Job, JobStatus, JobType
from app.db.models import Session as SessionModel


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
    row = Account(email="guard@test.com", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    db.add(SessionModel(account_id=row.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    db.commit()
    return row


def _job(db, status: JobStatus, job_type: JobType = JobType.sync) -> Job:
    job = Job(type=job_type, status=status, total_items=1)
    db.add(job)
    db.commit()
    return job


@pytest.mark.parametrize("status", [JobStatus.queued, JobStatus.running])
def test_a_second_sync_is_refused_while_one_is_active(api, db, account, status):
    active = _job(db, status)
    response = api.post("/jobs/sync", json={"account_ids": [account.id]})
    assert response.status_code == 409
    assert f"#{active.id}" in response.json()["detail"]
    assert db.query(Job).count() == 1  # nothing new was created


def test_a_finished_sync_does_not_block_the_next(api, db, account):
    _job(db, JobStatus.completed)
    assert api.post("/jobs/sync", json={"account_ids": [account.id]}).status_code == 201


def test_another_job_type_does_not_block_a_sync(api, db, account):
    _job(db, JobStatus.running, job_type=JobType.order)
    assert api.post("/jobs/sync", json={"account_ids": [account.id]}).status_code == 201
