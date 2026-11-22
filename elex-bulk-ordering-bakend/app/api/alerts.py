"""Alerts API — the "needs attention" queue (ROADMAP §2 Phase 11).

Mirrors the queue the operator already works from in their existing portal: a
list that defaults to what is still open, a count they can read at a glance, and
a way to close a row and to put it back. The rules themselves live in
:mod:`app.core.alerts`; this module is only the way in.

Two deliberate choices about the filters:

* **The default list is the open queue.** ``GET /alerts`` with no parameters
  answers the only question the page is ever opened to ask. The closed rows are
  still reachable (``?resolved=true``), and ``?resolved=`` — blank — is
  everything.
* **A blank filter is no filter.** ``?kind=&severity=`` is what a UI sends when
  its dropdowns are on "any", and 422-ing that would make the page's empty
  state an error. An unknown *non-blank* severity is still a 422 naming the
  valid values, the same as ``accounts._parse_statuses``: silently dropping it
  would answer with the wrong rows and no hint why.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.core import alerts as engine
from app.core.security import require_user
from app.db.base import get_db
from app.db.models import Account, Alert, AlertSeverity
from app.schemas.alert import AlertOut, AlertSummaryOut, EvaluateOut, ResolveIn, ResolveManyIn, ResolveManyOut

log = logging.getLogger("ordering.alerts")

router = APIRouter(
    prefix="/alerts",
    tags=["alerts"],
    dependencies=[Depends(require_user)],
)

#: ``?resolved=`` values meaning "both". Blank is the one a UI actually sends;
#: the words are for a human typing the URL.
_RESOLVED_ANY = {"", "any", "all"}
_RESOLVED_TRUE = {"true", "1", "yes", "on", "t", "y"}
_RESOLVED_FALSE = {"false", "0", "no", "off", "f", "n"}


def _parse_resolved(raw: str | None) -> bool | None:
    """Tri-state ``resolved`` filter: True, False, or None for "both"."""
    if raw is None:
        return False  # the queue — what the page is opened for
    value = raw.strip().lower()
    if value in _RESOLVED_ANY:
        return None
    if value in _RESOLVED_TRUE:
        return True
    if value in _RESOLVED_FALSE:
        return False
    raise HTTPException(
        status_code=422,
        detail=f"Unknown resolved '{raw}'. Valid values: true, false, any",
    )


def _parse_severity(raw: str | None) -> AlertSeverity | None:
    """Validate a severity filter; blank means no filter."""
    if raw is None or not raw.strip():
        return None
    try:
        return AlertSeverity(raw.strip().lower())
    except ValueError as exc:
        valid = ", ".join(severity.value for severity in AlertSeverity)
        raise HTTPException(
            status_code=422,
            detail=f"Unknown severity '{raw}'. Valid values: {valid}",
        ) from exc


def _get(db: DbSession, alert_id: int) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found")
    return alert


_ORDERS = ("updated", "created")


@router.get("", response_model=list[AlertOut])
def list_alerts(
    resolved: str | None = Query(default=None, description="true, false or any"),
    kind: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    order: str = Query(
        default="updated",
        description="updated: most recently changed first; created: most recently raised first",
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    db: DbSession = Depends(get_db),
) -> list[AlertOut]:
    """The queue, newest first.

    Ordered by ``updated_at`` by default rather than ``created_at``: a reopened
    alert is news again, and burying it under rows that have not moved in
    weeks is how a recurring problem gets missed. ``updated_at`` moves only
    when something about the alert changed — a pass that merely confirms the
    problem touches ``last_seen_at`` — so the order survives the passes that
    run after every read-back. ``order=created`` is "most recently raised", for
    the dashboard's latest-three. ``id`` breaks ties, because one
    :func:`app.core.alerts.evaluate` pass stamps every row it raises with the
    same instant and an unstable order would shuffle the page between polls.
    """
    want_resolved = _parse_resolved(resolved)
    want_severity = _parse_severity(severity)
    if order not in _ORDERS:
        raise HTTPException(
            status_code=422, detail=f"Unknown order '{order}'. Valid values: {', '.join(_ORDERS)}"
        )

    stmt = select(Alert)
    if want_resolved is not None:
        stmt = stmt.where(Alert.resolved.is_(want_resolved))
    if kind and kind.strip():
        stmt = stmt.where(Alert.kind == kind.strip())
    if want_severity is not None:
        stmt = stmt.where(Alert.severity == want_severity)
    column = Alert.created_at if order == "created" else Alert.updated_at
    stmt = stmt.order_by(column.desc(), Alert.id.desc()).limit(limit)

    alerts = db.scalars(stmt).all()
    emails = _emails_for(db, {a.account_id for a in alerts if a.account_id is not None})
    out: list[AlertOut] = []
    for alert in alerts:
        item = AlertOut.model_validate(alert)
        item.account_email = emails.get(alert.account_id) if alert.account_id is not None else None
        out.append(item)
    return out


def _emails_for(db: DbSession, account_ids: set[int]) -> dict[int, str]:
    """The emails behind a page of alerts, in one query rather than one per row."""
    if not account_ids:
        return {}
    rows = db.execute(select(Account.id, Account.email).where(Account.id.in_(account_ids))).all()
    return {account_id: email for account_id, email in rows}


@router.get("/summary", response_model=AlertSummaryOut)
def alerts_summary(db: DbSession = Depends(get_db)) -> AlertSummaryOut:
    """Counters for the queue header, as SQL aggregates (no rows loaded)."""
    return AlertSummaryOut.model_validate(engine.summary(db))


@router.post("/evaluate", response_model=EvaluateOut)
def evaluate_alerts(db: DbSession = Depends(get_db)) -> EvaluateOut:
    """Re-run every rule: raise what is wrong now, close what is not.

    Safe to call as often as the operator likes — a pass is an upsert keyed on
    each problem's identity, so nothing is duplicated and nothing that is still
    broken gets lost.
    """
    try:
        counts = engine.evaluate(db)
    except IntegrityError:
        # Another writer (a job raising an ad-hoc alert as the button was
        # pressed) won the race for a new key. It is there now, so a second
        # pass finds it and updates it instead of inserting.
        db.rollback()
        counts = engine.evaluate(db)
    return EvaluateOut(counts=counts, open=engine.open_count(db))


@router.post("/resolve", response_model=ResolveManyOut)
def resolve_alerts(
    payload: ResolveManyIn,
    db: DbSession = Depends(get_db),
    who: str = Depends(require_user),
) -> ResolveManyOut:
    """Close several alerts at once — the rows the operator sees and has dealt
    with — under one reason. Ids that are unknown or already closed are passed
    over; the answer says how many this call closed."""
    closed = engine.resolve_many(db, payload.ids, note=payload.note, who=who)
    return ResolveManyOut(resolved=closed, open=engine.open_count(db))


@router.post("/{alert_id}/resolve", response_model=AlertOut)
def resolve_alert(
    alert_id: int,
    payload: ResolveIn | None = None,
    db: DbSession = Depends(get_db),
    who: str = Depends(require_user),
) -> AlertOut:
    """Close one alert, with the operator's reason when they give one, and
    their email on the row. Idempotent: already-closed answers 200, unchanged."""
    alert = _get(db, alert_id)
    if engine.resolve_alert(db, alert.key, note=payload.note if payload else None, who=who):
        log.info("alert %s resolved from the API by %s", alert_id, who)
    db.refresh(alert)
    return AlertOut.model_validate(alert)


@router.post("/{alert_id}/reopen", response_model=AlertOut)
def reopen_alert(alert_id: int, db: DbSession = Depends(get_db)) -> AlertOut:
    """Put one alert back in the queue.

    For "no, this still needs attention". A rule-owned alert will be closed
    again by the next evaluate if the data says the condition is gone — the
    rules stay the authority on their own kinds.
    """
    alert = _get(db, alert_id)
    if engine.reopen_alert(db, alert.key):
        log.info("alert %s reopened from the API", alert_id)
    db.refresh(alert)
    return AlertOut.model_validate(alert)
