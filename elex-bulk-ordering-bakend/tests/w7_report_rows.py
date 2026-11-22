"""Report rows, an upload and the two fixtures the order report's tests
(tests/test_w7_report_*.py) share: a signed-in API over the orders routes
alone, and one buyer account to file orders under."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderUnit
from app.io import order_report

ORDER = "OD338967210239333101"
UNITS = ("439123456789012345601", "439123456789012345602", "439123456789012345603")
HEADER = ",".join(order_report.TEMPLATE_HEADERS)


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("ops@test.com")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def buyer(db) -> Account:
    account = Account(email="buyer1@example.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    return account


def line(**over) -> str:
    """One report line in the template's columns, quoted as a spreadsheet
    writes it; give only what a test is about."""
    values = {name: "" for name in order_report.TEMPLATE_HEADERS}
    values.update(
        platform="flipkart",
        order_id=ORDER,
        unit_id=UNITS[0],
        order_date="24-10-2026",
        order_time="10:10:15",
        platform_email="buyer1@example.com",
        status="Shipped",
        product="vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)",
        quantity="1",
        total_order_amount="18207.00",
    )
    values.update({key: str(value) for key, value in over.items()})
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow([values[name] for name in order_report.TEMPLATE_HEADERS])
    return buffer.getvalue()


def report(*lines: str) -> str:
    return "\n".join([HEADER, *lines])


def upload(api, text: str, path: str = "/orders/import-report"):
    return api.post(path, files={"file": ("report.csv", text.encode("utf-8"), "text/csv")})


def units_of(db, order_id: str = ORDER) -> list[OrderUnit]:
    db.expire_all()
    order = db.query(Order).filter(Order.order_id == order_id).one()
    return sorted(order.units, key=lambda unit: unit.position)


def order_of(db, order_id: str = ORDER) -> Order:
    db.expire_all()
    return db.query(Order).filter(Order.order_id == order_id).one()
