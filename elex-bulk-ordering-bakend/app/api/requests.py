"""Requests API — the cancel / reschedule log (spec FR-020, decision C-4).

The owner's decision of 25 Oct 2026: a row's Cancel, Cancel Order Units and
Reschedule Order Units record a request with a reason and a status log for a
person to action on the platform. Nothing here signs in to or acts on
Flipkart; the person cancels or reschedules the unit there by hand and then
marks the request done (or drops it) under Requests.

Operators and admins alike may do all of it: a request is part of moving
orders, not of setting the app up.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core import unit_requests as core
from app.core.order_status import classify
from app.core.security import Principal, current_user, require_user
from app.db.base import get_db
from app.db.models import Account, Order, OrderUnit, UnitRequest, utcnow
from app.schemas.request import (
    RequestCreateIn,
    RequestCreateOut,
    RequestEventOut,
    RequestOut,
    RequestPage,
    RequestPatchIn,
    RequestSkippedOut,
)

router = APIRouter(
    prefix="/requests",
    tags=["requests"],
    dependencies=[Depends(require_user)],
)

DEFAULT_SIZE = 50
MAX_SIZE = 500
MAX_PAGE = 1_000_000

#: ``?status=`` values meaning "every status". Blank is what a UI sends when
#: its dropdown is on "all"; the word is for a person typing the URL.
_STATUS_ANY = {"", "all", "any"}


def _joined():
    """Request → its unit → the order → the account: the one shape every read has."""
    return (
        select(UnitRequest, OrderUnit, Order, Account.email)
        .join(OrderUnit, OrderUnit.id == UnitRequest.unit_pk)
        .join(Order, Order.id == OrderUnit.order_pk)
        .join(Account, Account.id == Order.account_id)
    )


def _out(
    row: UnitRequest, unit: OrderUnit, order: Order, email: str | None, steps: dict | None = None
) -> RequestOut:
    """One request as the log shows it; ``steps`` is :func:`core.history` for
    the page, read in one query, from which the request's history comes."""
    return RequestOut(
        history=[
            RequestEventOut(status=e.status, actor=e.actor, at=e.at, note=e.note)
            for e in (steps or {}).get(row.id, [])
        ],
        id=row.id,
        unit_pk=row.unit_pk,
        kind=row.kind,
        reason=row.reason,
        requested_for=row.requested_for,
        status=row.status,
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        closed_by=row.closed_by,
        closed_at=row.closed_at,
        note=row.note,
        order_row_id=order.id,
        order_id=order.order_id,
        unit_id=unit.unit_id,
        product=order.model,
        platform_status=unit.platform_status,
        delivery_state=unit.delivery_state or classify(unit.platform_status),
        account_email=email,
    )


def _outs_for(db: DbSession, ids: list[int]) -> list[RequestOut]:
    """The rows behind these ids, in the order the ids were given."""
    if not ids:
        return []
    rows = db.execute(_joined().where(UnitRequest.id.in_(ids))).all()
    steps = core.history(db, ids)
    by_id = {row.id: _out(row, unit, order, email, steps) for row, unit, order, email in rows}
    return [by_id[i] for i in ids if i in by_id]


def requests_on_order(db: DbSession, order_pk: int) -> list[RequestOut]:
    """Every request on one order's units, open or closed, newest first, in the
    same shape as the log's rows: what the row dialog lists for its unit
    (``GET /orders/{id}/requests``), in one query however many there are."""
    rows = db.execute(
        _joined()
        .where(OrderUnit.order_pk == order_pk)
        .order_by(UnitRequest.requested_at.desc(), UnitRequest.id.desc())
    ).all()
    steps = core.history(db, [row.id for row, *_rest in rows])
    return [_out(*row, steps) for row in rows]


def _parse_status(raw: str | None) -> str | None:
    """The status filter: None for every status; absent means the open queue."""
    if raw is None:
        return core.STATUS_OPEN
    value = raw.strip().lower()
    if value in _STATUS_ANY:
        return None
    if value in core.STATUSES:
        return value
    raise HTTPException(
        status_code=422,
        detail=f"Unknown status '{raw}'. Valid values: {', '.join(core.STATUSES)}, all",
    )


def _parse_kind(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value in core.KINDS:
        return value
    raise HTTPException(
        status_code=422, detail=f"Unknown kind '{raw}'. Valid values: {', '.join(core.KINDS)}"
    )


@router.post("", status_code=201, response_model=RequestCreateOut)
def create_requests(
    payload: RequestCreateIn,
    principal: Principal = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> RequestCreateOut:
    """Record the same request on each unit in the list.

    A unit that cannot take it is passed over with the reason — it does not
    exist, it already has an open request of this kind, or (for a cancel) it
    is already settled: delivered, cancelled, returned or failed — and the
    rest are still recorded. A 409 is the one case the check cannot see:
    another operator recorded the same request between the check and the
    write, and the database's own rule (one open request per kind per unit)
    refused the second.
    """
    created, skipped = core.create_requests(
        db,
        unit_pks=payload.unit_ids,
        kind=payload.kind,
        reason=payload.reason,
        requested_for=payload.requested_for,
        requested_by=principal.email,
    )
    # Ids before the commit: afterwards every row is expired, and reading
    # ``row.id`` would be one refresh query per request recorded.
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"Another open {payload.kind} request was just recorded on one of these units "
                "— reload the page and try again."
            ),
        ) from None
    ids = [row.id for row in created]
    db.commit()
    return RequestCreateOut(
        created=_outs_for(db, ids),
        skipped=[RequestSkippedOut(unit_id=s.unit_pk, why=s.why) for s in skipped],
    )


@router.get("", response_model=RequestPage)
def list_requests(
    status: str | None = Query(default=None, description="open (the default), done, dropped or all"),
    kind: str | None = Query(default=None, description="cancel or reschedule; blank for both"),
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    size: int = Query(default=DEFAULT_SIZE, ge=1, le=MAX_SIZE),
    db: DbSession = Depends(get_db),
) -> RequestPage:
    """One page of requests, newest first, plus the full count for the filter."""
    want_status = _parse_status(status)
    want_kind = _parse_kind(kind)

    def narrow(stmt):
        if want_status is not None:
            stmt = stmt.where(UnitRequest.status == want_status)
        if want_kind is not None:
            stmt = stmt.where(UnitRequest.kind == want_kind)
        return stmt

    total = int(db.scalar(narrow(select(func.count()).select_from(UnitRequest))) or 0)
    rows = db.execute(
        narrow(_joined())
        .order_by(UnitRequest.requested_at.desc(), UnitRequest.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    steps = core.history(db, [row.id for row, *_rest in rows])
    return RequestPage(items=[_out(*row, steps) for row in rows], total=total)


@router.patch("/{request_id}", response_model=RequestOut)
def update_request(
    request_id: int,
    payload: RequestPatchIn,
    principal: Principal = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> RequestOut:
    """Mark a request done or dropped — by whom and when — or put it back to open.

    A change of status carries its own note (or none). A PATCH that repeats
    the current status — closing twice, "open" on an open row — changes
    nothing: the first close's name and time stay, and the note stays unless
    a new one is written. A reopen is refused while the unit already has
    another open request of the same kind.
    """
    row = db.get(UnitRequest, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Request not found")
    note = (payload.note or "").strip() or None
    kind = row.kind
    clash = HTTPException(status_code=409, detail=f"This unit already has another open {kind} request.")
    changed = row.status != payload.status
    if changed:
        if payload.status == core.STATUS_OPEN:
            if core.has_other_open(db, row):
                raise clash
            row.closed_by = None
            row.closed_at = None
        else:
            row.closed_by = principal.email
            row.closed_at = utcnow()
        row.status = payload.status
    if changed or note is not None:
        row.note = note
        # The log keeps the step, which the row's own fields lose at the next
        # change (spec C-4): a reopen used to erase who had closed it and when.
        core.record_step(row, status=row.status, actor=principal.email, note=note, at=row.closed_at if changed else None)
    try:
        db.commit()
    except IntegrityError:
        # The check above looked a moment too early: another open request of
        # the kind landed since, and the database's own rule refused this one.
        db.rollback()
        raise clash from None
    return _outs_for(db, [request_id])[0]
