"""Job progress persistence + live events.

Every method opens its own short-lived ``Session`` and closes it. That matters:
a job runs many accounts concurrently, and a SQLAlchemy ``Session`` is not
thread/task safe — sharing one would interleave flushes from different items.
Short sessions also mean the API sees progress immediately (each call commits),
which is what the dashboard polls and what ``is_cancel_requested`` reads back.

The DB calls are intentionally made inline (not via ``asyncio.to_thread``):
they are tiny, and serialising them on the event loop avoids concurrent writers
fighting over the SQLite file. Swapping to PostgreSQL later is where moving
them to a thread would start to pay off.

Nothing written here may contain card data (PLAN.md §5) — ``Job.log`` and the
event payloads are built only from account ids, outcomes and short messages
supplied by ``tasks``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.db.base import SessionLocal
from app.db.models import Account, Job, JobItem, JobItemStatus, JobStatus, naive_utcnow, utcnow
from app.jobs.events import bus as default_bus

log = logging.getLogger("ordering.jobs.tracker")

# Job.log is a running transcript; cap it so a huge batch cannot bloat the row.
_MAX_LOG_CHARS = 200_000


class JobTracker:
    """Writes per-item progress for one job and publishes it to subscribers."""

    def __init__(
        self,
        job_id: int,
        db_factory: Callable[[], DbSession] = SessionLocal,
        bus=None,
    ) -> None:
        self.job_id = job_id
        self._db_factory = db_factory
        self._bus = default_bus if bus is None else bus
        self._total_items = 0
        #: Accounts the run was asked for that no longer exist (deleted between
        #: the start request and the run): settled at start, ignored after.
        self._missing: set[int] = set()
        # Keeps fire-and-forget publish tasks referenced so they are not GC'd
        # mid-flight (only used for buses without a sync publish path).
        self._pending: set[asyncio.Task] = set()

    # --- lifecycle ----------------------------------------------------------
    def start(self, account_ids: list[int]) -> None:
        """Mark the job running and create one pending item per account.

        An account that no longer exists gets no item — an item row points at
        an account row, and PostgreSQL enforces that (SQLite does too since
        25 Oct 2026) — so it is written off here as skipped, with a log line,
        and whatever the run later reports for it is ignored rather than
        counted twice.
        """
        account_ids = list(account_ids)
        self._total_items = len(account_ids)

        with self._session() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                log.error("start(): job %s does not exist", self.job_id)
                return
            present = set(db.scalars(select(Account.id).where(Account.id.in_(account_ids)))) if account_ids else set()
            self._missing = {account_id for account_id in account_ids if account_id not in present}
            # Re-starting a job replaces its items rather than doubling them.
            for existing in list(job.items):
                db.delete(existing)
            job.status = JobStatus.running
            job.total_items = len(account_ids)
            job.done_items = len(self._missing)
            job.failed_items = 0
            job.skipped_items = len(self._missing)
            job.progress = _progress(job.done_items, job.total_items)
            job.finished_at = None
            for account_id in account_ids:
                if account_id in self._missing:
                    continue
                db.add(
                    JobItem(
                        job_id=self.job_id,
                        account_id=account_id,
                        status=JobItemStatus.pending,
                    )
                )
            self._append_log(job, f"job started with {len(account_ids)} account(s)")
            for account_id in sorted(self._missing):
                self._append_log(job, f"account {account_id}: skipped - account not found (deleted?)")
            db.commit()

        self._publish_sync(
            {
                "type": "job_started",
                "job_id": self.job_id,
                "total_items": len(account_ids),
                "account_ids": account_ids,
                "progress": 0,
            }
        )

    async def finish(self, status: JobStatus | None = None) -> None:
        """Close the job out. Default status: completed, or failed if nothing
        succeeded (an all-failed batch is a failed job, not a finished one)."""
        payload: dict = {
            "type": "job_finished",
            "job_id": self.job_id,
            "progress": 100,
        }
        with self._session() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                log.error("finish(): job %s does not exist", self.job_id)
                return
            resolved = status or self._default_final_status(job)
            job.status = resolved
            job.progress = 100
            job.finished_at = utcnow()
            succeeded = (
                (job.done_items or 0)
                - (job.failed_items or 0)
                - (job.skipped_items or 0)
                - (job.unconfirmed_items or 0)
            )
            unsure = (
                f", {job.unconfirmed_items} unconfirmed — check those accounts before re-running"
                if job.unconfirmed_items
                else ""
            )
            self._append_log(
                job,
                f"job finished: {resolved.value} "
                f"({succeeded} ok, {job.failed_items} failed, "
                f"{job.skipped_items} skipped{unsure})",
            )
            payload.update(
                status=resolved.value,
                total_items=job.total_items,
                done_items=job.done_items,
                failed_items=job.failed_items,
                skipped_items=job.skipped_items,
                unconfirmed_items=job.unconfirmed_items or 0,
                succeeded_items=succeeded,
            )
            db.commit()

        await self._publish(payload)

    @staticmethod
    def _default_final_status(job: Job) -> JobStatus:
        """Failed only when real failures happened and nothing succeeded.

        A batch where every account was merely skipped (e.g. none had a session
        yet) is `completed` — nothing went wrong, there was simply nothing to
        do. One over-target order among successes is also `completed`: the run
        worked, that single order didn't (PLAN.md §6a).
        """
        succeeded = (
            (job.done_items or 0)
            - (job.failed_items or 0)
            - (job.skipped_items or 0)
            - (job.unconfirmed_items or 0)
        )
        if (job.failed_items or 0) > 0 and succeeded <= 0:
            return JobStatus.failed
        # Unconfirmed orders never fail the job: "failed" tells the operator
        # to retry, and retrying an order that may exist buys it twice.
        return JobStatus.completed

    # --- per-item -----------------------------------------------------------
    async def item_started(self, account_id: int) -> None:
        if account_id in self._missing:
            return  # written off at start; nothing to mark
        progress = 0
        with self._session() as db:
            job = db.get(Job, self.job_id)
            item = self._item(db, account_id)
            if item is not None:
                item.status = JobItemStatus.running
                # The clock for this account starts here; a re-start clears a
                # stale end so the duration is never read off two runs. Naive
                # UTC on purpose (see JobItem): the two stamps must survive the
                # database's zone the same way.
                item.started_at = naive_utcnow()
                item.finished_at = None
            if job is not None:
                self._append_log(job, f"account {account_id}: started")
                progress = job.progress
            db.commit()

        await self._publish(
            {
                "type": "item_started",
                "job_id": self.job_id,
                "account_id": account_id,
                "progress": progress,
            }
        )

    async def item_finished(
        self,
        account_id: int,
        ok: bool,
        message: str | None = None,
        skipped: bool = False,
        unconfirmed: bool = False,
    ) -> None:
        """Record one account's outcome and recompute job progress.

        ``unconfirmed`` is an order that was sent but never confirmed: it may
        exist. It is neither ``ok`` nor a failure, so it is counted on its own —
        a run with one of these must not read as "failed" (which says retry)
        nor as clean (which says nothing needs looking at).
        """
        if account_id in self._missing:
            return  # counted as skipped at start; the run's own verdict is moot
        event: dict = {
            "type": "item_finished",
            "job_id": self.job_id,
            "account_id": account_id,
            "ok": ok,
            "message": message,
            "skipped": skipped,
            "unconfirmed": unconfirmed,
        }
        with self._session() as db:
            job = db.get(Job, self.job_id)
            item = self._item(db, account_id)
            if item is not None:
                if skipped:
                    item.status = JobItemStatus.skipped
                elif unconfirmed:
                    item.status = JobItemStatus.unconfirmed
                else:
                    item.status = JobItemStatus.done if ok else JobItemStatus.failed
                item.message = message
                item.updated_at = utcnow()
                item.finished_at = naive_utcnow()
                if skipped:
                    # Passed over (no session, no card, already done): the run
                    # spent nothing on it, whatever the clock said when the
                    # worker marked it started before its gates. No time shown.
                    item.started_at = None
                # Seconds spent on this account, for the live view; None for an
                # account passed over or never started.
                event["seconds"] = item.seconds
            if job is not None:
                job.done_items = (job.done_items or 0) + 1
                # A skip is not a failure: "no session" / "no card" / "already
                # done" / "cancelled" means we passed the account over, and
                # counting those red would misrepresent a healthy run.
                if skipped:
                    job.skipped_items = (job.skipped_items or 0) + 1
                elif unconfirmed:
                    job.unconfirmed_items = (job.unconfirmed_items or 0) + 1
                elif not ok:
                    job.failed_items = (job.failed_items or 0) + 1
                job.progress = _progress(job.done_items, job.total_items)
                outcome = (
                    "skipped"
                    if skipped
                    else "unconfirmed" if unconfirmed else ("ok" if ok else "failed")
                )
                suffix = f" - {message}" if message else ""
                self._append_log(job, f"account {account_id}: {outcome}{suffix}")
                event.update(
                    progress=job.progress,
                    done_items=job.done_items,
                    failed_items=job.failed_items,
                    skipped_items=job.skipped_items,
                    unconfirmed_items=job.unconfirmed_items or 0,
                    total_items=job.total_items,
                )
            db.commit()

        event.setdefault("progress", 0)
        await self._publish(event)

    # --- AI price-tracker helpers -------------------------------------------
    def add_attempt(self, account_id: int) -> None:
        """Append a fresh JobItem row when an AI price-tracker run recycles an
        account for a new probe interval, keeping total_items accurate."""
        if account_id in self._missing:
            return
        with self._session() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return
            db.add(
                JobItem(
                    job_id=self.job_id,
                    account_id=account_id,
                    status=JobItemStatus.pending,
                )
            )
            job.total_items = (job.total_items or 0) + 1
            job.progress = _progress(job.done_items or 0, job.total_items)
            db.commit()

    async def update_ai_tracker(self, snapshot: dict) -> None:
        """Persist the live AI price-tracker snapshot on Job.payload and push a
        WebSocket event so the UI shows current/lowest price and next probe."""
        price = snapshot.get("last_seen_price")
        target = snapshot.get("target_price")
        status = snapshot.get("status") or "watching"
        parts = [f"AI {status.replace('_', ' ')}"]
        if price is not None:
            parts.append(f"price ₹{price:,.2f}")
        if target is not None:
            parts.append(f"cap ₹{target:,.2f}")
        if snapshot.get("placed_orders"):
            parts.append(f"{snapshot['placed_orders']} placed")
        msg = " · ".join(parts)
        with self._session() as db:
            job = db.get(Job, self.job_id)
            if job is not None:
                # Reassign dict so SQLAlchemy flushes the JSON column cleanly.
                job.payload = {**(job.payload or {}), "ai_tracker": snapshot}
                db.commit()
        await self._publish(
            {
                "type": "ai_tracker_update",
                "job_id": self.job_id,
                "ai_tracker": snapshot,
                "message": msg,
            }
        )

    # --- cancellation -------------------------------------------------------
    def is_cancel_requested(self) -> bool:
        """Re-read the flag from the DB so the API can cancel mid-run."""
        with self._session() as db:
            job = db.get(Job, self.job_id)
            return bool(job.cancel_requested) if job is not None else False

    # --- internals ----------------------------------------------------------
    @contextlib.contextmanager
    def _session(self) -> Iterator[DbSession]:
        db = self._db_factory()
        try:
            yield db
        finally:
            db.close()

    def _item(self, db: DbSession, account_id: int) -> JobItem | None:
        item = db.scalar(
            select(JobItem)
            .where(JobItem.job_id == self.job_id, JobItem.account_id == account_id)
            .order_by(JobItem.id.desc())
        )
        if item is None:
            log.warning("job %s has no item for account %s", self.job_id, account_id)
        return item

    @staticmethod
    def _append_log(job: Job, line: str) -> None:
        stamped = f"[{utcnow().isoformat(timespec='seconds')}] {line}"
        text = f"{job.log or ''}{stamped}\n"
        if len(text) > _MAX_LOG_CHARS:  # keep the tail — the recent lines matter
            text = text[-_MAX_LOG_CHARS:]
        job.log = text

    async def _publish(self, event: dict) -> None:
        try:
            await self._bus.publish(self.job_id, event)
        except Exception:  # noqa: BLE001 - progress reporting is never fatal
            log.exception("publishing %s failed", event.get("type"))

    def _publish_sync(self, event: dict) -> None:
        """Publish from a non-async method, keeping event order intact."""
        nowait = getattr(self._bus, "publish_nowait", None)
        if callable(nowait):
            try:
                nowait(self.job_id, event)
            except Exception:  # noqa: BLE001
                log.exception("publishing %s failed", event.get("type"))
            return

        coro = self._bus.publish(self.job_id, event)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop => no subscriber can exist; drop it without a warning.
            coro.close()
            return
        task = loop.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


def _progress(done_items: int, total_items: int) -> int:
    if not total_items:  # guard: an empty batch is 0%, never a ZeroDivisionError
        return 0
    return round(100 * (done_items or 0) / total_items)
