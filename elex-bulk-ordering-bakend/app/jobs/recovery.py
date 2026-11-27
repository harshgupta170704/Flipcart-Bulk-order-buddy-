"""Jobs a dead process left behind.

A job runs as an asyncio task inside the API process (``api.jobs._spawn``).
The only things that ever move it past ``queued``/``running`` are that task's
own finish and its done-callback — both of which need the process alive. When
the process dies mid-run (uvicorn ``--reload`` picking up a save, ``docker
stop``'s SIGKILL, a crash in the Playwright child) the row stays ``running``
for ever: the dashboard counts a phantom run and, because one sync at a time is
enforced from ``Job.status`` alone (``_refuse_if_active``), no sync can be
started again. Cancel cannot clear it either — cancelling only asks the (gone)
engine to stop.

So the API sweeps at startup. Anything still queued or running at that moment
cannot be alive, with one exception: a login or coupon job that was really
handed to an arq worker (``Job.payload["dispatch"] == "arq"``, written when the
broker took it) runs in another process, and may well have survived the API's
restart. Those are left alone; a login or coupon job that fell back to
in-process (no broker, or the broker refused) is swept like every other.

The worker side closes its own rows too (``arq_worker``): a job that times out,
crashes or is stopped there is marked failed from the worker, so a row never
stays queued or running for want of a process to say otherwise — and since
``_refuse_if_active`` reads exactly that status, nothing blocks the next start.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.db.base import SessionLocal
from app.db.models import Job, JobStatus, JobType, utcnow

log = logging.getLogger("ordering.jobs.recovery")

#: How a run was started, kept in ``Job.payload["dispatch"]`` by the API.
DISPATCH_ARQ = "arq"
DISPATCH_IN_PROCESS = "in_process"

#: Always run inside the API process: an order job carries card data that must
#: never leave the process that received it, a sync drives Playwright here, and
#: a compilation writes its zip into this process's archive store.
IN_PROCESS_TYPES: tuple[JobType, ...] = (JobType.order, JobType.sync, JobType.compile, JobType.balances)
#: May be queued to an arq worker when a broker is configured (api/jobs.py).
QUEUEABLE_TYPES: tuple[JobType, ...] = (JobType.login, JobType.coupon)

_ACTIVE = (JobStatus.queued, JobStatus.running)

#: Appended to the job's log, so the run history says why it ended.
REASON = "Server restarted mid-run; this run did not finish."


def orphan_types(redis_url: str) -> tuple[JobType, ...]:
    """The job types that cannot be running anywhere once this process starts
    (a queueable type may still be alive in a worker — see :func:`may_be_alive_elsewhere`)."""
    if redis_url:
        return IN_PROCESS_TYPES
    return IN_PROCESS_TYPES + QUEUEABLE_TYPES


def may_be_alive_elsewhere(job: Job, redis_url: str) -> bool:
    """Whether a worker, not this process, may be running the job right now:
    a queueable type, a broker configured, and the row saying the broker took it."""
    return bool(redis_url) and job.type in QUEUEABLE_TYPES and (job.payload or {}).get("dispatch") == DISPATCH_ARQ


def _other_online_api_agents(db: DbSession, *, current_agent: str) -> set[str]:
    """Other API hosts sharing this database whose heartbeat is still live."""
    from app.core import fleet
    from app.db.models import Agent

    agents = db.scalars(select(Agent).where(Agent.kind == "api")).all()
    return {
        agent.name
        for agent in agents
        if agent.name != current_agent and fleet.state(agent) == "online"
    }


def _owned_by_other_online_agent(job: Job, *, current_agent: str, other_online: set[str]) -> bool:
    """True when another live API host may be executing this job right now."""
    if not other_online:
        return False
    owner = (job.payload or {}).get("agent")
    if isinstance(owner, str) and owner.strip():
        return owner != current_agent and owner in other_online
    # Legacy/untagged jobs (e.g. login or sync started by a host on older code):
    # while another API host is actively heartbeating, do not kill its live run.
    return True


def fail_orphaned_jobs(
    db: DbSession,
    *,
    redis_url: str | None = None,
    current_agent: str | None = None,
) -> list[int]:
    """Mark every queued/running job that can only have run here as failed.

    Returns the ids it changed. ``redis_url`` defaults to the configured one;
    tests pass it explicitly. When another API host sharing the same database
    is currently online, jobs belonging to that host (or untagged jobs it may
    be running) are left untouched.
    """
    from app.core import fleet

    if redis_url is None:
        redis_url = get_settings().redis_url
    this_agent = current_agent or fleet.agent_name("api")
    other_online = _other_online_api_agents(db, current_agent=this_agent)
    rows = [
        job
        for job in db.scalars(select(Job).where(Job.status.in_(_ACTIVE)).order_by(Job.id)).all()
        if not may_be_alive_elsewhere(job, redis_url)
        and not _owned_by_other_online_agent(job, current_agent=this_agent, other_online=other_online)
    ]
    if not rows:
        return []
    now = utcnow()
    for job in rows:
        job.status = JobStatus.failed
        job.finished_at = now
        job.log = f"{job.log or ''}\n{REASON}".strip()
    db.commit()
    ids = [job.id for job in rows]
    log.warning("marked %d job(s) left running by a previous process as failed: %s", len(ids), ids)
    return ids


def mark_failed(job_id: int, reason: str) -> bool:
    """Close one job's row as failed, from wherever the run died: a worker's
    timeout, a crash, a stop signal. Own session; a row already finished is
    left alone. Returns whether the row was changed."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status not in _ACTIVE:
            return False
        job.status = JobStatus.failed
        job.finished_at = utcnow()
        job.log = f"{job.log or ''}\n{reason}".strip()
        db.commit()
        return True
    except Exception:  # noqa: BLE001 — closing the row is best effort
        log.exception("could not mark job %s failed", job_id)
        return False
    finally:
        db.close()


def recover_orphaned_jobs() -> list[int]:
    """Startup entry point: its own session, and never the reason the app fails to boot."""
    db = SessionLocal()
    try:
        return fail_orphaned_jobs(db)
    except Exception:  # noqa: BLE001 — a failing sweep is logged, not fatal
        log.exception("could not sweep jobs left over from a previous process")
        return []
    finally:
        db.close()
