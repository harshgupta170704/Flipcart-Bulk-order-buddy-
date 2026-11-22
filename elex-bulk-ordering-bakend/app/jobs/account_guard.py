"""One account, one run at a time: an order run and a read-back never work
the same account at once (found by the card session, 26 Oct 2026).

The timer starts no read-back while an order run is going (``schedule``), but
a person can still start an order while a read-back is reading the account —
a tracking read-back of one busy account takes ten minutes — or press "Read
back now" on an account an order run is using. Two browsers then work one
account, and the read-back can find the order being placed before the run has
written down its id.

The order goes first. A read-back passes over an account an order run is
using, and one already reading it stops between pages when an order starts
there (``scrape_orders(should_stop=...)``). A passed-over account does not
count as read back (``schedule._last_read_backs``), so it is read after that
run. The order waits for that page to end, and for no longer than
:data:`READ_BACK_WAIT_SECONDS`: after that it goes ahead, and the run's own
reservation still wins the adoption (``tasks.settle_order_row``).

Read off the job tables rather than a lock in memory, so a run in another
process counts too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Job, JobItem, JobItemStatus, JobStatus, JobType

log = logging.getLogger("ordering.jobs.account_guard")

#: The longest an order waits for a read-back to let go of its account. A
#: read-back stops at its next page, about half a minute at most.
READ_BACK_WAIT_SECONDS = 120.0
#: How often the order looks while it waits.
POLL_SECONDS = 2.0

_ACTIVE = (JobStatus.queued, JobStatus.running)


def run_on_account(account_id: int, job_type: JobType) -> int | None:
    """The id of a ``job_type`` run working this account right now — one whose
    item for the account is running — or None."""
    with SessionLocal() as db:
        return db.scalar(
            select(Job.id)
            .join(JobItem, JobItem.job_id == Job.id)
            .where(
                Job.type == job_type,
                Job.status.in_(_ACTIVE),
                JobItem.account_id == account_id,
                JobItem.status == JobItemStatus.running,
            )
            .order_by(Job.id)
            .limit(1)
        )


async def wait_for_read_back(
    account_id: int,
    *,
    should_cancel: Callable[[], bool] | None = None,
    timeout: float | None = None,
    poll: float | None = None,
) -> str | None:
    """Wait while a read-back is reading this account; it stops at its next
    page once it sees the order's item running. Returns a line for the run's
    log when the order had to wait, else None. Returns early when the run is
    cancelled meanwhile — the caller looks at the flag again."""
    timeout = READ_BACK_WAIT_SECONDS if timeout is None else timeout
    poll = POLL_SECONDS if poll is None else poll
    reading = run_on_account(account_id, JobType.sync)
    if reading is None:
        return None
    log.info("account %s: waiting for read-back #%s to stop reading it", account_id, reading)
    started = time.monotonic()
    while True:
        await asyncio.sleep(poll)
        waited = time.monotonic() - started
        if should_cancel is not None and should_cancel():
            return f"account {account_id}: the run was cancelled while read-back #{reading} was reading it"
        still = run_on_account(account_id, JobType.sync)
        if still is None:
            return f"account {account_id}: waited {waited:.0f} s for read-back #{reading} to stop reading it first"
        if waited >= timeout:
            log.warning(
                "account %s: read-back #%s still reading after %.0f s; ordering anyway", account_id, still, waited
            )
            return f"account {account_id}: read-back #{still} was still reading it after {waited:.0f} s — ordered anyway"
