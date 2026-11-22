"""Optional Arq/Redis adapter — scale login and coupon work out of the API.

**Order jobs are deliberately NOT routed through Redis.** Enqueueing an order
would mean serialising the run's card details into the queue, where they would
be written to Redis storage and possibly to its logs. PLAN.md §5 says card data
is held in process memory for the duration of one run and nowhere else, so only
**login and coupon jobs are queue-safe**; order jobs always run in the process
that received the card upload. ``enqueue_job`` refuses the ``order`` type to
make that impossible to get wrong by accident.

This module is entirely optional: nothing in ``app.jobs`` imports it, the core
engine runs on plain asyncio, and importing it with ``redis_url`` blank is safe
— no connection is attempted at import time.

Run a worker (once Redis is configured):

    arq app.jobs.arq_worker.WorkerSettings
"""

from __future__ import annotations

import logging

import contextlib
from collections.abc import AsyncIterator

from app.config import get_settings
from app.core.logging_setup import configure_logging
from app.db.models import JobType
from app.jobs.recovery import mark_failed
from app.jobs.tasks import run_coupon_job, run_login_job

log = logging.getLogger("ordering.jobs.arq")


@contextlib.asynccontextmanager
async def _closing_the_row(job_id: int, what: str) -> AsyncIterator[None]:
    """Whatever ends a worker's run — the job body raising, arq's timeout or
    a stop signal (both arrive as CancelledError) — the job's row is closed as
    failed with the reason, so it never stays queued or running for want of a
    process to say so; the API's start guard reads that status, and a row left
    open would refuse every later run of the type. Re-raised afterwards, so
    arq still sees the failure."""
    try:
        yield
    except BaseException as exc:
        name = type(exc).__name__
        reason = f"{what} stopped in the worker: {name}" + (f": {exc}" if str(exc) and name != "CancelledError" else "")
        mark_failed(job_id, reason)
        raise

try:  # arq is an optional extra; the core engine must import without it
    from arq.connections import RedisSettings

    ARQ_AVAILABLE = True
except ImportError:  # pragma: no cover - arq is in requirements.txt
    RedisSettings = None  # type: ignore[assignment]
    ARQ_AVAILABLE = False

# Job types that may cross a queue boundary (see the module docstring).
QUEUE_SAFE_TYPES: frozenset[JobType] = frozenset({JobType.login, JobType.coupon})


def queue_enabled() -> bool:
    """True when a broker is configured. Blank redis_url => in-process only."""
    return bool(ARQ_AVAILABLE and get_settings().redis_url)


def build_redis_settings(redis_url: str | None = None):
    """Build ``RedisSettings`` without connecting.

    A blank URL yields arq's localhost defaults so the module (and
    ``WorkerSettings``) still import cleanly on a machine with no Redis.
    """
    if not ARQ_AVAILABLE:  # pragma: no cover
        return None
    url = get_settings().redis_url if redis_url is None else redis_url
    if not url:
        return RedisSettings()
    return RedisSettings.from_dsn(url)


# --- task wrappers -----------------------------------------------------------
# Arq passes its own context dict as the first argument; the job bodies do not
# need it, so it is accepted and ignored.
async def login_job(
    ctx: dict, job_id: int, account_ids: list[int], force: bool = False
) -> None:
    async with _closing_the_row(job_id, "login run"):
        await run_login_job(job_id, account_ids, force=force)


async def coupon_job(
    ctx: dict, job_id: int, account_ids: list[int], coupon_code: str
) -> None:
    async with _closing_the_row(job_id, "coupon run"):
        await run_coupon_job(job_id, account_ids, coupon_code)


# Name -> wrapper, for enqueueing by job type.
TASK_NAMES: dict[JobType, str] = {
    JobType.login: "login_job",
    JobType.coupon: "coupon_job",
}


async def enqueue_job(job_type: JobType, *args, **kwargs):
    """Enqueue a queue-safe job, or raise if it is not one.

    Returns None when no broker is configured, which tells the caller to run
    the job in-process instead.
    """
    if job_type not in QUEUE_SAFE_TYPES:
        raise ValueError(
            f"{getattr(job_type, 'value', job_type)} jobs are never queued: card "
            "details must not be serialised into a broker (PLAN.md §5)"
        )
    if not queue_enabled():
        log.info("no redis_url configured; caller should run the job in-process")
        return None

    from arq import create_pool  # imported lazily: this is the connecting path

    pool = await create_pool(build_redis_settings())
    try:
        return await pool.enqueue_job(TASK_NAMES[job_type], *args, **kwargs)
    finally:
        await pool.aclose()


async def on_startup(ctx: dict) -> None:
    """Arq configures its own loggers and nothing else, exactly like uvicorn:
    without this the worker's INFO lines (what each login did) go nowhere.
    It also registers this worker in the agent fleet and keeps its heartbeat."""
    configure_logging()
    import asyncio

    from app.core import fleet
    from app.db.base import SessionLocal

    async def beat() -> None:
        while True:
            try:
                with SessionLocal() as db:
                    fleet.heartbeat(db, "worker")
            except Exception:  # noqa: BLE001 — a missed beat must never stop the worker
                pass
            await asyncio.sleep(fleet.HEARTBEAT_SECONDS)

    ctx["fleet_heartbeat"] = asyncio.create_task(beat())


class WorkerSettings:
    """Arq worker entrypoint. Only queue-safe job types are registered."""

    functions = [login_job, coupon_job]
    on_startup = on_startup
    redis_settings = build_redis_settings()
    # One browser per job is heavy; keep a worker's parallelism modest and scale
    # by adding worker containers (PLAN.md §3).
    max_jobs = get_settings().max_concurrency
    job_timeout = 60 * 60
