"""Step D: a unit limit can cap the units bought from one seller."""

from __future__ import annotations

from datetime import timedelta

from app.core import unit_limits
from app.core.unit_limits import ProductFacts
from app.db.models import Account, AccountStatus, LimitScope, Order, OrderStatus, UnitLimit, naive_utcnow


def _setup(db):
    account = Account(email="s@t.in", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(UnitLimit(scope=LimitScope.seller, value="KanungaKouri", max_units=3, window_days=30, enabled=True))
    for _ in range(2):
        db.add(Order(account_id=account.id, status=OrderStatus.placed, seller="KANUNGAKOURI", qty=1,
                     created_at=naive_utcnow() - timedelta(days=1), placed_at=naive_utcnow() - timedelta(days=1)))
    db.commit()
    return account


def test_a_seller_limit_applies_to_that_seller_in_any_case(db):
    _setup(db)
    assert [l.scope for l in unit_limits.applicable(db, ProductFacts(seller="kanungakouri"))] == [LimitScope.seller]
    assert unit_limits.applicable(db, ProductFacts(seller="Someone Else")) == []
    assert unit_limits.applicable(db, ProductFacts()) == []


def test_units_from_that_seller_count_against_it(db):
    account = _setup(db)
    assert unit_limits.allowance(db, account.id, ProductFacts(seller="KanungaKouri")) == 1


def test_the_check_endpoint_takes_the_seller(db, client, auth_headers):
    account = _setup(db)
    body = client.post("/unit-limits/check", json={"seller": "KanungaKouri", "account_ids": [account.id]}, headers=auth_headers)
    assert body.status_code == 200, body.text
    (usage,) = body.json()
    assert usage["scope"] == "seller" and usage["used"] == 2 and usage["remaining"] == 1


def test_a_seller_limit_can_be_created(db, client, auth_headers):
    response = client.post("/unit-limits", json={"scope": "seller", "value": "TBL Online", "max_units": 5, "window_days": 31}, headers=auth_headers)
    assert response.status_code in (200, 201), response.text
    assert response.json()["scope"] == "seller"
