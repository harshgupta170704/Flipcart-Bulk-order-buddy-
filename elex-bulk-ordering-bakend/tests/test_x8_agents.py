"""Step H: GET /agents shows the API process and the optional queue."""

import asyncio

from app.api import agents
from app.config import get_settings
from app.db.models import Job, JobStatus, JobType


def test_without_redis_the_queue_is_not_configured(db, client, auth_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", "")
    db.add(Job(type=JobType.order, status=JobStatus.running))
    db.commit()
    body = client.get("/agents", headers=auth_headers).json()
    assert body["api"]["online"] is True and body["api"]["jobs_running"] == 1
    assert body["queue"]["configured"] is False


def test_an_unreachable_redis_is_shown_as_down_not_raised():
    status = asyncio.run(agents._queue_status("redis://127.0.0.1:9"))
    assert status == {"configured": True, "reachable": False, "health": None, "error": status["error"]}
    assert status["error"]


def test_agents_need_a_sign_in(client):
    assert client.get("/agents").status_code == 401
