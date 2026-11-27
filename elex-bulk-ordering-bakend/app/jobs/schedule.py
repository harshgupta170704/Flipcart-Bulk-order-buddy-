"""Read-backs on a timer (the owner's ask of 26 Oct 2026: an order's delivery
should show up without anyone remembering to press a button).

Every :data:`CHECK_SECONDS` the API process asks whether a read-back is due:
the Settings page's "read back every N hours" is on, no read-back is queued or
running, and the last one ended more than N hours ago (or none ever ran). If so
it starts one over every account that holds a valid session — the very job the
Sync button starts, with the same defaults, marked as started by "schedule" so
the run history says that nobody pressed anything.

The timer never raises: a check that fails is logged and tried again at the
next tick, and the job it starts reports like any other.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.core import app_settings
from app.core.order_units import out_for_delivery_rows
from app.db.base import SessionLocal
from app.db.models import (
    Account,
    AccountStatus,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    OrderUnit,
    utcnow,
)

log = logging.getLogger("ordering.jobs.schedule")

#: How often the timer looks; the hours setting decides how often it acts.
CHECK_SECONDS = 5 * 60
#: ``Job.payload["started_by"]`` of a run the timer started.
STARTED_BY = "schedule"

_ACTIVE = (JobStatus.queued, JobStatus.running)


def _naive(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def read_back_due(db: DbSession, now: datetime | None = None) -> tuple[bool, str]:
    """Whether a scheduled read-back should start now, and why or why not —
    in the words the log and the Settings page use."""
    hours = int(app_settings.get_effective(db).get("sync_every_hours") or 0)
    if hours <= 0:
        return False, "automatic read-backs are off"
    active = db.scalar(select(Job.id).where(Job.type == JobType.sync, Job.status.in_(_ACTIVE)).limit(1))
    if active is not None:
        return False, f"read-back #{active} is still running"
    # Nor while an order run is going, as the tracking read-back already
    # waits (tracking_due): a read-back over every account would open a second
    # browser on the account being ordered on, and could find the order being
    # placed before the run has written down its id.
    ordering = db.scalar(select(Job.id).where(Job.type == JobType.order, Job.status.in_(_ACTIVE)).limit(1))
    if ordering is not None:
        return False, f"order run #{ordering} is still going"
    # The read-backs that only follow open orders (tracking_due) are not the
    # full one this asks for: counted, a half-hourly follow-up would put the
    # full read-back off for ever.
    last = next(
        (
            job
            for job in db.scalars(select(Job).where(Job.type == JobType.sync).order_by(Job.id.desc()).limit(200))
            if not (job.payload or {}).get("tracking")
        ),
        None,
    )
    moment = _naive(now or utcnow())
    if last is not None:
        ended = _naive(last.finished_at or last.started_at)
        if ended is not None and moment is not None and moment - ended < timedelta(hours=hours):
            due_at = ended + timedelta(hours=hours)
            return False, f"next read-back due at {due_at.isoformat(timespec='minutes')} UTC"
    if last is None:
        return True, "no read-back has run yet"
    return True, f"the last read-back ended more than {hours} hour(s) ago"


def accounts_with_sessions(db: DbSession) -> list[int]:
    """The accounts a read-back can sign in as: those holding a valid session."""
    return list(db.scalars(select(Account.id).where(Account.status == AccountStatus.session_valid).order_by(Account.id)))


def start_scheduled_read_back(db: DbSession) -> int | None:
    """Start the read-back the timer asked for, the way the Sync button does;
    None when no account holds a session, so there is nothing to read."""
    # The one place jobs are created and detached runs started (app.api.jobs);
    # imported here, not at module level, so the API package is not pulled in
    # by whoever imports the timer.
    from app.api.jobs import _create_job, _resolve_selection, _spawn
    from app.jobs.sync_tasks import run_sync_job
    from app.schemas.sync import SyncJobRequest

    ids = accounts_with_sessions(db)
    if not ids:
        log.info("scheduled read-back: no account holds a valid session; nothing to read")
        return None
    cap = max(1, int(getattr(get_settings(), "max_accounts_per_run", 5000) or 5000))
    if len(ids) > cap:
        # Over the run limit the selection would be refused outright, at every
        # tick: those read back longest ago go first, the rest at the next one.
        last = _last_read_backs(db, ids)
        ids = sorted(ids, key=lambda account_id: (account_id in last, last.get(account_id) or datetime.min, account_id))[:cap]
        log.info("scheduled read-back: %d account(s) this time, the ones read back longest ago", cap)
    selection = _resolve_selection(db, account_ids=ids, account_filter=None)
    defaults = SyncJobRequest()
    params = {
        "limit_per_account": defaults.limit_per_account,
        "import_unknown": defaults.import_unknown,
        "scheduled": True,
    }
    job = _create_job(db, JobType.sync, selection, params, started_by=STARTED_BY)
    job_id = job.id
    account_ids = list(selection.ids)
    _spawn(
        lambda: run_sync_job(
            job_id,
            account_ids,
            limit_per_account=defaults.limit_per_account,
            import_unknown=defaults.import_unknown,
            order_ids=None,
        ),
        job_id,
    )
    log.info("scheduled read-back #%s started over %d account(s)", job_id, len(account_ids))
    return job_id


# --- following orders until they arrive (the owner's ask of 26 Oct 2026) --------
#: Orders recorded longer ago than this are left to the overdue alert.
TRACK_OPEN_DAYS = 45
#: An unconfirmed order is followed this long: the read-back is what finds it
#: on Flipkart and settles it; after that it is a person's to settle.
TRACK_UNCONFIRMED_DAYS = 7
#: An account with an order recorded after its last read-back is read back
#: this long after the order — once its run has ended, as no read-back starts
#: while an order run is going — not at the next hourly pass: a new order's
#: status, and an unconfirmed one's fate, show within minutes.
AFTER_ORDER_MINUTES = 3
_UNDELIVERED = ("in_transit", "unknown")
_CHUNK = 500


def _on_its_way():
    """An order with a read-back unit still on its way, or none read back at
    all (a requested unit Flipkart never listed — a capped quantity — decides
    nothing, as in the order row's own reading)."""
    seen = OrderUnit.synced_at.is_not(None)
    unsettled = or_(OrderUnit.delivery_state.is_(None), OrderUnit.delivery_state.in_(_UNDELIVERED))
    return or_(
        exists().where(OrderUnit.order_pk == Order.id, seen, unsettled).correlate(Order),
        ~exists().where(OrderUnit.order_pk == Order.id, seen).correlate(Order),
    )


def open_order_ids(db: DbSession, account_id: int, now: datetime | None = None) -> list[str]:
    """The Flipkart ids of this account's orders still to follow: placed
    within :data:`TRACK_OPEN_DAYS` and still on their way. A tracking
    read-back reads by id those its recent list does not reach, a few a pass.

    Least recently read first — never read first of all, the newest of those
    first — so the few a pass takes work through every open order in turn,
    rather than reading the same newest ones each hour while an older order
    on its way is never read again."""
    moment = _naive(now or utcnow())
    recorded = func.coalesce(Order.placed_at, Order.created_at)
    rows = db.execute(
        select(Order.id, Order.order_id, func.max(OrderUnit.synced_at))
        .outerjoin(OrderUnit, OrderUnit.order_pk == Order.id)
        .where(
            Order.account_id == account_id,
            Order.status == OrderStatus.placed,
            Order.order_id.is_not(None),
            recorded >= moment - timedelta(days=TRACK_OPEN_DAYS),
            _on_its_way(),
        )
        .group_by(Order.id, Order.order_id)
    ).all()
    rows.sort(key=lambda row: (row[2] is not None, _naive(row[2]) or datetime.min, -row[0]))
    return [order_id for _pk, order_id, _read in rows if order_id]


def tracked_accounts(db: DbSession, now: datetime | None = None) -> tuple[set[int], set[int]]:
    """``(accounts with a unit out for delivery, accounts with any order still
    to follow)`` — the second includes the first.

    Still to follow: a placed order of the last :data:`TRACK_OPEN_DAYS` days
    with a read-back unit on its way, or never read back at all (a requested
    unit Flipkart never listed — a capped quantity — decides nothing, as in
    the order row's own reading); and an unconfirmed order of the last
    :data:`TRACK_UNCONFIRMED_DAYS` days, which a read-back can settle.
    """
    moment = _naive(now or utcnow())
    open_since = moment - timedelta(days=TRACK_OPEN_DAYS)
    unconfirmed_since = moment - timedelta(days=TRACK_UNCONFIRMED_DAYS)
    on_its_way = _on_its_way()
    recorded = func.coalesce(Order.placed_at, Order.created_at)
    placed = db.scalars(
        select(Order.account_id)
        .where(Order.status == OrderStatus.placed, recorded >= open_since, on_its_way)
        .distinct()
    ).all()
    unconfirmed = db.scalars(
        select(Order.account_id)
        .where(Order.status == OrderStatus.unconfirmed, recorded >= unconfirmed_since)
        .distinct()
    ).all()
    # Bounded like the open orders: a unit stuck at "out for delivery" (one the
    # recent list no longer reaches) would otherwise be read every half hour for ever.
    out_for_delivery = {
        int(order.account_id)
        for _unit, order, _email in out_for_delivery_rows(db)
        if (_naive(order.placed_at or order.created_at) or moment) >= open_since
    }
    return out_for_delivery, {int(a) for a in (*placed, *unconfirmed)} | out_for_delivery


def _last_read_backs(db: DbSession, account_ids) -> dict[int, datetime]:
    """When each account was last read back: its newest finished read-back
    item that was not skipped. A skipped item read nothing, or gave the account
    over to an order run part-way (app.jobs.account_guard): either way the
    account is still to be read."""
    ids = sorted(account_ids)
    found: dict[int, datetime] = {}
    for start in range(0, len(ids), _CHUNK):
        rows = db.execute(
            select(JobItem.account_id, func.max(JobItem.finished_at))
            .join(Job, Job.id == JobItem.job_id)
            .where(
                Job.type == JobType.sync,
                JobItem.account_id.in_(ids[start : start + _CHUNK]),
                JobItem.finished_at.is_not(None),
                JobItem.status != JobItemStatus.skipped,
            )
            .group_by(JobItem.account_id)
        ).all()
        for account_id, moment in rows:
            if moment is not None:
                found[int(account_id)] = _naive(moment)
    return found


def _newest_orders(db: DbSession, account_ids) -> dict[int, datetime]:
    """When each account's newest placed or unconfirmed order was recorded by
    a run of this tool. Orders filed with no run — an order report or an id
    upload, which file many accounts' orders at once — are left to the clock:
    counted here, one upload would set off a read-back of every account in it
    a few minutes later (27 Oct 2026)."""
    ids = sorted(account_ids)
    found: dict[int, datetime] = {}
    for start in range(0, len(ids), _CHUNK):
        rows = db.execute(
            select(Order.account_id, func.max(Order.created_at))
            .where(
                Order.account_id.in_(ids[start : start + _CHUNK]),
                Order.status.in_((OrderStatus.placed, OrderStatus.unconfirmed)),
                Order.job_id.is_not(None),
            )
            .group_by(Order.account_id)
        ).all()
        for account_id, moment in rows:
            if moment is not None:
                found[int(account_id)] = _naive(moment)
    return found


def tracking_due(db: DbSession, now: datetime | None = None) -> tuple[list[int], str]:
    """The accounts whose orders should be read back now, and why — in the
    words the log uses. Empty while any read-back or order run is going, so
    two browsers never work one account at once."""
    effective = app_settings.get_effective(db)
    every = max(0, int(effective.get("track_every_minutes") or 0))
    ofd_every = max(0, int(effective.get("out_for_delivery_minutes") or 0))
    if not every and not ofd_every:
        return [], "order tracking is off"
    out_for_delivery, open_accounts = tracked_accounts(db, now)
    if not open_accounts:
        return [], "no order is on its way"
    active = db.scalar(
        select(Job.id).where(Job.type.in_((JobType.sync, JobType.order)), Job.status.in_(_ACTIVE)).limit(1)
    )
    if active is not None:
        return [], f"run #{active} is still going"
    ids = sorted(open_accounts)
    signed_in: set[int] = set()
    for start in range(0, len(ids), _CHUNK):
        signed_in.update(
            db.scalars(
                select(Account.id).where(
                    Account.id.in_(ids[start : start + _CHUNK]), Account.status == AccountStatus.session_valid
                )
            ).all()
        )
    if not signed_in:
        return [], "no account with an order on its way holds a session"
    moment = _naive(now or utcnow())
    last = _last_read_backs(db, signed_in)
    newest = _newest_orders(db, signed_in)
    ordered: list[int] = []
    on_the_van: list[int] = []
    by_clock: list[int] = []
    for account_id in sorted(signed_in):
        seen_at = last.get(account_id)
        ordered_at = newest.get(account_id)
        if (
            seen_at is not None
            and ordered_at is not None
            and ordered_at > seen_at
            and moment - ordered_at >= timedelta(minutes=AFTER_ORDER_MINUTES)
        ):
            # Ordered on since it was last read: read it now the run is over.
            ordered.append(account_id)
            continue
        interval = ofd_every if (account_id in out_for_delivery and ofd_every) else every
        if not interval:
            continue
        if seen_at is None or (moment is not None and moment - seen_at >= timedelta(minutes=interval)):
            (on_the_van if account_id in out_for_delivery else by_clock).append(account_id)
    # Under the cap the urgent ones go first: a run takes the first ``cap``.
    by_clock.sort(key=lambda account_id: (account_id in last, last.get(account_id) or datetime.min, account_id))
    due = ordered + on_the_van + by_clock
    ordered_due = len(ordered)
    if not due:
        return [], "every account with an order on its way was read back recently"
    cap = max(1, int(getattr(get_settings(), "max_accounts_per_run", 5000) or 5000))
    ofd_due = sum(1 for account_id in due if account_id in out_for_delivery)
    return due[:cap], (
        f"{len(due)} account(s) with an order on its way not read back recently ({ofd_due} out for delivery)"
        + (f"; {ordered_due} ordered on since" if ordered_due else "")
    )


def start_tracking_read_back(db: DbSession, account_ids: list[int]) -> int | None:
    """Read back the accounts :func:`tracking_due` named, the way the Sync
    button does; marked as the timer's, and as tracking, so the full
    scheduled read-back is not put off by it."""
    from app.api.jobs import _create_job, _resolve_selection, _spawn
    from app.jobs.sync_tasks import run_sync_job
    from app.schemas.sync import SyncJobRequest

    if not account_ids:
        return None
    selection = _resolve_selection(db, account_ids=list(account_ids), account_filter=None)
    defaults = SyncJobRequest()
    params = {
        "limit_per_account": defaults.limit_per_account,
        "import_unknown": defaults.import_unknown,
        "scheduled": True,
        "tracking": True,
    }
    job = _create_job(db, JobType.sync, selection, params, started_by=STARTED_BY)
    job_id = job.id
    ids = list(selection.ids)
    _spawn(
        lambda: run_sync_job(
            job_id,
            ids,
            limit_per_account=defaults.limit_per_account,
            import_unknown=defaults.import_unknown,
            order_ids=None,
            follow_open=True,
        ),
        job_id,
    )
    log.info("order tracking read-back #%s started over %d account(s)", job_id, len(ids))
    return job_id


async def tick_once() -> int | None:
    """One look at the clock: the full read-back the hours setting asks for,
    if due; else the read-back of the accounts with an order still on its way.
    Returns the id of the run it started, if any."""
    with SessionLocal() as db:
        due, why = read_back_due(db)
        if due:
            log.info("scheduled read-back: %s", why)
            try:
                started = start_scheduled_read_back(db)
            except Exception:  # noqa: BLE001 — a full read-back that cannot start must not stop the follow-ups
                log.exception("the scheduled read-back could not start; following the open orders instead")
                db.rollback()
            else:
                if started is not None:
                    return started
        else:
            log.debug("no scheduled read-back: %s", why)
        accounts, why = tracking_due(db)
        if not accounts:
            log.debug("no order tracking read-back: %s", why)
            return None
        log.info("order tracking: %s", why)
        return start_tracking_read_back(db, accounts)


async def run_read_back_scheduler(check_seconds: float = CHECK_SECONDS) -> None:
    """The loop the API process runs for its lifetime (see app.main.lifespan)."""
    while True:
        await asyncio.sleep(check_seconds)
        try:
            await tick_once()
        except Exception:  # noqa: BLE001 — the timer must outlive any one failure
            log.exception("the scheduled read-back check failed; trying again at the next tick")
