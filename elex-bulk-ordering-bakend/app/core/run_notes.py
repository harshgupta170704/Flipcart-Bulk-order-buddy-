"""What a run said about one account's order attempt, found again later.

A run writes its verdict for an account on that account's line of the run
(``JobItem.message``: "not placed (failed): Cash on Delivery is not available
for this product — nothing was charged"). The order row keeps the outcome but
not the words, so until 11 Nov 2026 the ledger, the row dialog and the alert
could only say "failed" — the reason was on a page the operator had to find by
run number. This reads it back for any number of order rows in one query.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session as DbSession

from app.db.models import JobItem, Order

#: Ids per IN clause: SQLite's bound-parameter ceiling is 999 on older builds,
#: and each pair binds two.
_CHUNK = 400

#: The verdict a run's line opens with, which the row's own Outcome already
#: says: "not placed (failed): ", "not placed (over target): ", "unconfirmed: ".
_VERDICT = re.compile(r"^\s*(?:not\s+placed\s*\([^)]*\)|unconfirmed|failed|skipped)\s*:\s*", re.IGNORECASE)


def reason_text(message: str | None) -> str | None:
    """A run's line without the verdict it opens with, first letter
    capitalised: "Cash on Delivery is not available for this product — nothing
    was charged". None for a blank line."""
    text = _VERDICT.sub("", (message or "").strip(), count=1).strip()
    if not text:
        return None
    return text[0].upper() + text[1:]


def messages_for(db: DbSession, orders: Iterable[Order]) -> dict[int, str]:
    """``{order row id: what its run said about its account}`` for the orders
    a run recorded — one query per chunk of them, never one per order. An
    order no run placed (read back, uploaded, a command-line rehearsal) has
    no line and is left out."""
    wanted: dict[tuple[int, int], list[int]] = {}
    for order in orders:
        if order.job_id is None or order.account_id is None:
            continue
        wanted.setdefault((int(order.job_id), int(order.account_id)), []).append(int(order.id))
    found: dict[int, str] = {}
    pairs = sorted(wanted)
    for start in range(0, len(pairs), _CHUNK):
        chunk = pairs[start : start + _CHUNK]
        rows = db.execute(
            select(JobItem.job_id, JobItem.account_id, JobItem.message).where(
                tuple_(JobItem.job_id, JobItem.account_id).in_(chunk),
                JobItem.message.is_not(None),
            )
        ).all()
        for job_id, account_id, message in rows:
            text = (message or "").strip()
            if not text:
                continue
            for order_pk in wanted.get((int(job_id), int(account_id)), ()):
                found[order_pk] = text
    return found
