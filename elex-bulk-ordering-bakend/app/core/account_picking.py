"""Which of a run's accounts to use, and in what order (step B).

Two rules an order run can ask for, both read from the orders this tool has
recorded (one query each, however many accounts the run has):

- **Exclude recently ordered**: an account with an order placed, or sent and
  never confirmed, in the last N days is passed over. Failed and over-cap
  attempts bought nothing, so they do not count.
- **Smart selection**: accounts are tried in the order they last ordered —
  never-ordered first, then the longest ago — so a run that needs only some
  of its accounts spreads the buying instead of always using the same ones.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.db.models import Order, OrderStatus, naive_utcnow

#: Order outcomes that mean something may have been bought on the account.
BOUGHT = (OrderStatus.placed, OrderStatus.unconfirmed)

#: Ids per IN (...) clause, well under every database's parameter limit.
_CHUNK = 900


def _chunks(ids: list[int]) -> Iterable[list[int]]:
    for start in range(0, len(ids), _CHUNK):
        yield ids[start : start + _CHUNK]


def recently_ordered(db: DbSession, account_ids: Iterable[int], days: int) -> set[int]:
    """The accounts among ``account_ids`` with a bought order in the last ``days`` days."""
    ids = list(dict.fromkeys(account_ids))
    if days <= 0 or not ids:
        return set()
    since = naive_utcnow() - timedelta(days=days)
    found: set[int] = set()
    for part in _chunks(ids):
        found.update(
            db.scalars(
                select(Order.account_id)
                .where(Order.account_id.in_(part))
                .where(Order.status.in_(BOUGHT))
                .where(Order.created_at >= since)
                .distinct()
            )
        )
    return found


def least_recently_ordered_first(db: DbSession, account_ids: list[int]) -> list[int]:
    """``account_ids`` reordered: never ordered first, then oldest last order.

    Ties keep the run's own order, so the same selection is always tried the
    same way.
    """
    ids = list(dict.fromkeys(account_ids))
    last: dict[int, object] = {}
    for part in _chunks(ids):
        rows = db.execute(
            select(Order.account_id, func.max(Order.created_at))
            .where(Order.account_id.in_(part))
            .where(Order.status.in_(BOUGHT))
            .group_by(Order.account_id)
        ).all()
        last.update({account_id: when for account_id, when in rows if when is not None})
    position = {account_id: index for index, account_id in enumerate(ids)}
    return sorted(ids, key=lambda a: (a in last, last.get(a) or 0, position[a]))
