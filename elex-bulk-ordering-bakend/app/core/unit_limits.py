"""Per-account unit limits — "how many of this may this account still buy?"

Platforms cap what a single account may buy of a category/brand/model in a
rolling window (20 handsets / 31 days, 16 vivo / 31 days, 5 of one model /
30 days). Going over gets the order rejected or the account flagged, so a run
has to ask *before* it spends an account's quota rather than discovering the
cap by tripping it (ROADMAP §2 Phase 9).

Two rules decide what a limit covers:

* **An empty value is a wildcard** inside its scope, so "no more than 30 units
  of anything a month" is ``scope=category, value=""``, while "20 handsets /
  31 days" is ``scope=category, value="Mobile", window_days=31``. A wildcard
  caps the account's whole activity in that scope — it is one shared cap, not a
  separate cap per value.
* **Matching is case-insensitive**, because "vivo", "Vivo" and "VIVO" are one
  brand to the operator; treated as three rules each would see a third of the
  orders and none of them would ever bite.

Counting is always a SQL aggregate. :func:`allowance` is called once per
account on batches of thousands, so loading order rows to add them up in
Python is the difference between a run that starts and a run that hangs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, String, and_, case, cast, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session as DbSession, aliased

from app.db.models import Account, LimitScope, Order, OrderStatus, UnitLimit, utcnow

log = logging.getLogger("ordering.limits")

#: Statuses that consume quota. ``pending`` counts on purpose: an order that is
#: in flight is already spoken for as far as the platform is concerned, and
#: ignoring it is exactly how a concurrent run spends the same unit twice.
#: ``unconfirmed`` counts for the same reason — it was submitted and may exist.
#: ``failed`` and ``failed_over_target`` never reached the platform, so they
#: give their units back.
COUNTED_STATUSES: tuple[OrderStatus, ...] = (
    OrderStatus.placed,
    OrderStatus.pending,
    OrderStatus.unconfirmed,
)

#: Scope -> the ``Order`` column it counts over.
#: ``variant`` is absent deliberately: neither ``Order`` nor :class:`ProductFacts`
#: carries a variant yet, so a variant-scoped limit is stored and listed but
#: reports as *not applicable* instead of silently counting every order.
_SCOPE_COLUMN: dict[LimitScope, InstrumentedAttribute] = {
    LimitScope.category: Order.category,
    LimitScope.brand: Order.brand,
    LimitScope.model: Order.model,
    # The seller a unit was bought from, as the read-back records it.
    LimitScope.seller: Order.seller,
}

#: Scope -> the :class:`ProductFacts` field a limit of that scope matches against.
_SCOPE_FACT: dict[LimitScope, str] = {
    LimitScope.category: "category",
    LimitScope.brand: "brand",
    LimitScope.model: "model",
    LimitScope.seller: "seller",
}

#: Scopes this module can actually enforce, in broad-to-narrow order.
ENFORCEABLE_SCOPES: tuple[LimitScope, ...] = tuple(_SCOPE_FACT)

#: How many ids go into one IN clause. Well under both SQLite's and
#: PostgreSQL's bind-parameter ceilings, which a 5000-account run would hit.
_IN_CHUNK = 900


@dataclass(frozen=True)
class ProductFacts:
    """What is being ordered, in the terms a limit matches on.

    Frozen because it is read concurrently by every worker in a run: a fact
    that could change mid-run would make two accounts measure against
    different rules.
    """

    brand: str | None = None
    category: str | None = None
    model: str | None = None
    #: The seller the run buys from (its chosen one); None for Flipkart's default.
    seller: str | None = None

    def value_for(self, scope: LimitScope) -> str | None:
        """The fact a limit of this scope matches against, or None if unknown.

        Blank and whitespace-only facts read as unknown rather than as the
        empty value, so a missing brand can never match a ``value=""`` rule by
        accident.
        """
        field = _SCOPE_FACT.get(scope)
        raw = getattr(self, field, None) if field else None
        return (raw or "").strip() or None


@dataclass(frozen=True)
class LimitUsage:
    """One limit measured against a selection of accounts."""

    limit: UnitLimit
    used: int
    remaining: int

    @property
    def blocked(self) -> bool:
        """True when the selection has no room left under this limit."""
        return self.remaining <= 0


# --- SQL building blocks ------------------------------------------------------
def _units() -> ColumnElement[int]:
    """The unit count of an order row.

    ``Order.qty`` is nullable, and a row with no quantity is still one unit
    that the platform saw. Counting it as zero would under-spend the quota,
    which is the dangerous direction: it lets a run place one order too many.
    """
    return func.coalesce(Order.qty, 1)


def _wildcard() -> ColumnElement[bool]:
    """True for a limit whose value covers every value in its scope."""
    return func.coalesce(func.trim(UnitLimit.value), "") == ""


def _window_start(limit: UnitLimit) -> datetime:
    """The oldest moment that still counts against ``limit``.

    A zero or missing window is read as one day rather than as "no window",
    because an unbounded window would count every order the account ever
    placed and permanently block it.
    """
    return utcnow() - timedelta(days=max(1, int(limit.window_days or 1)))


def _in_window(limit: UnitLimit) -> ColumnElement[bool]:
    """Orders inside the limit's rolling window.

    ``placed_at`` is NULL on a row that has not been placed yet (a pending
    order mid-run) and on over-target rows. The pending ones still have to
    count — they are in flight and their units are spoken for — so those rows
    fall back to **identity order**: ids are issued in creation order, so a row
    whose id is at or after the first id placed inside the window was itself
    created inside the window. An older NULL-dated row (an abandoned run from
    months ago) then falls outside, instead of blocking the account forever.

    When nothing at all was placed inside the window there is no id to anchor
    to and every NULL-dated row counts: over-counting costs one order,
    under-counting double-spends the account's quota.
    """
    start = _window_start(limit)
    # An alias keeps this subquery from being auto-correlated to the outer
    # ``orders`` row — it is a clock reading over the whole table, not a
    # per-row lookup.
    clock = aliased(Order)
    anchor = func.coalesce(
        select(func.min(clock.id)).where(clock.placed_at >= start).scalar_subquery(), 0
    )
    return or_(
        Order.placed_at >= start,
        and_(Order.placed_at.is_(None), Order.id >= anchor),
    )


def _counted_orders(limit: UnitLimit) -> list[ColumnElement[bool]] | None:
    """WHERE terms selecting the orders ``limit`` counts.

    None means the limit cannot be counted at all (an unmapped scope such as
    ``variant``) — distinct from an empty narrowing, which would mean "count
    everything" and is how a wildcard is expressed.

    The value match is ``lower(col) = lower(value)`` rather than ``ilike``: on
    SQLite ``ilike`` compiles to LIKE, where a model name containing ``_`` or
    ``%`` would turn into a wildcard and quietly count other models too.
    """
    column = _SCOPE_COLUMN.get(limit.scope)
    if column is None:
        return None

    terms: list[ColumnElement[bool]] = [
        Order.status.in_(COUNTED_STATUSES),
        _in_window(limit),
    ]
    value = (limit.value or "").strip()
    if value:
        if limit.scope == LimitScope.model:
            # An order row keeps Flipkart's whole product title ("vivo T5x 5G
            # (Blue, 128 GB)"), a model limit names the model ("T5x 5G"): the
            # title must contain it. Escaped, so "_" or "%" in a model name
            # stays a character (audit of 19 Nov 2026: model limits never matched).
            terms.append(
                func.lower(func.coalesce(column, "")).contains(value.lower(), autoescape=True)
            )
        else:
            terms.append(func.lower(func.coalesce(column, "")) == value.lower())
    return terms


def model_matches(limit_value: str | None, model: str | None) -> bool:
    """Whether a model limit's value names this product: the product's model
    or title contains it, case-insensitively. Blank on either side is no match."""
    value = (limit_value or "").strip().lower()
    fact = (model or "").strip().lower()
    return bool(value) and bool(fact) and value in fact


def _remaining(limit: UnitLimit, used: int) -> int:
    """Units left for one account under ``limit`` — never negative.

    An account can end up past its cap (an order placed outside this tool, a
    cap lowered after the fact). Returning a negative number would let it
    cancel out a tighter limit in :func:`allowance`, so it clamps at 0.
    """
    return max(0, int(limit.max_units or 0) - used)


# --- The engine ---------------------------------------------------------------
def applicable(db: DbSession, facts: ProductFacts) -> list[UnitLimit]:
    """Enabled limits that govern an order of ``facts``.

    A limit applies when its scope's fact matches its value case-insensitively,
    or when its value is the wildcard. A wildcard applies even when the fact is
    unknown — it caps the scope as a whole, so whatever this order turns out to
    be it lands inside it — while a *named* value needs a fact to match
    against and is skipped when we do not have one.
    """
    clauses: list[ColumnElement[bool]] = []
    for scope in ENFORCEABLE_SCOPES:
        fact = facts.value_for(scope)
        if scope == LimitScope.model and fact is not None:
            # Every model limit is read, and the named ones are matched below
            # by containment — in the same one query.
            clauses.append(UnitLimit.scope == scope)
        elif fact is None:
            clauses.append(and_(UnitLimit.scope == scope, _wildcard()))
        else:
            clauses.append(
                and_(
                    UnitLimit.scope == scope,
                    or_(_wildcard(), func.lower(UnitLimit.value) == fact.lower()),
                )
            )

    stmt = (
        select(UnitLimit)
        .where(UnitLimit.enabled.is_(True), or_(*clauses))
        .order_by(cast(UnitLimit.scope, String).asc(), UnitLimit.value.asc(), UnitLimit.id.asc())
    )
    model = facts.value_for(LimitScope.model)
    # A named model limit applies when the product's model or title contains
    # it: "T5x 5G" covers "vivo T5x 5G (Blue, 128 GB)" (audit of 19 Nov 2026).
    return [
        limit
        for limit in db.scalars(stmt).all()
        if limit.scope != LimitScope.model
        or not (limit.value or "").strip()
        or model_matches(limit.value, model)
    ]


def units_used(
    db: DbSession, account_id: int, limit: UnitLimit, facts: ProductFacts
) -> int:
    """Units this account has already committed inside ``limit``'s window.

    A SUM of quantities, not a count of rows: one order of 5 handsets spends
    five of the twenty.

    ``facts`` is part of the shared signature and is not needed for the three
    scopes that map to a column — a wildcard limit caps the account's whole
    activity in its scope regardless of what is being ordered now — but it
    keeps callers uniform and is what a future ``variant`` scope would match on.
    """
    terms = _counted_orders(limit)
    if terms is None:
        log.debug(
            "limit %s has scope %s, which cannot be counted yet — treating as unused",
            limit.id,
            getattr(limit.scope, "value", limit.scope),
        )
        return 0

    stmt = select(func.coalesce(func.sum(_units()), 0)).where(
        Order.account_id == account_id, *terms
    )
    return int(db.scalar(stmt) or 0)


def allowance(db: DbSession, account_id: int, facts: ProductFacts) -> int | None:
    """How many units this account may still order of ``facts``.

    The tightest of every applicable limit, because they all have to hold at
    once. ``None`` means no limit applies (unlimited); ``0`` means the account
    is out of quota and the run must skip it rather than spend an attempt on a
    rejection.
    """
    limits = applicable(db, facts)
    if not limits:
        return None
    return min(
        _remaining(limit, units_used(db, account_id, limit, facts)) for limit in limits
    )


def allowances(
    db: DbSession, account_ids: Sequence[int], facts: ProductFacts
) -> dict[int, int | None]:
    """:func:`allowance` for many accounts, in a handful of queries.

    A run pre-checks every selected account before it starts spending, and
    ``max_accounts_per_run`` is 5000: calling :func:`allowance` per account is
    three queries each, which is 15,000 round trips before the first browser
    opens. This groups by account instead, so it is a few queries per limit
    however many accounts are selected.

    Same semantics as :func:`allowance` per account, including ``None`` for
    "no limit applies". Accounts with nothing in the window are absent from the
    GROUP BY and get their whole quota, so every requested id is in the result.
    """
    ids = [int(account_id) for account_id in dict.fromkeys(account_ids)]
    if not ids:
        return {}

    limits = applicable(db, facts)
    if not limits:
        return {account_id: None for account_id in ids}

    tightest: dict[int, int] = {}
    for limit in limits:
        terms = _counted_orders(limit)
        if terms is None:
            continue  # a limit we cannot count must not block anyone
        cap = max(0, int(limit.max_units or 0))

        used_by: dict[int, int] = {}
        for chunk in _chunks(ids, _IN_CHUNK):
            rows = db.execute(
                select(Order.account_id, func.sum(_units()))
                .where(Order.account_id.in_(chunk), *terms)
                .group_by(Order.account_id)
            ).all()
            used_by.update({int(account_id): int(used or 0) for account_id, used in rows})

        for account_id in ids:
            left = max(0, cap - used_by.get(account_id, 0))
            tightest[account_id] = min(tightest.get(account_id, left), left)

    return {account_id: tightest.get(account_id) for account_id in ids}


def _chunks(ids: list[int], size: int) -> Iterator[list[int]]:
    """Split an id list into IN-clause-sized pieces.

    Both SQLite and PostgreSQL cap bind parameters per statement, and a run may
    select 5000 accounts, so the IN list is chunked rather than trusted to fit.
    ``size`` is passed by the caller (never defaulted here) so the module-level
    chunk size stays the single place it is configured.
    """
    for start in range(0, len(ids), max(1, size)):
        yield ids[start : start + size]


def blocking_limit(
    db: DbSession, account_id: int, facts: ProductFacts
) -> UnitLimit | None:
    """The limit that leaves this account the least room, if any applies.

    A run that skips an account owes the operator the reason, and "blocked" on
    its own is not one — the message has to name the cap that bit.
    """
    limits = applicable(db, facts)
    if not limits:
        return None
    return min(
        limits, key=lambda limit: _remaining(limit, units_used(db, account_id, limit, facts))
    )


def describe(limit: UnitLimit) -> str:
    """A limit as the operator wrote it, for a skip message or a log line."""
    scope = getattr(limit.scope, "value", str(limit.scope))
    value = (limit.value or "").strip()
    subject = f"{scope} '{value}'" if value else f"any {scope}"
    return f"{limit.max_units} units of {subject} per {limit.window_days} days"


# --- Measuring a whole selection ---------------------------------------------
def selection_usage(
    db: DbSession,
    facts: ProductFacts,
    *,
    account_where: ColumnElement[bool] | None = None,
) -> list[LimitUsage]:
    """Every applicable limit measured over a selection of accounts.

    ``used`` and ``remaining`` are **totals over the selection**. A limit is a
    per-account rule, but what the operator needs before starting a run is how
    many units the selection as a whole can still absorb under it ("I want 200
    handsets; these accounts can take 137"). For a single account the two
    readings coincide, which is the case the pre-flight check is usually read
    in.

    ``account_where`` is a predicate over ``Account``, never a list of ids: at
    37k accounts the selection has to stay a subquery (ROADMAP §0). None means
    every account.
    """
    limits = applicable(db, facts)
    if not limits:
        return []

    counter = select(func.count()).select_from(Account)
    if account_where is not None:
        counter = counter.where(account_where)
    account_count = int(db.scalar(counter) or 0)

    return [
        _usage_over(db, limit, account_count=account_count, account_where=account_where)
        for limit in limits
    ]


def _usage_over(
    db: DbSession,
    limit: UnitLimit,
    *,
    account_count: int,
    account_where: ColumnElement[bool] | None,
) -> LimitUsage:
    """One limit's totals over a selection, in a single aggregate query."""
    cap = max(0, int(limit.max_units or 0))
    terms = _counted_orders(limit)
    if terms is None:
        # Nothing to count against: the whole quota reads as free.
        return LimitUsage(limit=limit, used=0, remaining=cap * account_count)

    per_account = select(
        Order.account_id.label("account_id"),
        func.sum(_units()).label("used"),
    ).where(*terms)
    if account_where is not None:
        per_account = per_account.where(
            Order.account_id.in_(select(Account.id).where(account_where))
        )
    per_account = per_account.group_by(Order.account_id).subquery()

    used_col = per_account.c.used
    # CASE rather than a two-argument max(): ``greatest`` and ``max`` disagree
    # between SQLite and PostgreSQL, and this has to read the same on both.
    spare_col = case((used_col >= cap, 0), else_=cap - used_col)

    used, spare, spent_accounts = db.execute(
        select(
            func.coalesce(func.sum(used_col), 0),
            func.coalesce(func.sum(spare_col), 0),
            func.count(),
        ).select_from(per_account)
    ).one()

    # Accounts with nothing in the window are absent from the GROUP BY, so
    # their untouched quota is added here rather than selected in SQL.
    untouched = max(0, account_count - int(spent_accounts or 0))
    return LimitUsage(
        limit=limit,
        used=int(used or 0),
        remaining=int(spare or 0) + cap * untouched,
    )
