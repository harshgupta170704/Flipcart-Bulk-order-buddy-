"""The dashboard's "View in ledger" link lands on the card's own count (SC-004).

The "To be delivered" card links to Order Units History under the filter it
counts by (LEDGER_LINK in frontend/src/components/ToBeDelivered.tsx), so the
two totals can be compared on screen. The link is read out of the frontend
source here and sent through the ledger's own query parsing: were the link or
what the card counts to move, the ledger would show another number than the
card, and this file would say so.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.api import stats as stats_api
from app.core.order_units import apply_observations
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus

from tests import layout

#: The link is read out of the web app's source.
pytestmark = layout.needs_web
CARD_FILE = layout.web("src", "components", "ToBeDelivered.tsx")


class _Seen:
    """One unit as a read-back reports it; only what a test sets is shown."""

    def __init__(self, **fields):
        for name in ("order_id", "unit_id", "item_id", "platform_status", "delivery_date", "seller", "tracking_id", "invoice_url", "delivery_otp"):
            setattr(self, name, None)
        self.__dict__.update(fields)


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    application.include_router(stats_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def ledger_link() -> tuple[str, list[tuple[str, str]]]:
    """The card's link as the page holds it: its path and its query, in order."""
    source = CARD_FILE.read_text(encoding="utf-8")
    found = re.search(r'export const LEDGER_LINK = "([^"]+)";', source)
    assert found, "LEDGER_LINK is no longer declared in ToBeDelivered.tsx"
    link = urlsplit(found.group(1))
    return link.path, parse_qsl(link.query)


def test_the_cards_link_shows_the_cards_count_in_the_ledger(api, db):
    path, params = ledger_link()
    assert path == "/order-units"

    account = Account(email="link@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    read_back = Order(account_id=account.id, status=OrderStatus.placed, brand="vivo", model="T5x", qty=3,
                      placed_at=now - timedelta(days=3), order_id="OD1")
    never_read = Order(account_id=account.id, status=OrderStatus.placed, brand="vivo", model="T5x", qty=1,
                       placed_at=now - timedelta(days=20), order_id="OD2")
    # Never reached Flipkart: its placeholder unit is in neither count.
    failed = Order(account_id=account.id, status=OrderStatus.failed, brand="vivo", model="T5x", qty=1)
    db.add_all([read_back, never_read, failed])
    db.commit()
    apply_observations(db, read_back, [
        _Seen(order_id="OD1", unit_id="U1", platform_status="Shipped"),
        _Seen(order_id="OD1", unit_id="U2", platform_status="Delivered"),
        _Seen(order_id="OD1", unit_id="U3", platform_status="Cancelled"),
    ])
    db.commit()

    card = api.get("/stats/to-be-delivered").json()["total_units"]
    ledger = api.get("/orders", params=params)

    assert ledger.status_code == 200, ledger.text
    # U1 on its way, and OD2's one unit never read back.
    assert ledger.json()["total"] == card == 2
