"""Changes API — what the read-backs found since the signed-in user last looked.

``GET /changes`` lists the unit events (app.core.unit_events) recorded after
the caller's own seen mark, newest first; ``POST /changes/seen`` moves the
mark. The mark is per user (``User.changes_seen_at``): two people sharing the
deployment each see what is new to them, and one marking the list as seen
does not empty it for the other.

Bounded both ways: a page is at most :data:`MAX_SIZE` rows, and the window
never reaches further back than ``unit_events.MAX_DAYS`` (30 days), however
long ago the caller last looked. With no mark yet it is the last
``unit_events.UNSEEN_DAYS`` (7 days).

Operators and admins alike: it only reads, and the mark is the caller's own.
"""

from __future__ import annotations

import math
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core import unit_events
from app.core.security import Principal, current_user, require_user
from app.db.base import get_db
from app.db.models import Account, Order, OrderUnit, UnitEvent, User, naive_utcnow
from app.schemas.change import ChangeOut, ChangesPage, ChangesSeenIn, ChangesSeenOut

router = APIRouter(
    prefix="/changes",
    tags=["changes"],
    dependencies=[Depends(require_user)],
)

DEFAULT_SIZE = 25
MAX_SIZE = 200
#: A page number the offset arithmetic can never overflow on.
MAX_PAGE = 100_000
#: How far behind the list's own moment a seen mark is set. A read-back
#: stamps a change when its fold sees it and commits it a moment later, with
#: the rest of that account's orders; a change stamped just before a list was
#: read but committed just after it was never on that list, and a mark at the
#: list's moment would hide it for good. Behind by this much, such a change
#: shows again instead — as, at worst, does one seen in these last seconds.
#: (A cursor on the event id would not do: PostgreSQL hands out ids before
#: commit, so they too can commit out of order.)
SEEN_GRACE = timedelta(seconds=10)


def _user(db: DbSession, principal: Principal) -> User | None:
    """The caller's row; None for a token minted without one (a worker, a
    script), which has no mark of its own and sees the default window."""
    if principal.id is None:
        return None
    return db.get(User, principal.id)


@router.get("", response_model=ChangesPage)
def list_changes(
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    size: int = Query(default=DEFAULT_SIZE, ge=1, le=MAX_SIZE),
    principal: Principal = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> ChangesPage:
    """One page of the changes after the caller's seen mark, newest first.

    Ordered by when the read-back saw each change, then by id: one read-back
    stamps every change it finds on an account with the same instant, and an
    unstable order would shuffle the list between loads. The counts by kind
    cover the whole window, not just the page.
    """
    now = naive_utcnow()
    user = _user(db, principal)
    seen_at = user.changes_seen_at if user is not None else None
    since, capped = unit_events.window_start(seen_at, now)
    in_window = UnitEvent.at > since

    by_kind = {kind: 0 for kind in unit_events.KINDS}
    for kind, count in db.execute(
        select(UnitEvent.kind, func.count(UnitEvent.id)).where(in_window).group_by(UnitEvent.kind)
    ).all():
        by_kind[kind] = int(count)
    total = sum(by_kind.values())

    rows = db.execute(
        select(UnitEvent, OrderUnit.position, OrderUnit.unit_id, Order.order_id, Order.model, Order.color, Account.email)
        .join(OrderUnit, OrderUnit.id == UnitEvent.unit_pk)
        .join(Order, Order.id == UnitEvent.order_pk)
        .outerjoin(Account, Account.id == UnitEvent.account_id)
        .where(in_window)
        .order_by(UnitEvent.at.desc(), UnitEvent.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    ).all()
    items = [
        ChangeOut(
            id=event.id,
            at=event.at,
            kind=event.kind,
            from_state=event.from_state,
            to_state=event.to_state,
            unit_pk=event.unit_pk,
            unit_position=position,
            unit_id=unit_id,
            order_row_id=event.order_pk,
            order_id=order_id,
            model=model,
            color=color,
            account_id=event.account_id,
            account_email=email,
        )
        for event, position, unit_id, order_id, model, color, email in rows
    ]
    return ChangesPage(
        items=items,
        total=total,
        page=page,
        size=size,
        pages=max(1, math.ceil(total / size)),
        by_kind=by_kind,
        since=since,
        seen_at=seen_at,
        capped=capped,
        as_of=now,
    )


@router.post("/seen", response_model=ChangesSeenOut)
def mark_seen(
    payload: ChangesSeenIn | None = None,
    principal: Principal = Depends(current_user),
    db: DbSession = Depends(get_db),
) -> ChangesSeenOut:
    """Mark the caller's changes as seen: up to now, or up to ``through``.

    The dashboard sends ``through`` — the ``as_of`` of the list it showed — so
    a change a read-back records while the page is open is not cleared unread.
    The mark never moves back (a second tab holding an older list must not
    bring back what the first cleared) and never past now (a mark in the
    future would hide changes not yet made); it is set :data:`SEEN_GRACE`
    behind, for a change committed a moment after its stamp.
    """
    user = _user(db, principal)
    if user is None:
        raise HTTPException(
            status_code=409,
            detail="This sign-in has no user of its own to remember what was seen. Sign in with your email.",
        )
    now = naive_utcnow()
    through = now
    if payload is not None and payload.through is not None:
        through = min(unit_events.naive_utc(payload.through), now)
    through -= SEEN_GRACE
    seen = user.changes_seen_at
    if seen is None or through > seen:
        user.changes_seen_at = seen = through
        db.commit()
    return ChangesSeenOut(seen_at=seen)
