"""What the Phase 4 adversarial review found, pinned.

A close by hand is an acceptance (spec C-6): the rules leave the row closed
while the problem persists and reopen it only once the problem has gone away
and come back. ``updated_at`` moves only when something about an alert changed,
so the queue's ages survive the passes that run after every read-back. The
dashboard's to-be-delivered figure is reproducible in the ledger under the
same filter (SC-004).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import alerts as alerts_api
from app.api import stats as stats_api
from app.core import alerts as engine
from app.core import order_filter
from app.core.order_filter import OrderFilter
from app.core.order_units import apply_observations
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Alert, AlertSeverity, Order, OrderStatus, utcnow


class _Seen:
    def __init__(self, **fields):
        for name in ("order_id", "unit_id", "item_id", "platform_status", "delivery_date", "seller", "tracking_id", "invoice_url", "delivery_otp"):
            setattr(self, name, None)
        self.__dict__.update(fields)


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(alerts_api.router)
    application.include_router(stats_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def _naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def _account(db, email="acc@test.com") -> Account:
    account = Account(email=email, status=AccountStatus.new)
    db.add(account)
    db.commit()
    return account


def _failed_order(db) -> Order:
    order = Order(account_id=_account(db).id, status=OrderStatus.failed, model="T5x")
    db.add(order)
    db.commit()
    return order


def _alert(db, key: str) -> Alert:
    alert = db.scalar(select(Alert).where(Alert.key == key))
    assert alert is not None, key
    db.refresh(alert)
    return alert


# --- a close by hand is an acceptance ----------------------------------------------
def test_a_resolve_by_hand_survives_the_next_passes(db):
    order = _failed_order(db)
    engine.evaluate(db)
    key = f"order_failed:order:{order.id}"
    assert engine.resolve_alert(db, key, note="Accepted: refunded by hand") is True

    engine.evaluate(db)  # the order is still failed — the problem persists
    engine.evaluate_in_own_session()  # the start-up / post-sync path

    alert = _alert(db, key)
    assert alert.resolved is True
    assert alert.resolved_by == engine.RESOLVED_BY_USER
    assert alert.resolution_note == "Accepted: refunded by hand"
    assert alert.last_seen_at is not None
    assert engine.open_count(db) == 0


def test_an_accepted_alert_reopens_only_after_the_problem_went_away_and_came_back(db):
    order = _failed_order(db)
    engine.evaluate(db)
    key = f"order_failed:order:{order.id}"
    engine.resolve_alert(db, key, note="Looked at it")

    # The problem clears by itself: the row stays closed, the acceptance is
    # spent, and the row now reads as a rule's close — which carries no note.
    order.status = OrderStatus.placed
    db.commit()
    engine.evaluate(db)
    alert = _alert(db, key)
    assert alert.resolved is True
    assert alert.resolved_by == engine.RESOLVED_BY_RULE
    assert alert.resolution_note is None

    # ...and comes back: the same row reopens, and the old note goes with it.
    order.status = OrderStatus.failed
    db.commit()
    engine.evaluate(db)
    alert = _alert(db, key)
    assert alert.resolved is False
    assert alert.resolved_by is None
    assert alert.resolution_note is None
    assert engine.open_count(db) == 1


def test_a_note_never_describes_a_later_close(db):
    order = _failed_order(db)
    engine.evaluate(db)
    key = f"order_failed:order:{order.id}"
    engine.resolve_alert(db, key, note="first note")

    assert engine.reopen_alert(db, key) is True
    alert = _alert(db, key)
    assert (alert.resolved, alert.resolved_by, alert.resolution_note) == (False, None, None)

    order.status = OrderStatus.placed
    db.commit()
    engine.evaluate(db)  # a rule closes it — with no note of its own
    alert = _alert(db, key)
    assert (alert.resolved, alert.resolved_by, alert.resolution_note) == (True, engine.RESOLVED_BY_RULE, None)


def test_raise_alert_still_reopens_an_accepted_ad_hoc_alert(db):
    engine.raise_alert(db, key="adhoc:1", kind="adhoc", message="seen once")
    engine.resolve_alert(db, "adhoc:1", note="ok")

    alert = engine.raise_alert(db, key="adhoc:1", kind="adhoc", message="seen again")

    assert alert.resolved is False
    assert alert.resolution_note is None
    assert alert.message == "seen again"


def test_the_api_reports_who_closed_it(api, db):
    order = _failed_order(db)
    engine.evaluate(db)
    alert = _alert(db, f"order_failed:order:{order.id}")

    out = api.post(f"/alerts/{alert.id}/resolve", json={"note": "fine"}).json()
    assert (out["resolved_by"], out["resolution_note"]) == ("user", "fine")
    assert out["last_seen_at"] is not None

    # "Evaluate now" counts what is open — an accepted problem is not.
    counts = api.post("/alerts/evaluate").json()
    assert counts["counts"]["order_failed"] == 0
    assert counts["open"] == 0

    out = api.post(f"/alerts/{alert.id}/reopen").json()
    assert (out["resolved_by"], out["resolution_note"]) == (None, None)


def test_an_accepted_alert_is_not_rewritten_underneath_the_operator(db):
    order = _failed_order(db)
    engine.evaluate(db)
    key = f"order_failed:order:{order.id}"
    engine.resolve_alert(db, key, note="Accepted")
    before = _alert(db, key)
    message, updated_at = before.message, before.updated_at

    order.model = "T5x 5G"  # the finding's message would change
    db.commit()
    engine.evaluate(db)

    after = _alert(db, key)
    assert after.message == message  # left exactly as the operator closed it
    assert after.updated_at == updated_at
    assert after.last_seen_at is not None and _naive(after.last_seen_at) >= _naive(updated_at)


def test_alerts_closed_before_resolved_by_existed_are_owned_by_a_person(db):
    """The upgrade must not reopen every hand close at its first start-up."""
    from app.db import base

    now = _naive(utcnow())
    legacy = Alert(key="order_failed:order:9", kind="order_failed", severity=AlertSeverity.warning,
                   message="old", resolved=True, resolved_at=now, resolution_note="refunded by hand",
                   resolved_by=None, created_at=now, updated_at=now)
    by_rule = Alert(key="k:2", kind="k", severity=AlertSeverity.info, message="m", resolved=True,
                    resolved_by=engine.RESOLVED_BY_RULE, created_at=now, updated_at=now)
    open_row = Alert(key="k:3", kind="k", severity=AlertSeverity.info, message="m", resolved=False,
                     created_at=now, updated_at=now)
    db.add_all([legacy, by_rule, open_row])
    db.commit()

    assert base.sync_alert_closes() == 1
    assert base.sync_alert_closes() == 0  # nothing left to own
    db.expire_all()
    assert db.get(Alert, legacy.id).resolved_by == engine.RESOLVED_BY_USER
    assert db.get(Alert, by_rule.id).resolved_by == engine.RESOLVED_BY_RULE
    assert db.get(Alert, open_row.id).resolved_by is None


# --- updated_at moves only on a change --------------------------------------------
def test_an_unchanged_pass_moves_last_seen_but_not_updated_at(db):
    order = _failed_order(db)
    engine.evaluate(db)
    key = f"order_failed:order:{order.id}"
    alert = _alert(db, key)
    then = _naive(utcnow()) - timedelta(days=21)
    alert.created_at = then
    alert.updated_at = then
    alert.last_seen_at = then
    db.commit()

    engine.evaluate(db)
    alert = _alert(db, key)
    assert _naive(alert.updated_at) == then  # nothing about the alert changed
    assert _naive(alert.created_at) == then
    assert _naive(alert.last_seen_at) > then

    order.model = "T5x 5G"  # the message changes with it
    db.commit()
    engine.evaluate(db)
    alert = _alert(db, key)
    assert _naive(alert.updated_at) > then


def test_an_age_that_grows_by_itself_is_not_a_change(db):
    """An overdue delivery is a day older every day; that is not news."""
    account = _account(db)
    order = Order(account_id=account.id, status=OrderStatus.placed, model="T5x", order_id="OD9",
                  placed_at=_naive(utcnow()) - timedelta(days=12), platform_status="Shipped")
    db.add(order)
    db.commit()
    engine.evaluate(db)
    key = f"delivery_overdue:order:{order.id}"
    alert = _alert(db, key)
    assert "ordered 12 days ago" in alert.message
    then = _naive(utcnow()) - timedelta(days=5)
    alert.updated_at = then
    db.commit()

    order.placed_at = order.placed_at - timedelta(days=1)  # a day passes
    db.commit()
    engine.evaluate(db)

    alert = _alert(db, key)
    assert "ordered 13 days ago" in alert.message  # the reading is current...
    assert alert.details["days"] == 13
    assert _naive(alert.updated_at) == then  # ...but nothing about the problem changed

    order.platform_status = "Out for delivery"  # this, on the other hand, is news
    db.commit()
    engine.evaluate(db)
    assert _naive(_alert(db, key).updated_at) > then


def test_the_queue_can_be_ordered_by_first_sighting(api, db):
    now = _naive(utcnow())
    old = Alert(key="k:1", kind="k", severity=AlertSeverity.info, message="old", resolved=False,
                created_at=now - timedelta(days=3), updated_at=now)
    new = Alert(key="k:2", kind="k", severity=AlertSeverity.info, message="new", resolved=False,
                created_at=now, updated_at=now - timedelta(days=3))
    db.add_all([old, new])
    db.commit()

    assert [a["message"] for a in api.get("/alerts", params={"order": "created"}).json()] == ["new", "old"]
    assert [a["message"] for a in api.get("/alerts").json()] == ["old", "new"]
    assert api.get("/alerts", params={"order": "sideways"}).status_code == 422


# --- two passes colliding -----------------------------------------------------------
def test_evaluate_now_recovers_from_a_real_collision(api, db, monkeypatch):
    """Another writer lands the same new key between the pass's read and its
    commit: the first attempt fails on the UNIQUE index, the request rolls back
    and the second pass updates the row that is now there."""
    from app.db.base import SessionLocal

    order = _failed_order(db)
    key = f"order_failed:order:{order.id}"
    real = engine._existing_for
    raced: list[str] = []

    def racing(session, kind, findings):
        result = real(session, kind, findings)
        if kind == "order_failed" and not raced:
            raced.append(kind)
            with SessionLocal() as other:
                now = utcnow()
                other.add(Alert(key=key, kind=kind, severity=AlertSeverity.warning, message="raced",
                                resolved=False, created_at=now, updated_at=now))
                other.commit()
        return result

    monkeypatch.setattr(engine, "_existing_for", racing)

    response = api.post("/alerts/evaluate")

    assert response.status_code == 200, response.text
    assert response.json()["counts"]["order_failed"] == 1
    assert db.query(Alert).filter(Alert.key == key).count() == 1
    assert raced == ["order_failed"]


# --- SC-004: the card's figure is the ledger's figure under the same filter ----------
def test_to_be_delivered_equals_the_ledger_under_the_same_filter(api, db):
    account = _account(db, "tbd@test.com")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    placed = Order(account_id=account.id, status=OrderStatus.placed, brand="vivo", model="T5x", qty=2,
                   placed_at=now - timedelta(days=3), order_id="OD1")
    failed = Order(account_id=account.id, status=OrderStatus.failed, brand="vivo", model="T5x", qty=1)
    unsure = Order(account_id=account.id, status=OrderStatus.unconfirmed, brand="vivo", model="T5x", qty=1)
    db.add_all([placed, failed, unsure])
    db.commit()
    apply_observations(db, placed, [
        _Seen(order_id="OD1", unit_id="U1", platform_status="Shipped"),
        _Seen(order_id="OD1", unit_id="U2", platform_status="Out for delivery"),
    ])
    db.commit()

    total = api.get("/stats/to-be-delivered").json()["total_units"]
    same_filter = OrderFilter(statuses=(OrderStatus.placed,), delivery_states=("in_transit", "unknown"))
    assert total == order_filter.count(db, same_filter) == 2
    # Without the status clause the ledger also counts the units the flush
    # guard gives orders that never reached the platform — which is why the
    # card pins status=placed, and why a comparison must too.
    assert order_filter.count(db, OrderFilter(delivery_states=("in_transit", "unknown"))) == 4
