"""HDFC card batches: ask for virtual cards, then watch the inbox for them.

A batch is "these user ids, this many cards each, of this amount, valid this
many days". It is either sent to the CorpAssist portal (app.automation.hdfc_portal,
``send="portal"``) or downloaded as HDFC's bulk sheet to upload by hand
(``send="download"``). HDFC then mails each card to its user's email, which is
the shared inbox, and Fetch (app.core.corpassist) reads it.

:func:`tick` runs every minute in the API process (app.main): it fetches the
user ids of every batch still waiting, ties the new cards to their batch
(:func:`match_cards`: same user id, same amount, arrived after the batch was
sent), and closes a batch when all its cards are in or its wait
(``hdfc_batch_wait_minutes``) is over. With ``hdfc_auto_fetch_minutes`` set it
also fetches every active user id on that period. Being one loop over the
table, a restart loses nothing: the next tick carries on.

Statuses: ``sending`` (the portal is being driven), ``waiting`` (sent; cards
are arriving), ``done``, ``failed`` (the portal refused it, or no card came),
``cancelled``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.db.models import HdfcCardBatch, HdfcChildCard, HdfcUserId, naive_utcnow
from app.io import hdfc_bulk_csv

log = logging.getLogger("ordering.hdfc_batches")

SENDING, WAITING, DONE, FAILED, CANCELLED = "sending", "waiting", "done", "failed", "cancelled"
OPEN = (SENDING, WAITING)
#: The most cards one batch may ask for.
MAX_CARDS = 1000
#: How long before "sent" a card may have arrived and still count (clock skew).
EARLY_MINUTES = 10
TICK_SECONDS = 60


class BatchRefused(ValueError):
    """The batch cannot be made as asked; the message is for the page."""


@dataclass(frozen=True)
class RowIn:
    user_id: int
    amount: float
    count: int
    valid_days: int


def plan(db: DbSession, rows: Sequence[RowIn]) -> list[dict]:
    """The batch's rows as stored, refusing what HDFC's sheet cannot carry."""
    if not rows:
        raise BatchRefused("choose at least one user id")
    total = sum(r.count for r in rows)
    if total > MAX_CARDS:
        raise BatchRefused(f"{total} cards is over the {MAX_CARDS} one batch may ask for")
    users = {u.id: u for u in db.scalars(select(HdfcUserId).where(HdfcUserId.id.in_([r.user_id for r in rows]))).all()}
    planned: list[dict] = []
    problems: list[str] = []
    for row in rows:
        user = users.get(row.user_id)
        if user is None:
            problems.append(f"user id {row.user_id} does not exist")
            continue
        missing = [what for what, value in (("Aan", user.aan), ("mobile", user.mobile)) if not value]
        if missing:
            problems.append(f"{user.username} has no {' or '.join(missing)}")
            continue
        if not user.active:
            problems.append(f"{user.username} is inactive")
            continue
        planned.append(
            {
                "user_id": user.id,
                "username": user.username,
                "amount": round(float(row.amount), 2),
                "count": int(row.count),
                "valid_days": int(row.valid_days),
            }
        )
    if problems:
        raise BatchRefused("; ".join(problems))
    return planned


def create(db: DbSession, rows: Sequence[RowIn], *, send: str, started_by: str | None) -> HdfcCardBatch:
    planned = plan(db, rows)
    now = naive_utcnow()
    batch = HdfcCardBatch(
        status=SENDING if send == "portal" else WAITING,
        send=send,
        rows=planned,
        card_count=sum(r["count"] for r in planned),
        cards_received=0,
        started_by=started_by,
        created_at=now,
        # A downloaded sheet's cards can come from the moment it is made.
        submitted_at=None if send == "portal" else now,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    log.info("HDFC batch %d: %d card(s) for %d user id(s), %s", batch.id, batch.card_count, len(planned), send)
    return batch


def sheet(db: DbSession, batch: HdfcCardBatch) -> bytes:
    """HDFC's bulk sheet for the batch, from its user ids as they are now."""
    users = {u.id: u for u in db.scalars(select(HdfcUserId).where(HdfcUserId.id.in_([r["user_id"] for r in batch.rows]))).all()}
    out: list[hdfc_bulk_csv.BulkRow] = []
    for row in batch.rows:
        user = users.get(row["user_id"])
        if user is None or not user.aan or not user.mobile:
            raise BatchRefused(f"{row['username']} no longer has an Aan and a mobile: fix the user id and retry")
        out.extend(
            hdfc_bulk_csv.BulkRow(
                aan=user.aan,
                username=user.username,
                email=user.email,
                mobile=user.mobile,
                amount=row["amount"],
                valid_days=row["valid_days"],
            )
            for _ in range(row["count"])
        )
    return hdfc_bulk_csv.build(out)


async def send_to_portal(batch_id: int, *, session_factory, portal_factory: Callable[[], object]) -> None:
    """Drive the portal for one batch; the batch says how it went."""
    from app.automation.hdfc_portal import PortalError

    with session_factory() as db:
        batch = db.get(HdfcCardBatch, batch_id)
        if batch is None or batch.status != SENDING:
            return
        try:
            data = sheet(db, batch)
        except (BatchRefused, ValueError) as exc:
            _finish(db, batch, FAILED, str(exc))
            return
    try:
        result = await portal_factory().bulk_create(data, hdfc_bulk_csv.FILE_NAME)
    except PortalError as exc:
        result = None
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 — never leave a batch "sending"
        log.exception("HDFC batch %d: the portal step failed", batch_id)
        result = None
        error = f"the portal step failed ({type(exc).__name__})"
    with session_factory() as db:
        batch = db.get(HdfcCardBatch, batch_id)
        if batch is None:
            return
        if result is None:
            _finish(db, batch, FAILED, error)
        elif not result.ok:
            batch.portal_report = "\n".join(result.report) or None
            _finish(db, batch, FAILED, result.detail)
        else:
            batch.status = WAITING
            batch.submitted_at = naive_utcnow()
            batch.error = None
            batch.portal_report = result.detail
            db.commit()
            log.info("HDFC batch %d: sent to the portal", batch_id)


def _finish(db: DbSession, batch: HdfcCardBatch, status: str, error: str | None = None) -> None:
    batch.status = status
    batch.error = (error or None) and error[:500]
    batch.finished_at = naive_utcnow()
    db.commit()
    log.info("HDFC batch %d: %s", batch.id, status)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def match_cards(db: DbSession, batch: HdfcCardBatch) -> int:
    """Tie the batch's cards that have arrived to it; returns how many it holds."""
    if batch.submitted_at is None:
        return batch.cards_received
    since = batch.submitted_at - timedelta(minutes=EARLY_MINUTES)
    held = db.scalars(select(HdfcChildCard).where(HdfcChildCard.batch_id == batch.id)).all()
    have: dict[tuple[int, float], int] = {}
    for card in held:
        key = (card.user_id, round(card.opening_amount or 0.0, 2))
        have[key] = have.get(key, 0) + 1
    user_ids = [r["user_id"] for r in batch.rows]
    free = db.scalars(
        select(HdfcChildCard)
        .where(HdfcChildCard.batch_id.is_(None))
        .where(HdfcChildCard.user_id.in_(user_ids))
        .order_by(HdfcChildCard.id.asc())
    ).all()
    taken = len(held)
    for row in batch.rows:
        key = (row["user_id"], round(row["amount"], 2))
        want = row["count"] - have.get(key, 0)
        for card in free:
            if want <= 0:
                break
            if card.batch_id is not None or card.user_id != row["user_id"]:
                continue
            if card.opening_amount is not None and round(card.opening_amount, 2) != key[1]:
                continue
            arrived = _naive_utc(card.issued_at) or _naive_utc(card.created_at)
            if arrived is not None and arrived < since:
                continue
            card.batch_id = batch.id
            want -= 1
            taken += 1
    batch.cards_received = taken
    db.commit()
    return taken


def _fetch_users(db: DbSession, users: list[HdfcUserId]) -> str | None:
    """Fetch these user ids' cards; the reason when it could not."""
    from app.core import corpassist

    if not users:
        return None
    try:
        source = corpassist.mail_source(db)
    except corpassist.InboxNotConfigured as exc:
        return str(exc)
    corpassist.fetch(db, users, source)
    return None


_LAST_AUTO: dict[str, datetime | None] = {"at": None}


def tick_once(db: DbSession, *, now: datetime | None = None) -> dict:
    """One pass: fetch what is due, match cards to open batches, close batches."""
    from app.core import corpassist

    settings = corpassist.effective_settings(db)
    now = now or naive_utcnow()
    waiting = list(db.scalars(select(HdfcCardBatch).where(HdfcCardBatch.status == WAITING)).all())
    due_ids: set[int] = {r["user_id"] for b in waiting for r in b.rows}
    auto = int(getattr(settings, "hdfc_auto_fetch_minutes", 0) or 0)
    last = _LAST_AUTO["at"]
    auto_due = auto > 0 and (last is None or now - last >= timedelta(minutes=auto))
    stmt = select(HdfcUserId).where(HdfcUserId.active.is_(True))
    if not auto_due:
        stmt = stmt.where(HdfcUserId.id.in_(due_ids or {-1}))
    users = list(db.scalars(stmt).all())
    problem = _fetch_users(db, users) if users else None
    if auto_due:
        _LAST_AUTO["at"] = now
    wait = timedelta(minutes=int(getattr(settings, "hdfc_batch_wait_minutes", 60) or 60))
    closed = 0
    for batch in waiting:
        got = match_cards(db, batch)
        if got >= batch.card_count:
            _finish(db, batch, DONE)
            closed += 1
        elif batch.submitted_at and now - batch.submitted_at > wait:
            minutes = int(wait.total_seconds() // 60)
            note = problem or f"{got} of {batch.card_count} card(s) arrived within {minutes} minutes"
            # Some cards came: done, with the shortfall noted. None: failed.
            _finish(db, batch, DONE if got else FAILED, note)
            closed += 1
        elif problem and batch.error != problem:
            batch.error = problem[:500]
            db.commit()
    return {"fetched": len(users), "waiting": len(waiting), "closed": closed, "problem": problem}


def recover(db: DbSession) -> int:
    """At start-up: a batch left "sending" by a stopped server cannot be known
    to have reached the portal, so it is failed with a reason, never resent."""
    stuck = db.scalars(select(HdfcCardBatch).where(HdfcCardBatch.status == SENDING)).all()
    for batch in stuck:
        _finish(
            db,
            batch,
            FAILED,
            "the server stopped while this batch was being sent: check Virtual Card Details on the portal before retrying",
        )
    return len(stuck)


async def run_forever(session_factory=None) -> None:
    """The API process's loop (app.main)."""
    if session_factory is None:
        from app.db.base import SessionLocal

        session_factory = SessionLocal

    def one_pass() -> None:
        with session_factory() as db:
            tick_once(db)

    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await asyncio.to_thread(one_pass)
        except Exception:  # noqa: BLE001 — the loop must outlive one bad pass
            log.exception("HDFC batches: a pass failed")
