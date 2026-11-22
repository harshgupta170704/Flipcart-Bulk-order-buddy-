"""In-process pub/sub for live job progress (feeds the WebSocket in PLAN.md §6).

One ``asyncio.Queue`` per subscriber, fanned out per ``job_id``. Deliberately
in-process: the API process is the only publisher of the events a browser needs
to see, so a broker would add an operational dependency for no gain. If workers
are ever moved out of the API process (``arq_worker``), this is the seam where a
Redis pub/sub backend would slot in.

Back-pressure policy: queues are **unbounded** so ``publish`` can never block or
deadlock a job on a slow consumer. To keep a stalled subscriber from growing
without limit we drop the *oldest* pending event once it is ``_MAX_PENDING``
behind — losing old progress lines is always better than stalling the run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

log = logging.getLogger("ordering.jobs.events")

# Per-subscriber backlog before the oldest events start being dropped.
_MAX_PENDING = 2000


class EventBus:
    """Fan-out of job events to any number of per-job subscribers."""

    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = {}

    # --- publishing ---------------------------------------------------------
    def publish_nowait(self, job_id: int, event: dict) -> None:
        """Synchronous fan-out.

        Exists so callers that are *not* coroutines (``JobTracker.start``) can
        publish in a well-defined order relative to the async publishers,
        instead of scheduling a task that would be delivered out of order.
        """
        queues = self._subscribers.get(job_id)
        if not queues:
            return  # nobody listening: publishing is a no-op, never an error
        for queue in list(queues):
            try:
                if queue.qsize() >= _MAX_PENDING:
                    _drop_oldest(queue, job_id)
                queue.put_nowait(event)
            except Exception as exc:  # noqa: BLE001 - a bad subscriber must not break the run
                log.warning("dropping event for job %s: %s", job_id, exc)

    async def publish(self, job_id: int, event: dict) -> None:
        """Async publish. Never blocks and never raises to the caller."""
        self.publish_nowait(job_id, event)

    # --- subscribing --------------------------------------------------------
    def subscribe(self, job_id: int) -> AsyncIterator[dict]:
        """Return an async iterator of this job's events.

        Sync on purpose: the queue is registered *before* this returns, so a
        caller can subscribe and then trigger work without racing the first
        events. Clean-up happens when the generator is closed or GC'd.
        """
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers.setdefault(job_id, set()).add(queue)

        async def _stream() -> AsyncIterator[dict]:
            try:
                while True:
                    yield await queue.get()
            finally:
                self._unsubscribe(job_id, queue)

        return _stream()

    def _unsubscribe(self, job_id: int, queue: asyncio.Queue) -> None:
        queues = self._subscribers.get(job_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(job_id, None)

    def subscriber_count(self, job_id: int) -> int:
        """Live subscribers for a job — used by tests and diagnostics."""
        return len(self._subscribers.get(job_id, ()))


def _drop_oldest(queue: asyncio.Queue, job_id: int) -> None:
    try:
        queue.get_nowait()
        log.warning("subscriber of job %s is behind; dropped oldest event", job_id)
    except asyncio.QueueEmpty:  # pragma: no cover - racy only in theory
        pass


# Module-level singleton used by the tracker and the API layer.
bus = EventBus()
