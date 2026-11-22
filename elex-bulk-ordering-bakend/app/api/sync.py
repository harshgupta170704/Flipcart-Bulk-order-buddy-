"""Sync API — start a post-order sync run (ROADMAP §10).

``POST /jobs/sync`` is the fourth start endpoint, and it deliberately behaves
exactly like the other three: the accounts are chosen by a **filter** rather
than by a list of ids (at 37k accounts "all matching" cannot mean posting 37,000
ids), the resolved count is checked against ``settings.max_accounts_per_run``
before anything starts, and the answer is the ``Job`` row the client then polls
on ``GET /jobs/{id}`` or follows over ``/ws/jobs/{id}`` — all of which already
exist in ``app.api.jobs``, so nothing here duplicates them.

The selection, the Job row and the background task are created through
``app.api.jobs``' own helpers rather than re-implemented. That is the point: the
ceiling, the 400 on an empty selection, the ``account_filter``/``account_count``
provenance recorded on the job and the strong reference that keeps a long run
from being garbage-collected must be *identical* to the other endpoints, and a
second copy of that logic here would be free to drift from them.

Like an order run this always runs in-process. Not for the card reason (a sync
reads nothing sensitive — it could be queued later); simply because the job
engine runs in this process and a sync has no reason to be the first thing to
need a broker.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

# Private-by-name but shared by design: see the module docstring. _resolve_selection
# is the one path "which accounts?" takes for every run, _create_job the one place
# a Job row is written, _spawn the one place a detached run is started.
from app.api.jobs import _create_job, _refuse_if_active, _resolve_selection, _Selection, _spawn
from app.core.order_filter import normalise_many
from app.core.security import require_user
from app.db.base import get_db
from app.db.models import JobType, Order
from app.jobs.sync_tasks import run_sync_job
from app.schemas.job import JobOut
from app.schemas.sync import SyncJobRequest

log = logging.getLogger("ordering.api.sync")

router = APIRouter(
    prefix="/jobs",
    tags=["sync"],
    # Declared on the router, not per route, so a route added here later cannot
    # ship unauthenticated by a wiring mistake.
    dependencies=[Depends(require_user)],
)


@router.post("/sync", response_model=JobOut, status_code=201)
async def start_sync_job(
    payload: SyncJobRequest, db: DbSession = Depends(get_db), who: str = Depends(require_user)
) -> JobOut:
    """Read each selected account's recent orders back off Flipkart.

    Fills in what actually happened — delivered state, tracking id, real
    delivery date, delivery OTP, seller, the invoice's link — on the orders we already
    recorded. It never rewrites what we intended and paid; see
    ``app.jobs.sync_tasks``.

    ``limit_per_account`` is how many of an account's most recent orders to
    read. ``import_unknown`` additionally creates rows for orders this tool
    never placed, so orders made by hand or by the old portal show up too.
    """
    # One sync at a time, enforced here and not only by the button: two syncs
    # over the same accounts would write the same rows at once.
    _refuse_if_active(db, JobType.sync)
    order_ids = _listed_order_ids(payload.order_ids)
    if order_ids and payload.account_ids is None and payload.account_filter is None:
        selection = _selection_from_orders(db, order_ids)
    else:
        selection = _resolve_selection(
            db, account_ids=payload.account_ids, account_filter=payload.account_filter
        )
    account_ids = selection.ids
    params: dict = {
        # Non-sensitive run parameters only, like every other job payload
        # (PLAN.md §5) — a sync has none that could be otherwise.
        "limit_per_account": payload.limit_per_account,
        "import_unknown": payload.import_unknown,
    }
    if order_ids:
        params["order_ids"] = order_ids
    job = _create_job(db, JobType.sync, selection, params, started_by=who)
    # Snapshot before dispatch: the background run owns the row from here on.
    out = JobOut.model_validate(job)
    log.info(
        "starting sync job %s over %s account(s), %s",
        out.id,
        len(account_ids),
        f"{len(order_ids)} listed order(s)" if order_ids else f"{payload.limit_per_account} order(s) each",
    )
    _spawn(
        lambda: run_sync_job(
            out.id,
            account_ids,
            limit_per_account=payload.limit_per_account,
            import_unknown=payload.import_unknown,
            order_ids=order_ids or None,
            # A read of the recent list also reads, by id, the orders still on
            # their way that it does not reach: "Read back now" pressed on such
            # an order would otherwise change nothing (26 Oct 2026).
            follow_open=not order_ids,
        ),
        out.id,
    )
    return out


_ORDER_ID_RE = re.compile(r"^OD\d{10,}$")
#: SQLite binds at most 999 variables per statement.
_CHUNK = 500


def _listed_order_ids(raw: list[str] | None) -> list[str]:
    """The listed ids, normalised and checked — or a 422, never a silent
    widening: a list that holds no usable id must not turn into "every
    account's recent orders"."""
    if raw is None:
        return []
    order_ids = list(normalise_many(raw, upper=True))
    if not order_ids:
        raise HTTPException(status_code=422, detail="order_ids holds no usable order id")
    bad = [value for value in order_ids if not _ORDER_ID_RE.match(value)]
    if bad:
        raise HTTPException(
            status_code=422, detail=f"not Flipkart order ids: {', '.join(bad[:5])}"
        )
    return order_ids


def _selection_from_orders(db: DbSession, order_ids: list[str]) -> _Selection:
    """The accounts holding these orders — the ones a by-id read-back signs in
    as — resolved through the same path as every other run, so the ceiling
    and the provenance shape stay identical."""
    account_ids: set[int] = set()
    for start in range(0, len(order_ids), _CHUNK):
        chunk = order_ids[start : start + _CHUNK]
        account_ids.update(
            db.scalars(select(Order.account_id).where(Order.order_id.in_(chunk))).all()
        )
    if not account_ids:
        raise HTTPException(
            status_code=400,
            detail="none of the listed order ids is in the ledger — upload them first",
        )
    selection = _resolve_selection(db, account_ids=sorted(account_ids))
    return _Selection(
        ids=selection.ids,
        provenance={**selection.provenance, "selected_by": "order_ids"},
    )
