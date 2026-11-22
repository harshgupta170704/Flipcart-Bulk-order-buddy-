"""Flipkart cookie sets marked invalid are deleted at start-up (spec NFR-003;
review of 27 Oct 2026). No browser is ever given one and no page shows one,
but a set may still sign in until Flipkart expires it. Sets superseded before
27 Oct 2026 were kept as invalid; save_session now deletes them as it goes."""

from __future__ import annotations

from app.core import session_store
from app.db.models import Account, AccountStatus, Session as SessionModel


def _account(db, email: str) -> Account:
    account = Account(email=email, status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    return account


def test_only_the_invalid_sets_are_deleted_and_twice_is_harmless(db):
    one, two = _account(db, "one@test.com"), _account(db, "two@test.com")
    db.add_all(
        [
            SessionModel(account_id=one.id, cookies=[{"name": "old"}], valid=False),
            SessionModel(account_id=one.id, cookies=[{"name": "older"}], valid=False),
            SessionModel(account_id=one.id, cookies=[{"name": "live"}], valid=True),
            SessionModel(account_id=two.id, cookies=[{"name": "live"}], valid=True),
        ]
    )
    db.commit()

    assert session_store.drop_invalid(db) == 2
    db.expire_all()
    left = db.query(SessionModel).all()
    assert sorted((row.account_id, row.valid) for row in left) == [(one.id, True), (two.id, True)]
    assert session_store.load_cookies(db, one) == [{"name": "live"}]
    assert session_store.drop_invalid(db) == 0


def test_the_start_up_pass_clears_them_in_its_own_session(db):
    account = _account(db, "start@test.com")
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "old"}], valid=False))
    db.commit()

    assert session_store.drop_invalid_in_own_session() == 1
    db.expire_all()
    assert db.query(SessionModel).count() == 0


def test_the_app_runs_the_pass_at_every_start():
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    assert "drop_invalid_in_own_session()" in source
