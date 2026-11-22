"""Agents (step H): the processes that do the browser work, and whether they are up.

Two kinds in this app:

- **The API process**: every order run happens here (card data never goes
  through the queue, PLAN.md §5), with ``max_concurrency`` accounts at once
  and one browser per ``contexts_per_browser`` accounts.
- **Queue workers** (optional, with ``ORDERING_REDIS_URL``): sign-in and
  coupon runs. Each Arq worker writes a health line to Redis every few
  seconds; this reads it.

The fleet (app.core.fleet) is the per-process view: every API process and
queue worker registers itself and writes a heartbeat; a person can test, enable
and disable agents and mark one isolated for test runs.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.core.security import require_user
from app.db.base import get_db
from app.core import fleet
from app.db.models import Agent, Job, JobStatus

router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_user)])

#: Arq's default queue and the key its workers write their health line to.
ARQ_HEALTH_KEY = "arq:queue:health-check"
REDIS_TIMEOUT_S = 2.0


async def _queue_status(redis_url: str) -> dict:
    if not redis_url:
        return {"configured": False, "reachable": None, "health": None, "error": None}
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(redis_url, socket_connect_timeout=REDIS_TIMEOUT_S, socket_timeout=REDIS_TIMEOUT_S)
        try:
            await asyncio.wait_for(client.ping(), REDIS_TIMEOUT_S)
            health = await asyncio.wait_for(client.get(ARQ_HEALTH_KEY), REDIS_TIMEOUT_S)
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 — down is a state to show, not an error
        return {"configured": True, "reachable": False, "health": None, "error": exc.__class__.__name__}
    text = health.decode("utf-8", "replace") if isinstance(health, bytes) else health
    return {"configured": True, "reachable": True, "health": text, "error": None}


@router.get("")
async def agents_status(db: DbSession = Depends(get_db)) -> dict:
    settings = get_settings()
    running = int(db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.running)) or 0)
    queued = int(db.scalar(select(func.count()).select_from(Job).where(Job.status == JobStatus.queued)) or 0)
    return {
        "api": {
            "online": True,
            "max_concurrency": settings.max_concurrency,
            "contexts_per_browser": settings.contexts_per_browser,
            "jobs_running": running,
            "jobs_queued": queued,
        },
        "queue": await _queue_status(settings.redis_url),
    }


# --- the fleet ------------------------------------------------------------------


class AgentPatch(BaseModel):
    enabled: bool | None = None
    isolated: bool | None = None


@router.get("/fleet")
def fleet_status(db: DbSession = Depends(get_db)) -> dict:
    """Every agent with its state, and the counts the Orders page shows."""
    return fleet.summary(db)


@router.post("/fleet/test-all")
def fleet_test_all(db: DbSession = Depends(get_db)) -> dict:
    return fleet.test_all(db)


@router.post("/fleet/enable-all")
def fleet_enable_all(db: DbSession = Depends(get_db)) -> dict:
    return fleet.set_all_enabled(db, True)


@router.post("/fleet/disable-all")
def fleet_disable_all(db: DbSession = Depends(get_db)) -> dict:
    return fleet.set_all_enabled(db, False)


@router.patch("/fleet/{agent_id}")
def fleet_update(agent_id: int, patch: AgentPatch, db: DbSession = Depends(get_db)) -> dict:
    agent = db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if patch.enabled is not None:
        agent.enabled = patch.enabled
    if patch.isolated is not None:
        agent.isolated = patch.isolated
    db.commit()
    return fleet.summary(db)
