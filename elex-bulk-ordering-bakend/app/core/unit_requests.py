"""Cancel and reschedule requests on order units (spec FR-020, decision C-4).

Nothing here talks to Flipkart. A request is a note in the ledger: "someone
please cancel this unit" or "please move its delivery to this date", with the
reason, who asked and when. A person then does it on the platform by hand and
marks the request done (or drops it) under Requests. The rows are the log of
that hand-off, which the owner's existing build keeps in people's heads.

One rule: a unit holds at most one OPEN request per kind. A second cancel on
a unit whose first cancel is still open is skipped with the reason, so a bulk
action over a page never doubles up what is already queued. The check here is
a read before a write, so the database keeps the rule too — a unique index on
the open rows (app.db.models.UnitRequest) — for two operators submitting the
same request at the same moment; app.api.requests turns its refusal into a 409.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession, selectinload

from app.core.order_status import SETTLED, classify
from app.db.models import OrderStatus, OrderUnit, UnitRequest, UnitRequestEvent, naive_utcnow, utcnow

log = logging.getLogger("ordering.requests")

KIND_CANCEL = "cancel"
KIND_RESCHEDULE = "reschedule"
KINDS: tuple[str, ...] = (KIND_CANCEL, KIND_RESCHEDULE)

STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_DROPPED = "dropped"
STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_DONE, STATUS_DROPPED)

#: A unit in a settled state — delivered, cancelled, returned or failed —
#: cannot be cancelled any more (FR-020: cancel only on undelivered rows), so a
#: cancel request on it is skipped, naming the state, rather than recorded for
#: someone to find out. One reading of "settled" for the whole portal.
REFUSE_CANCEL_STATES = SETTLED

#: Ids per IN clause: SQLite's bound-parameter ceiling is 999 on older builds.
_CHUNK = 500


@dataclass(frozen=True)
class Skipped:
    """A unit the request was not recorded for, and the plain reason."""

    unit_pk: int
    why: str


def _state(unit: OrderUnit) -> str:
    return unit.delivery_state or classify(unit.platform_status)


def open_kinds_by_unit(db: DbSession, unit_pks: Iterable[int]) -> dict[int, list[str]]:
    """The kinds of open request on each of these units — one query per chunk.

    What the ledger page shows as a "Cancel requested" tag: fetched for the
    page's units at once rather than per row.
    """
    wanted = sorted({int(pk) for pk in unit_pks})
    found: dict[int, list[str]] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        rows = db.execute(
            select(UnitRequest.unit_pk, UnitRequest.kind)
            .where(UnitRequest.status == STATUS_OPEN, UnitRequest.unit_pk.in_(chunk))
            .distinct()
        ).all()
        for pk, kind in rows:
            found.setdefault(int(pk), []).append(kind)
    for kinds in found.values():
        kinds.sort()
    return found


#: The outcomes of an attempt this tool stopped before Flipkart took anything.
_NEVER_SENT = (OrderStatus.failed, OrderStatus.over_target)


def never_reached_flipkart(unit: OrderUnit) -> bool:
    """Whether the unit belongs to an attempt Flipkart never took: one that
    failed, or stopped over its target, with no Flipkart order id. A request
    on it would send a person to cancel an order that does not exist (spec
    FR-020; review of 27 Oct 2026). An unconfirmed order may exist, so it is
    not one of these; an order Flipkart took and later cancelled has its id."""
    order = unit.order
    return order is not None and order.status in _NEVER_SENT and not order.order_id


def create_requests(
    db: DbSession,
    *,
    unit_pks: Sequence[int],
    kind: str,
    reason: str | None,
    requested_for: date | None,
    requested_by: str,
) -> tuple[list[UnitRequest], list[Skipped]]:
    """Record one request per unit; say why for each unit that got none.

    Skipped, with the reason: a unit that does not exist, a unit that already
    has an open request of this kind, a unit of an attempt that never reached
    Flipkart (:func:`never_reached_flipkart`: there is no order there to
    cancel or reschedule), and — for a cancel — a unit that is already
    settled (delivered, cancelled, returned or failed). Repeats in the list
    count once. The rows are added to the session; the caller commits —
    and translates the database's refusal of a second open request of one kind
    (a rival write between this check and the commit) into the same answer.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown request kind {kind!r}")
    wanted = list(dict.fromkeys(int(pk) for pk in unit_pks))
    units: dict[int, OrderUnit] = {}
    already: set[int] = set()
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        for unit in db.scalars(
            select(OrderUnit).options(selectinload(OrderUnit.order)).where(OrderUnit.id.in_(chunk))
        ).all():
            units[unit.id] = unit
        already.update(
            int(pk)
            for pk in db.scalars(
                select(UnitRequest.unit_pk).where(
                    UnitRequest.kind == kind,
                    UnitRequest.status == STATUS_OPEN,
                    UnitRequest.unit_pk.in_(chunk),
                )
            ).all()
        )

    created: list[UnitRequest] = []
    skipped: list[Skipped] = []
    for pk in wanted:
        unit = units.get(pk)
        if unit is None:
            skipped.append(Skipped(pk, "no such unit"))
            continue
        if pk in already:
            skipped.append(Skipped(pk, f"already has an open {kind} request"))
            continue
        if never_reached_flipkart(unit):
            skipped.append(Skipped(pk, "never reached Flipkart: there is no order to " + kind))
            continue
        if kind == KIND_CANCEL and _state(unit) in REFUSE_CANCEL_STATES:
            skipped.append(Skipped(pk, f"already {_state(unit)}"))
            continue
        now = utcnow()
        row = UnitRequest(
            unit_pk=pk,
            kind=kind,
            reason=reason,
            requested_for=requested_for,
            status=STATUS_OPEN,
            requested_by=requested_by,
            requested_at=now,
        )
        # The log's first step: opened, by whom and when (spec C-4).
        record_step(row, status=STATUS_OPEN, actor=requested_by, note=None, at=now)
        db.add(row)
        created.append(row)
    return created, skipped


def record_step(row: UnitRequest, *, status: str, actor: str, note: str | None, at: datetime | None = None) -> None:
    """Log one step of a request — the status it now stands at, who took the
    step, when and the note written with it — beside the row itself, which
    keeps only where it stands (spec C-4: "a status log").

    The time is kept as plain UTC with no zone (app.db.models.naive_utcnow):
    the steps are put in order by it, and PostgreSQL writes a zoned time into
    such a column at its session's wall clock, which one connection can have
    moved — two steps a second apart then came back hours out of order."""
    when = at or naive_utcnow()
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    row.events.append(UnitRequestEvent(status=status, actor=actor, at=when, note=note))


def history(db: DbSession, request_ids: Iterable[int]) -> dict[int, list[UnitRequestEvent]]:
    """Every step of these requests, oldest first, by request: one query per
    chunk of requests, so a page of the log costs one query more, not one per
    row. In the order the steps were written, which is the order they were
    taken (a request's earlier steps are filled in at the start-up before it
    can take a new one), whatever the clock of the database's session."""
    wanted = sorted({int(pk) for pk in request_ids})
    found: dict[int, list[UnitRequestEvent]] = {}
    for start in range(0, len(wanted), _CHUNK):
        rows = db.scalars(
            select(UnitRequestEvent)
            .where(UnitRequestEvent.request_id.in_(wanted[start : start + _CHUNK]))
            .order_by(UnitRequestEvent.request_id, UnitRequestEvent.id)
        ).all()
        for event in rows:
            found.setdefault(event.request_id, []).append(event)
    return found


def backfill_history(db: DbSession) -> int:
    """Give every request older than the log the steps its row still tells:
    opened (by whom, when) and, for one closed now, its close (by whom, when,
    the note). An earlier close a reopen erased is gone for good. Requests
    that already have steps are left alone, so this runs at every start.
    Returns how many requests it filled in."""
    logged = select(UnitRequestEvent.request_id).distinct()
    rows = db.scalars(select(UnitRequest).where(UnitRequest.id.not_in(logged))).all()
    for row in rows:
        record_step(row, status=STATUS_OPEN, actor=row.requested_by, note=None, at=row.requested_at)
        if row.status != STATUS_OPEN:
            record_step(
                row,
                status=row.status,
                actor=row.closed_by or row.requested_by,
                note=row.note,
                at=row.closed_at or row.requested_at,
            )
    db.commit()
    return len(rows)


def backfill_history_in_own_session() -> int:
    """:func:`backfill_history` at start-up, in a session of its own. Never
    stops a start: a log that cannot be filled in now is filled in at the next."""
    from app.db.base import SessionLocal  # local: the engine is built from settings at import

    try:
        with SessionLocal() as db:
            filled = backfill_history(db)
    except SQLAlchemyError as exc:
        log.warning("could not fill in the request log (%s)", type(exc).__name__)
        return 0
    if filled:
        log.info("filled in the log of %d earlier request(s)", filled)
    return filled


def has_other_open(db: DbSession, row: UnitRequest) -> bool:
    """Another open request of the same kind on the same unit — what stops a
    reopen from breaking the one-open-per-kind rule."""
    other = db.scalar(
        select(UnitRequest.id).where(
            UnitRequest.unit_pk == row.unit_pk,
            UnitRequest.kind == row.kind,
            UnitRequest.status == STATUS_OPEN,
            UnitRequest.id != row.id,
        )
    )
    return other is not None
