"""Tests for the unit-limit engine (``app.core.unit_limits``, Phase 9).

The load-bearing tests are the ones about *what counts*:

* ``test_pending_and_placed_count_but_failures_do_not`` — an in-flight order
  still consumes quota, and ignoring it is how a concurrent run spends the same
  unit twice;
* ``test_units_used_sums_quantities_not_rows`` — one order of five handsets
  spends five of the twenty, not one;
* ``test_allowance_issues_aggregates_not_row_loads`` — this runs once per
  account over batches of thousands, so counting in Python is not an option.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.orm import Session as DbSession

from app.core.unit_limits import (
    COUNTED_STATUSES,
    ProductFacts,
    allowance,
    allowances,
    applicable,
    blocking_limit,
    describe,
    selection_usage,
    units_used,
)
from app.db.base import engine
from app.db.models import (
    Account,
    AccountStatus,
    LimitScope,
    Order,
    OrderStatus,
    UnitLimit,
    utcnow,
)

_SENTINEL = object()


# --- seeding ------------------------------------------------------------------
def _account(db: DbSession, email: str = "a@test.com") -> Account:
    account = Account(email=email, phone="6000000000", status=AccountStatus.new)
    db.add(account)
    db.flush()
    return account


def _limit(
    db: DbSession,
    scope: LimitScope,
    value: str = "",
    *,
    max_units: int = 20,
    window_days: int = 30,
    enabled: bool = True,
) -> UnitLimit:
    limit = UnitLimit(
        scope=scope,
        value=value,
        max_units=max_units,
        window_days=window_days,
        enabled=enabled,
    )
    db.add(limit)
    db.flush()
    return limit


def _order(
    db: DbSession,
    account: Account,
    *,
    qty: int | None = 1,
    status: OrderStatus = OrderStatus.placed,
    brand: str | None = None,
    category: str | None = None,
    model: str | None = None,
    days_ago: float = 1,
    placed_at: datetime | None | object = _SENTINEL,
) -> Order:
    """One order row.

    ``placed_at`` defaults to ``days_ago`` for anything that reached the
    platform and to NULL for a pending row — which is what the real runner
    writes (``app.jobs.tasks.save_order_result``).
    """
    if placed_at is _SENTINEL:
        placed_at = (
            None
            if status is OrderStatus.pending
            else utcnow() - timedelta(days=days_ago)
        )
    order = Order(
        account_id=account.id,
        qty=qty,
        status=status,
        brand=brand,
        category=category,
        model=model,
        placed_at=placed_at,
    )
    db.add(order)
    db.flush()
    return order


@pytest.fixture
def account(db: DbSession) -> Account:
    return _account(db)


# --- applicable: scope matching ------------------------------------------------
def test_each_scope_matches_its_own_fact(db: DbSession) -> None:
    brand = _limit(db, LimitScope.brand, "vivo")
    category = _limit(db, LimitScope.category, "Mobile")
    model = _limit(db, LimitScope.model, "T5x 5G")

    assert [limit.id for limit in applicable(db, ProductFacts(brand="vivo"))] == [
        brand.id
    ]
    assert [limit.id for limit in applicable(db, ProductFacts(category="Mobile"))] == [
        category.id
    ]
    assert [limit.id for limit in applicable(db, ProductFacts(model="T5x 5G"))] == [
        model.id
    ]


def test_a_fact_does_not_match_another_scopes_limit(db: DbSession) -> None:
    """A brand limit must not bite because the *category* happens to say "vivo"."""
    _limit(db, LimitScope.brand, "vivo")
    assert applicable(db, ProductFacts(category="vivo", model="vivo")) == []


def test_a_named_value_only_matches_that_value(db: DbSession) -> None:
    _limit(db, LimitScope.brand, "vivo")
    assert applicable(db, ProductFacts(brand="oppo")) == []
    assert applicable(db, ProductFacts(brand="vivo-x")) == []  # no prefix matching


def test_every_applicable_limit_comes_back(db: DbSession) -> None:
    """A handset run is governed by all three caps at once."""
    ids = {
        _limit(db, LimitScope.category, "Mobile", max_units=20, window_days=31).id,
        _limit(db, LimitScope.brand, "vivo", max_units=16, window_days=31).id,
        _limit(db, LimitScope.model, "T5x 5G", max_units=5).id,
    }
    facts = ProductFacts(brand="vivo", category="Mobile", model="T5x 5G")
    assert {limit.id for limit in applicable(db, facts)} == ids


def test_a_wildcard_value_matches_any_value_in_its_scope(db: DbSession) -> None:
    blanket = _limit(db, LimitScope.category, "", max_units=30)
    for category in ("Mobile", "Laptop", "anything at all"):
        assert [limit.id for limit in applicable(db, ProductFacts(category=category))] == [
            blanket.id
        ]


def test_a_wildcard_applies_even_when_the_fact_is_unknown(db: DbSession) -> None:
    """"30 of anything a month" caps the scope, so it holds whatever this is."""
    blanket = _limit(db, LimitScope.category, "", max_units=30)
    named = _limit(db, LimitScope.category, "Mobile")

    got = {limit.id for limit in applicable(db, ProductFacts())}
    assert got == {blanket.id}
    assert named.id not in got  # nothing to match "Mobile" against


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_fact_reads_as_unknown(db: DbSession, blank: str | None) -> None:
    _limit(db, LimitScope.brand, "vivo")
    assert applicable(db, ProductFacts(brand=blank)) == []


@pytest.mark.parametrize(
    ("limit_value", "fact"),
    [("VIVO", "vivo"), ("vivo", "VIVO"), ("ViVo", "vIvO")],
)
def test_matching_is_case_insensitive(
    db: DbSession, limit_value: str, fact: str
) -> None:
    limit = _limit(db, LimitScope.brand, limit_value)
    assert [found.id for found in applicable(db, ProductFacts(brand=fact))] == [limit.id]


def test_a_disabled_limit_is_ignored(db: DbSession, account: Account) -> None:
    _limit(db, LimitScope.brand, "vivo", max_units=5, enabled=False)
    _order(db, account, qty=99, brand="vivo")

    assert applicable(db, ProductFacts(brand="vivo")) == []
    assert allowance(db, account.id, ProductFacts(brand="vivo")) is None


def test_a_variant_limit_is_not_enforced_yet(db: DbSession, account: Account) -> None:
    """No order column and no fact carries a variant, so it cannot be counted.

    Stored and listed, but reported as not applicable rather than silently
    counting every order (see the module docstring).
    """
    variant = _limit(db, LimitScope.variant, "Black 128GB", max_units=2)
    _order(db, account, qty=9, brand="vivo", category="Mobile", model="T5x 5G")

    facts = ProductFacts(brand="vivo", category="Mobile", model="T5x 5G")
    assert variant.id not in {limit.id for limit in applicable(db, facts)}
    assert units_used(db, account.id, variant, facts) == 0


# --- units_used ---------------------------------------------------------------
def test_units_used_sums_quantities_not_rows(db: DbSession, account: Account) -> None:
    limit = _limit(db, LimitScope.brand, "vivo")
    for _ in range(3):
        _order(db, account, qty=4, brand="vivo")

    assert units_used(db, account.id, limit, ProductFacts(brand="vivo")) == 12


def test_an_order_with_no_quantity_counts_as_one_unit(
    db: DbSession, account: Account
) -> None:
    """Under-counting is the dangerous direction — it places one order too many."""
    limit = _limit(db, LimitScope.brand, "vivo")
    _order(db, account, qty=None, brand="vivo")
    _order(db, account, qty=2, brand="vivo")

    assert units_used(db, account.id, limit, ProductFacts(brand="vivo")) == 3


def test_units_used_counts_only_the_scoped_value(
    db: DbSession, account: Account
) -> None:
    limit = _limit(db, LimitScope.brand, "vivo")
    _order(db, account, qty=3, brand="vivo")
    _order(db, account, qty=7, brand="oppo")

    assert units_used(db, account.id, limit, ProductFacts(brand="vivo")) == 3


def test_units_used_counts_a_scoped_value_case_insensitively(
    db: DbSession, account: Account
) -> None:
    limit = _limit(db, LimitScope.brand, "VIVO")
    _order(db, account, qty=2, brand="vivo")
    _order(db, account, qty=3, brand="ViVo")

    assert units_used(db, account.id, limit, ProductFacts(brand="vivo")) == 5


def test_a_wildcard_limit_counts_the_whole_scope(
    db: DbSession, account: Account
) -> None:
    limit = _limit(db, LimitScope.category, "", max_units=30)
    _order(db, account, qty=4, category="Mobile")
    _order(db, account, qty=6, category="Laptop")

    assert units_used(db, account.id, limit, ProductFacts(category="Mobile")) == 10


def test_units_used_counts_only_this_account(db: DbSession, account: Account) -> None:
    other = _account(db, "b@test.com")
    limit = _limit(db, LimitScope.brand, "vivo")
    _order(db, account, qty=2, brand="vivo")
    _order(db, other, qty=9, brand="vivo")

    assert units_used(db, account.id, limit, ProductFacts(brand="vivo")) == 2


# --- the window ---------------------------------------------------------------
def test_the_window_edge_is_respected_exactly(db: DbSession, account: Account) -> None:
    limit = _limit(db, LimitScope.brand, "vivo", window_days=30)
    facts = ProductFacts(brand="vivo")

    # Inside by an hour.
    _order(db, account, qty=1, brand="vivo", placed_at=utcnow() - timedelta(days=30) + timedelta(hours=1))
    assert units_used(db, account.id, limit, facts) == 1

    # Outside by a minute — the 31st day no longer counts against a 30-day cap.
    _order(db, account, qty=5, brand="vivo", placed_at=utcnow() - timedelta(days=30, minutes=1))
    assert units_used(db, account.id, limit, facts) == 1


def test_each_limit_uses_its_own_window(db: DbSession, account: Account) -> None:
    short = _limit(db, LimitScope.brand, "vivo", window_days=7)
    long = _limit(db, LimitScope.brand, "vivo", window_days=31)
    facts = ProductFacts(brand="vivo")
    _order(db, account, qty=3, brand="vivo", days_ago=20)

    assert units_used(db, account.id, short, facts) == 0
    assert units_used(db, account.id, long, facts) == 3


def test_pending_and_placed_count_but_failures_do_not(
    db: DbSession, account: Account
) -> None:
    """An in-flight order has its units spoken for; a failure gives them back.

    An unconfirmed order counts too: it was submitted and may exist, and not
    counting it is how a later run buys the same units a second time.
    """
    limit = _limit(db, LimitScope.brand, "vivo", max_units=50)
    facts = ProductFacts(brand="vivo")

    _order(db, account, qty=2, brand="vivo", status=OrderStatus.placed)
    _order(db, account, qty=3, brand="vivo", status=OrderStatus.pending)
    _order(db, account, qty=7, brand="vivo", status=OrderStatus.unconfirmed)
    _order(db, account, qty=100, brand="vivo", status=OrderStatus.failed)
    _order(db, account, qty=100, brand="vivo", status=OrderStatus.over_target)

    assert COUNTED_STATUSES == (
        OrderStatus.placed,
        OrderStatus.pending,
        OrderStatus.unconfirmed,
    )
    assert units_used(db, account.id, limit, facts) == 12


def test_an_undated_pending_order_counts(db: DbSession, account: Account) -> None:
    """``placed_at`` is NULL until the order lands, but the units are committed."""
    limit = _limit(db, LimitScope.brand, "vivo")
    _order(db, account, qty=4, brand="vivo", status=OrderStatus.pending, placed_at=None)

    assert units_used(db, account.id, limit, ProductFacts(brand="vivo")) == 4


def test_an_undated_row_older_than_the_window_falls_out(
    db: DbSession, account: Account
) -> None:
    """The identity-order fallback: ids are issued in creation order.

    A pending row left behind by a run months ago must not block the account
    forever, so a NULL-dated row only counts from the first id placed inside
    the window onwards.
    """
    limit = _limit(db, LimitScope.brand, "vivo", window_days=30)
    facts = ProductFacts(brand="vivo")

    stale = _order(
        db, account, qty=7, brand="vivo", status=OrderStatus.pending, placed_at=None
    )
    anchor = _order(db, account, qty=1, brand="vivo", days_ago=2)
    fresh = _order(
        db, account, qty=3, brand="vivo", status=OrderStatus.pending, placed_at=None
    )
    assert stale.id < anchor.id < fresh.id

    assert units_used(db, account.id, limit, facts) == 4  # anchor + fresh, not stale


def test_undated_rows_count_when_nothing_was_placed_in_the_window(
    db: DbSession, account: Account
) -> None:
    """With no id to anchor to, over-counting beats double-spending the quota."""
    limit = _limit(db, LimitScope.brand, "vivo", window_days=30)
    _order(db, account, qty=2, brand="vivo", days_ago=400)  # outside, so no anchor
    _order(db, account, qty=5, brand="vivo", status=OrderStatus.pending, placed_at=None)

    assert units_used(db, account.id, limit, ProductFacts(brand="vivo")) == 5


# --- allowance ----------------------------------------------------------------
def test_allowance_is_none_when_nothing_applies(db: DbSession, account: Account) -> None:
    _limit(db, LimitScope.brand, "oppo", max_units=5)
    assert allowance(db, account.id, ProductFacts(brand="vivo")) is None


def test_allowance_with_no_limits_configured_is_none(
    db: DbSession, account: Account
) -> None:
    assert allowance(db, account.id, ProductFacts(brand="vivo")) is None


def test_allowance_is_the_tightest_of_several(db: DbSession, account: Account) -> None:
    _limit(db, LimitScope.category, "Mobile", max_units=20, window_days=31)
    _limit(db, LimitScope.brand, "vivo", max_units=16, window_days=31)
    _limit(db, LimitScope.model, "T5x 5G", max_units=5)
    facts = ProductFacts(brand="vivo", category="Mobile", model="T5x 5G")

    assert allowance(db, account.id, facts) == 5  # the model cap is the binding one

    _order(db, account, qty=3, brand="vivo", category="Mobile", model="T5x 5G")
    assert allowance(db, account.id, facts) == 2

    # A different model of the same brand eats the brand cap, not the model cap.
    _order(db, account, qty=14, brand="vivo", category="Mobile", model="Y28")
    assert allowance(db, account.id, facts) == 0  # 16 - 17 clamped, now the tightest


def test_allowance_is_zero_when_exhausted(db: DbSession, account: Account) -> None:
    _limit(db, LimitScope.brand, "vivo", max_units=5)
    _order(db, account, qty=5, brand="vivo")

    assert allowance(db, account.id, ProductFacts(brand="vivo")) == 0


def test_allowance_never_goes_negative(db: DbSession, account: Account) -> None:
    """A cap lowered after the fact leaves accounts past it; 0 is the floor."""
    _limit(db, LimitScope.brand, "vivo", max_units=5)
    _order(db, account, qty=40, brand="vivo")

    assert allowance(db, account.id, ProductFacts(brand="vivo")) == 0


def test_allowance_is_per_account(db: DbSession, account: Account) -> None:
    other = _account(db, "b@test.com")
    _limit(db, LimitScope.brand, "vivo", max_units=5)
    _order(db, account, qty=5, brand="vivo")

    facts = ProductFacts(brand="vivo")
    assert allowance(db, account.id, facts) == 0
    assert allowance(db, other.id, facts) == 5


def test_blocking_limit_names_the_cap_that_bit(db: DbSession, account: Account) -> None:
    _limit(db, LimitScope.category, "Mobile", max_units=20, window_days=31)
    model = _limit(db, LimitScope.model, "T5x 5G", max_units=5)
    facts = ProductFacts(category="Mobile", model="T5x 5G")
    _order(db, account, qty=5, category="Mobile", model="T5x 5G")

    blocker = blocking_limit(db, account.id, facts)
    assert blocker is not None and blocker.id == model.id
    assert describe(blocker) == "5 units of model 'T5x 5G' per 30 days"
    assert blocking_limit(db, account.id, ProductFacts(brand="oppo")) is None


# --- allowances (the batched form) --------------------------------------------
def test_allowances_matches_allowance_account_by_account(db: DbSession) -> None:
    accounts = [_account(db, f"m{i}@test.com") for i in range(4)]
    _limit(db, LimitScope.brand, "vivo", max_units=10)
    _limit(db, LimitScope.model, "T5x 5G", max_units=5)
    facts = ProductFacts(brand="vivo", model="T5x 5G")

    _order(db, accounts[0], qty=3, brand="vivo", model="T5x 5G")  # 2 left on the model
    _order(db, accounts[1], qty=9, brand="vivo", model="Y28")  # 1 left on the brand
    _order(db, accounts[2], qty=20, brand="vivo", model="T5x 5G")  # spent

    ids = [account.id for account in accounts]
    batched = allowances(db, ids, facts)
    assert batched == {account_id: allowance(db, account_id, facts) for account_id in ids}
    assert batched == {ids[0]: 2, ids[1]: 1, ids[2]: 0, ids[3]: 5}


def test_allowances_is_none_for_every_account_when_nothing_applies(
    db: DbSession,
) -> None:
    ids = [_account(db, f"n{i}@test.com").id for i in range(3)]
    _limit(db, LimitScope.brand, "oppo", max_units=5)

    assert allowances(db, ids, ProductFacts(brand="vivo")) == dict.fromkeys(ids)


def test_allowances_of_nothing_is_empty(db: DbSession) -> None:
    _limit(db, LimitScope.brand, "vivo", max_units=5)
    assert allowances(db, [], ProductFacts(brand="vivo")) == {}


def test_allowances_ignores_a_disabled_limit(db: DbSession, account: Account) -> None:
    _limit(db, LimitScope.brand, "vivo", max_units=5, enabled=False)
    _order(db, account, qty=99, brand="vivo")

    assert allowances(db, [account.id], ProductFacts(brand="vivo")) == {account.id: None}


def test_allowances_chunks_a_long_id_list(db: DbSession, monkeypatch) -> None:
    """Both backends cap bind parameters, and a run may select 5000 accounts."""
    from app.core import unit_limits as engine

    monkeypatch.setattr(engine, "_IN_CHUNK", 7)
    ids = [_account(db, f"c{i:04d}@test.com").id for i in range(20)]
    _limit(db, LimitScope.brand, "vivo", max_units=4)
    _order(db, db.get(Account, ids[0]), qty=4, brand="vivo")
    db.commit()

    got = engine.allowances(db, ids, ProductFacts(brand="vivo"))
    assert len(got) == 20
    assert got[ids[0]] == 0
    assert all(got[account_id] == 4 for account_id in ids[1:])


# --- selection_usage ----------------------------------------------------------
def test_selection_usage_totals_over_several_accounts(db: DbSession) -> None:
    first, second, _untouched = (_account(db, f"s{i}@test.com") for i in range(3))
    _limit(db, LimitScope.brand, "vivo", max_units=10)
    _order(db, first, qty=3, brand="vivo")
    _order(db, second, qty=12, brand="vivo")  # past its cap

    [usage] = selection_usage(db, ProductFacts(brand="vivo"))
    assert usage.used == 15
    # 7 left on the first, 0 on the over-cap second (clamped, not -2), and a
    # full 10 on the third, which has no orders and so no GROUP BY row.
    assert usage.remaining == 17
    assert usage.blocked is False

    only_second = selection_usage(
        db, ProductFacts(brand="vivo"), account_where=Account.id == second.id
    )
    assert (only_second[0].used, only_second[0].remaining) == (12, 0)
    assert only_second[0].blocked is True


def test_selection_usage_is_empty_when_nothing_applies(db: DbSession) -> None:
    _account(db)
    _limit(db, LimitScope.brand, "oppo", max_units=5)
    assert selection_usage(db, ProductFacts(brand="vivo")) == []


# --- scale --------------------------------------------------------------------
def test_allowance_issues_aggregates_not_row_loads(db: DbSession) -> None:
    """A few thousand orders must not become a few thousand Python objects.

    ``allowance`` runs once per account on batches of thousands, so every
    statement it issues against ``orders`` has to be an aggregate.
    """
    account = _account(db)
    _limit(db, LimitScope.category, "Mobile", max_units=20, window_days=31)
    _limit(db, LimitScope.brand, "vivo", max_units=16, window_days=31)
    _limit(db, LimitScope.model, "T5x 5G", max_units=5)
    db.commit()

    placed = utcnow() - timedelta(days=1)
    rows = [
        {
            "account_id": account.id,
            "qty": 1,
            "status": OrderStatus.placed,
            "brand": "vivo",
            "category": "Mobile",
            "model": "T5x 5G" if i % 2 else "Y28",
            "placed_at": placed,
        }
        for i in range(3000)
    ]
    db.execute(insert(Order), rows)
    db.commit()
    assert db.scalar(select(Order.id).limit(1)) is not None

    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()).lower())

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        got = allowance(db, account.id, ProductFacts(brand="vivo", category="Mobile", model="T5x 5G"))
    finally:
        event.remove(engine, "before_cursor_execute", _capture)

    assert got == 0  # 1500 of that model against a cap of 5

    order_reads = [sql for sql in statements if "from orders" in sql]
    assert order_reads, f"no query hit orders: {statements}"
    assert all("sum(" in sql for sql in order_reads), order_reads
    # One read of unit_limits plus one aggregate per limit — nothing per row.
    assert len(order_reads) == 3, order_reads
    assert len(statements) <= 5, statements
