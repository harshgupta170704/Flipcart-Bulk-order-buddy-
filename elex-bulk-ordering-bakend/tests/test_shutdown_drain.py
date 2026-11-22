"""Stopping the API process with runs in flight (audit F-32, 25 Oct 2026): no
new run may start, and the ones running get a grace period to finish before
the loop goes down with them."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.api import jobs as jobs_api


@pytest.fixture(autouse=True)
def _not_shutting_down(monkeypatch):
    monkeypatch.setattr(jobs_api, "_SHUTTING_DOWN", False)
    yield
    monkeypatch.setattr(jobs_api, "_SHUTTING_DOWN", False)


def test_the_drain_waits_for_a_run_that_finishes_in_time(monkeypatch):
    monkeypatch.setattr(jobs_api, "_on_task_done", lambda task, job_id: None)
    finished: list[int] = []

    async def scenario():
        async def run():
            await asyncio.sleep(0.05)
            finished.append(1)

        jobs_api._spawn(run, job_id=1)
        return await jobs_api.drain_background_tasks(timeout_s=2.0)

    assert asyncio.run(scenario()) == 0
    assert finished == [1]


def test_the_drain_gives_up_after_the_grace_period_and_says_so(monkeypatch):
    monkeypatch.setattr(jobs_api, "_on_task_done", lambda task, job_id: None)

    async def scenario():
        async def run():
            await asyncio.sleep(10)

        task = jobs_api._spawn(run, job_id=2)
        still = await jobs_api.drain_background_tasks(timeout_s=0.1)
        task.cancel()
        return still

    assert asyncio.run(scenario()) == 1


def test_nothing_new_starts_once_the_process_is_stopping(monkeypatch):
    monkeypatch.setattr(jobs_api, "_on_task_done", lambda task, job_id: None)

    async def scenario():
        jobs_api.begin_shutdown()

        async def run():
            return None

        with pytest.raises(HTTPException) as refused:
            jobs_api._spawn(run, job_id=3)
        return refused.value.status_code

    assert asyncio.run(scenario()) == 503
