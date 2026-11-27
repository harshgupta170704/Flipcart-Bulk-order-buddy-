"""Which of a run's accounts to use, and in what order (step B).

Two rules an order run can ask for, both read from the orders this tool has
recorded (one query each, however many accounts the run has):

- **Exclude recently ordered**: an account with an order placed, or sent and
  never confirmed, in the last N days is passed over. Failed and over-cap
  attempts bought nothing, so they do not count.
- **Smart selection**: accounts are tried in the order they last ordered —
  never-ordered first, then the longest ago — so a run that needs only some
  of its accounts spreads the buying instead of always using the same ones.
- **Skip accounts that already bought this product**: an account with an order
  placed (or sent and never confirmed) for the run's own product, at any time,
  is passed over. "The same product" is read off the links (``same_product``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import func, or_, select
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


# --- "already bought this product" -------------------------------------------

#: A Flipkart product id (``pid=``): letters and digits only.
_PID_RE = re.compile(r"[A-Za-z0-9]{6,40}")
#: The product page's own id in the path: ``/p/itm5420c578ec2df``.
_ITEM_RE = re.compile(r"/p/(itm[A-Za-z0-9]{6,40})(?:[/?#]|$)")


@dataclass(frozen=True)
class ProductKey:
    """What identifies a product in a Flipkart link.

    ``pid`` names the exact variant (colour, storage); ``item`` is the product
    page, which several variants can share; ``link`` is the whole link, kept
    for a short link that carries neither.
    """

    pid: str | None
    item: str | None
    link: str

    @property
    def readable(self) -> bool:
        return bool(self.pid or self.item)


def product_key(link: str | None) -> ProductKey:
    text = (link or "").strip()
    try:
        parts = urlsplit(text)
        pids = parse_qs(parts.query).get("pid", [])
    except ValueError:
        return ProductKey(None, None, text)
    pid = next((p.strip().upper() for p in pids if _PID_RE.fullmatch(p.strip())), None)
    found = _ITEM_RE.search(parts.path or "")
    item = found.group(1).lower() if found else None
    return ProductKey(pid, item, text)


def same_product(run: ProductKey, other: str | None) -> bool:
    """Whether the link ``other`` names the run's product.

    Both links carry a ``pid=``: the variant must match, so a run for the blue
    phone does not pass over an account that bought the black one. Otherwise
    the product page id must match, and failing both, the whole link.
    """
    seen = product_key(other)
    if run.pid and seen.pid:
        return run.pid == seen.pid
    if run.item and seen.item:
        return run.item == seen.item
    return bool(run.link) and not run.readable and run.link == seen.link


@dataclass(frozen=True)
class BoughtBefore:
    """The order that shows an account already bought the run's product."""

    order_id: str | None
    when: datetime | None

    def describe(self) -> str:
        parts = [self.order_id] if self.order_id else []
        if self.when is not None:
            parts.append(f"on {self.when:%d %b %Y}")
        return f"already bought this product ({' '.join(parts)})" if parts else "already bought this product"


def already_bought(
    db: DbSession, account_ids: Iterable[int], product_link: str | None
) -> dict[int, BoughtBefore]:
    """The accounts among ``account_ids`` that already bought ``product_link``.

    Placed and unconfirmed orders count, however long ago; failed and
    over-cap attempts bought nothing, so they do not. Each account maps to its
    latest such order. The database narrows the rows with a text match on the
    product's id; ``same_product`` then decides on each link, so a longer id
    that merely contains this one never counts.
    """
    ids = list(dict.fromkeys(account_ids))
    run = product_key(product_link)
    if not ids or not run.link:
        return {}
    link_col = func.lower(Order.product_link)
    if run.pid or run.item:
        needles = [n.lower() for n in (run.pid, run.item) if n]
        narrow = or_(*(link_col.contains(n, autoescape=True) for n in needles))
    else:
        narrow = Order.product_link == run.link
    found: dict[int, BoughtBefore] = {}
    for part in _chunks(ids):
        rows = db.execute(
            select(Order.account_id, Order.product_link, Order.order_id, Order.created_at)
            .where(Order.account_id.in_(part))
            .where(Order.status.in_(BOUGHT))
            .where(narrow)
            .order_by(Order.created_at)
        ).all()
        for account_id, link, order_id, when in rows:
            if same_product(run, link):
                found[account_id] = BoughtBefore(order_id, when)
    return found
