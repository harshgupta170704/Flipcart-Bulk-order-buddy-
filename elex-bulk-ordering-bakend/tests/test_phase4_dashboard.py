"""The dashboard to spec: what is still to be delivered, by product, and an
alert closed by hand keeps the operator's reason."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import alerts as alerts_api
from app.api import stats as stats_api
from app.core import alerts as engine
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
    application.include_router(stats_api.router)
    application.include_router(alerts_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_to_be_delivered_counts_units_not_yet_delivered_by_product(api, db):
    account = Account(email="tbd@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    phone = Order(account_id=account.id, status=OrderStatus.placed, brand="vivo", model="T5 Lite", qty=3, placed_at=_now() - timedelta(days=20), order_id="OD1")
    bag = Order(account_id=account.id, status=OrderStatus.placed, brand="HP", model="Backpack", qty=1, placed_at=_now() - timedelta(days=2), order_id="OD2")
    never_placed = Order(account_id=account.id, status=OrderStatus.failed, brand="vivo", model="T5 Lite", qty=1)
    db.add_all([phone, bag, never_placed])
    db.commit()
    # Two of the phone's three units are on their way; the third arrived.
    apply_observations(db, phone, [
        _Seen(order_id="OD1", unit_id="U1", platform_status="Delivered"),
        _Seen(order_id="OD1", unit_id="U2", platform_status="Shipped"),
        _Seen(order_id="OD1", unit_id="U3", platform_status="Out for delivery"),
    ])
    db.commit()

    out = api.get("/stats/to-be-delivered").json()
    assert (out["total_units"], out["total_orders"]) == (3, 2)
    assert [(m["brand"], m["model"], m["units"], m["orders"]) for m in out["models"]] == [
        ("vivo", "T5 Lite", 2, 1),  # the delivered unit is not waiting
        ("HP", "Backpack", 1, 1),  # never read back: still honestly on its way
    ]

    older = api.get("/stats/to-be-delivered", params={"older_than_days": 15}).json()
    assert [(m["model"], m["units"]) for m in older["models"]] == [("T5 Lite", 2)]
    assert older["older_than_days"] == 15
    assert api.get("/stats/to-be-delivered", params={"older_than_days": 0}).status_code == 422


def test_resolving_by_hand_keeps_the_reason_and_a_rule_closing_keeps_none(api, db):
    now = utcnow()
    row = Alert(key="delivery_overdue:order:1", kind="delivery_overdue", severity=AlertSeverity.warning,
                message="Order 1 is late", resolved=False, created_at=now, updated_at=now)
    other = Alert(key="order_failed:order:2", kind="order_failed", severity=AlertSeverity.warning,
                  message="Order 2 failed", resolved=False, created_at=now, updated_at=now)
    db.add_all([row, other])
    db.commit()

    out = api.post(f"/alerts/{row.id}/resolve", json={"note": "  Checked the account — delivered on the 20th.  "}).json()
    assert out["resolved"] is True
    assert out["resolution_note"] == "Checked the account — delivered on the 20th."
    # No body at all is still a resolve.
    plain = api.post(f"/alerts/{other.id}/resolve").json()
    assert plain["resolved"] is True and plain["resolution_note"] is None

    listed = {a["id"]: a for a in api.get("/alerts", params={"resolved": "true"}).json()}
    assert listed[row.id]["resolution_note"].startswith("Checked the account")
    # A second resolve of a closed row changes nothing, note included.
    again = api.post(f"/alerts/{row.id}/resolve", json={"note": "something else"}).json()
    assert again["resolution_note"] == "Checked the account — delivered on the 20th."
    assert api.post(f"/alerts/{row.id}/resolve", json={"nope": 1}).status_code == 422


def test_the_engine_close_leaves_no_note(db):
    now = utcnow()
    row = Alert(key="k:1", kind="k", severity=AlertSeverity.info, message="m", resolved=False, created_at=now, updated_at=now)
    db.add(row)
    db.commit()
    engine._close(row, now)
    db.commit()
    assert row.resolved and row.resolution_note is None
