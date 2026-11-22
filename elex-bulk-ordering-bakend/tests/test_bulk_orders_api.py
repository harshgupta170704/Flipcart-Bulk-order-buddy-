"""GET /jobs/bulk-orders: the Orders page's one row per order run."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import jobs as jobs_api
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Gst,
    Job,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
)


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(jobs_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_one_row_per_order_run_with_its_tallies(api, db):
    account = Account(email="a@test.com", phone="9000000001", status=AccountStatus.new)
    address = Address(label="Warehouse", name="WH", line1="1 Road", pincode="110052")
    gst = Gst(label="Main GST", gstin="07AAAAA0000A1Z5", business_name="Acme LLP")
    db.add_all([account, address, gst])
    db.commit()

    ai_run = Job(
        type=JobType.order,
        status=JobStatus.completed,
        total_items=3,
        done_items=3,
        payload={
            "product_link": "https://www.flipkart.com/p/itm1",
            "mode": "ai",
            "payment_mode": "cod",
            "qty": 2,
            "total_quantity": 4,
            "address_id": address.id,
            "gst_id": gst.id,
            "started_by": "ops@test.com",
            "target": 26900,
        },
    )
    login_run = Job(type=JobType.login, status=JobStatus.completed, payload={})
    plain_run = Job(type=JobType.order, status=JobStatus.running, payload={"product_link": "x"})
    db.add_all([ai_run, login_run, plain_run])
    db.commit()

    def order(status, qty=2, amount=844.0, model="vivo T5x 5G"):
        return Order(
            account_id=account.id, job_id=ai_run.id, status=status, qty=qty, amount=amount, model=model
        )

    db.add_all(
        [
            order(OrderStatus.placed),
            order(OrderStatus.placed, qty=None, amount=None),
            order(OrderStatus.failed),
            order(OrderStatus.unconfirmed),
        ]
    )
    db.commit()

    resp = api.get("/jobs/bulk-orders")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [row["id"] for row in rows] == [plain_run.id, ai_run.id]  # order runs only, newest first

    row = rows[1]
    assert row["mode"] == "ai"
    assert row["platform"] == "flipkart"
    assert row["product_name"] == "vivo T5x 5G"
    assert row["orders_placed"] == 2
    assert row["units_placed"] == 3  # a placed order with no qty counts as one unit
    assert row["orders_unconfirmed"] == 1
    assert row["amount_placed"] == 844.0
    assert row["total_quantity"] == 4
    assert row["qty_per_order"] == 2
    assert row["payment_mode"] == "cod"
    assert row["started_by"] == "ops@test.com"
    assert row["address_label"] == "Warehouse"
    assert row["gst_label"] == "Main GST"

    plain = rows[0]
    assert plain["mode"] == "real"
    assert plain["orders_placed"] == 0
    assert plain["product_name"] is None
    assert plain["address_label"] is None


def test_empty_and_needs_sign_in(api):
    assert api.get("/jobs/bulk-orders").json() == []
    application = FastAPI()
    application.include_router(jobs_api.router)
    assert TestClient(application).get("/jobs/bulk-orders").status_code == 401


def test_filters_parent_card_and_creators(api, db):
    from datetime import datetime, timedelta

    account = Account(email="f@test.com", phone="9000000009", status=AccountStatus.new)
    db.add(account)
    db.commit()
    old = Job(type=JobType.order, status=JobStatus.completed, payload={"mode": "ai", "started_by": "a@x"},
              started_at=datetime.utcnow() - timedelta(days=10))
    new = Job(type=JobType.order, status=JobStatus.failed, payload={"started_by": "b@x", "flipkart_minutes": True})
    db.add_all([old, new])
    db.commit()
    db.add(Order(account_id=account.id, job_id=new.id, status=OrderStatus.placed, card_number="5412345678824803"))
    db.add(Order(account_id=account.id, job_id=new.id, status=OrderStatus.failed))
    db.commit()

    def ids(**params):
        return [row["id"] for row in api.get("/jobs/bulk-orders", params=params).json()]

    assert ids(mode="ai") == [old.id]
    assert ids(created_by="b@x") == [new.id]
    assert ids(status="failed") == [new.id]
    assert ids(since=(datetime.utcnow() - timedelta(days=1)).isoformat()) == [new.id]
    assert ids(platform="amazon") == []

    row = api.get("/jobs/bulk-orders", params={"created_by": "b@x"}).json()[0]
    assert row["parent_card"] == "54XX…824803"
    assert row["orders_failed"] == 1 and row["orders_placed"] == 1
    assert row["flipkart_minutes"] is True
    assert api.get("/jobs/bulk-orders/creators").json() == ["a@x", "b@x"]
