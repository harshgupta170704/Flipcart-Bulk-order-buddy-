"""The request log's rules below the API (spec FR-020, decision C-4): the
one-open-per-kind rule is the database's too — a unique index on the open rows,
built on an existing database at start-up — and a cancel is refused on every
settled unit, with one reading of "settled" for the whole portal."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.core import unit_requests as core
from app.core.order_status import SETTLED
from app.db.base import engine, sync_missing_indexes
from app.db.models import Account, AccountStatus, Order, OrderStatus, UnitRequest

INDEX = "uq_unit_requests_open_kind"


@pytest.fixture
def unit_pk(db) -> int:
    account = Account(email="rule@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    order = Order(account_id=account.id, status=OrderStatus.placed, order_id="OD100000000000000009", platform_status="Shipped")
    db.add(order)
    db.commit()
    db.refresh(order)
    return order.units[0].id


def test_a_second_open_request_of_one_kind_on_one_unit_is_refused_by_the_database(db, unit_pk):
    db.add(UnitRequest(unit_pk=unit_pk, kind="cancel", status="open", requested_by="a@test.com"))
    db.commit()

    db.add(UnitRequest(unit_pk=unit_pk, kind="cancel", status="open", requested_by="b@test.com"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    # A closed one beside the open one, and the other kind, are no clash.
    db.add_all(
        [
            UnitRequest(unit_pk=unit_pk, kind="cancel", status="done", requested_by="b@test.com"),
            UnitRequest(unit_pk=unit_pk, kind="reschedule", status="open", requested_by="b@test.com"),
        ]
    )
    db.commit()
    assert db.query(UnitRequest).filter(UnitRequest.unit_pk == unit_pk).count() == 3


def test_the_open_kind_index_is_built_on_an_existing_database_at_start_up(db):
    names = lambda: {ix["name"] for ix in inspect(engine).get_indexes("unit_requests")}  # noqa: E731
    assert INDEX in names()  # create_all made it on a fresh database
    with engine.begin() as conn:
        conn.execute(text(f"DROP INDEX {INDEX}"))
    assert INDEX not in names()

    created = sync_missing_indexes()

    assert f"unit_requests.{INDEX}" in created
    catalogue = (
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"
        if engine.dialect.name == "sqlite"
        else "SELECT indexdef FROM pg_indexes WHERE indexname = :name"
    )
    with engine.connect() as conn:
        ddl = conn.execute(text(catalogue), {"name": INDEX}).scalar()
    assert "UNIQUE" in ddl and "WHERE" in ddl and "status" in ddl and "'open'" in ddl  # partial: the open rows only
    assert sync_missing_indexes() == []  # idempotent


def test_a_cancel_is_refused_in_every_settled_state():
    assert core.REFUSE_CANCEL_STATES == SETTLED == {"delivered", "cancelled", "returned", "failed"}


@pytest.mark.parametrize("kind", ["cancel", "reschedule"])
def test_a_request_on_an_attempt_flipkart_never_took_is_skipped_with_its_reason(db, kind):
    """A failed attempt, or one stopped over its target, with no order id never
    reached Flipkart: a request on it would send a person to cancel an order
    that does not exist (spec FR-020; review of 27 Oct 2026). An unconfirmed
    order may exist, and one Flipkart took and later cancelled has its id."""
    account = Account(email="never@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    orders = {
        "failed": Order(account_id=account.id, status=OrderStatus.failed),
        "over": Order(account_id=account.id, status=OrderStatus.over_target),
        "unconfirmed": Order(account_id=account.id, status=OrderStatus.unconfirmed),
        "placed": Order(account_id=account.id, status=OrderStatus.placed, order_id="OD100000000000000010"),
    }
    db.add_all(orders.values())
    db.commit()
    pks = {name: order.units[0].id for name, order in orders.items()}

    created, skipped = core.create_requests(
        db, unit_pks=list(pks.values()), kind=kind, reason="test", requested_for=None, requested_by="a@test.com"
    )

    assert sorted(row.unit_pk for row in created) == sorted([pks["unconfirmed"], pks["placed"]])
    assert {s.unit_pk: s.why for s in skipped} == {
        pks["failed"]: f"never reached Flipkart: there is no order to {kind}",
        pks["over"]: f"never reached Flipkart: there is no order to {kind}",
    }
