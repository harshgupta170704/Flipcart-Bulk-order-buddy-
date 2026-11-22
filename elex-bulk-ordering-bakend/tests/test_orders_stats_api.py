"""Tests for the orders list/export and the dashboard counters.

The seeded fixture is the interesting case from PLAN.md §6a: one order placed
under its target, one recorded as ``failed_over_target`` with the amount that
breached it. Both are kept — the over-target order is a result, not an error,
and it must never have stopped the other account from ordering.

The routers are mounted on a local FastAPI app because ``app/main.py`` belongs
to the orchestrator and does not include them yet.
"""

from __future__ import annotations

import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api import orders as orders_api
from app.api import stats as stats_api
from app.automation.coupons import ALREADY_USED, CLAIMED
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    CouponRun,
    Job,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
)
from app.db.models import Session as SessionModel
from app.db.models import utcnow

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(orders_api.router)
    application.include_router(stats_api.router)
    return application


@pytest.fixture
def api(api_app: FastAPI) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anon(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


@pytest.fixture
def seeded(db) -> dict[str, Account]:
    """Two accounts: one placed order, one over-target order."""
    first = Account(email="first@test.com", status=AccountStatus.session_valid)
    second = Account(email="second@test.com", status=AccountStatus.new)
    db.add_all([first, second])
    db.commit()
    db.refresh(first)
    db.refresh(second)

    # Two session rows for the same account — one stale — so "sessions_valid"
    # has to count distinct accounts rather than rows.
    db.add(SessionModel(account_id=first.id, cookies=[{"name": "SN"}], valid=False))
    db.add(SessionModel(account_id=first.id, cookies=[{"name": "SN"}], valid=True))

    db.add(CouponRun(account_id=first.id, coupon_code="SAVE10", result=CLAIMED))
    db.add(CouponRun(account_id=second.id, coupon_code="SAVE10", result=ALREADY_USED))

    db.add(
        Order(
            account_id=first.id,
            product_link="https://www.flipkart.com/p/1",
            qty=1,
            model="Phone X",
            color="Blue",
            pin="560001",
            amount=999.0,
            target=1000.0,
            order_id="OD111",
            gst_name="Acme Ltd",
            status=OrderStatus.placed,
            placed_at=utcnow(),
        )
    )
    db.add(
        Order(
            account_id=second.id,
            product_link="https://www.flipkart.com/p/1",
            qty=1,
            amount=1000.01,  # one paisa over: not placed (PLAN.md §6a)
            target=1000.0,
            status=OrderStatus.over_target,
        )
    )
    db.add(Job(type=JobType.order, status=JobStatus.running, total_items=2))
    db.add(Job(type=JobType.login, status=JobStatus.completed, total_items=1))
    db.commit()
    return {"first": first, "second": second}


# --- auth ---------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/orders", "/orders/export", "/stats"])
def test_routes_need_a_token(anon: TestClient, path: str) -> None:
    assert anon.get(path).status_code in (401, 403)


# --- orders -------------------------------------------------------------------
def test_list_orders_newest_first_with_account_email(api, seeded) -> None:
    response = api.get("/orders")
    assert response.status_code == 200

    page = response.json()
    assert page["total"] == 2  # the FULL match count, not the page length
    assert (page["page"], page["size"], page["pages"]) == (1, 100, 1)
    rows = page["items"]
    assert len(rows) == 2
    assert rows[0]["id"] > rows[1]["id"]  # newest first
    assert rows[0]["account_email"] == seeded["second"].email
    assert rows[1]["account_email"] == seeded["first"].email

    placed = rows[1]
    assert placed["status"] == OrderStatus.placed.value
    assert placed["order_id"] == "OD111"
    assert placed["model"] == "Phone X"
    assert placed["amount"] == 999.0


def test_every_synced_column_reaches_the_api(api, seeded, db) -> None:
    """Eleven columns — every synced field among them — were missing from the
    response schema, so the table's "sync columns" could never show a value."""
    row = db.query(Order).filter(Order.order_id == "OD111").one()
    # What a read-back writes lands on the order's unit — the ledger's row.
    unit = row.units[0]
    unit.unit_id = "439123456789012345678"
    unit.platform_status = "Out for delivery"
    unit.tracking_id = "TRK1"
    unit.seller = "Example Seller"
    unit.synced_at = utcnow()
    db.commit()

    out = next(r for r in api.get("/orders").json()["items"] if r["order_id"] == "OD111")
    assert out["unit_id"] == "439123456789012345678"  # every digit, as text
    assert out["platform_status"] == "Out for delivery"
    assert out["delivery_state"] == "in_transit"  # the server's reading of it
    assert out["tracking_id"] == "TRK1"
    assert out["seller"] == "Example Seller"
    assert out["synced_at"] is not None
    for key in ("brand", "category", "job_id", "item_id", "delivery_date", "delivery_otp", "invoice_url"):
        assert key in out, key


def test_filter_by_status_keeps_the_over_target_row_visible(api, seeded) -> None:
    page = api.get("/orders", params={"status": OrderStatus.over_target.value}).json()
    assert page["total"] == 1
    over = page["items"][0]
    assert over["account_email"] == seeded["second"].email
    assert over["amount"] == 1000.01
    assert over["target"] == 1000.0
    # Never placed: no order id, no placed_at.
    assert over["order_id"] is None
    assert over["placed_at"] is None

    assert api.get("/orders", params={"status": OrderStatus.placed.value}).json()["total"] == 1
    assert api.get("/orders", params={"status": OrderStatus.failed.value}).json()["items"] == []


def test_filter_by_account_and_paging(api, seeded) -> None:
    page = api.get("/orders", params={"account_id": seeded["first"].id}).json()
    assert [r["account_email"] for r in page["items"]] == [seeded["first"].email]

    first = api.get("/orders", params={"size": 1}).json()
    assert (len(first["items"]), first["total"], first["pages"]) == (1, 2, 2)
    second = api.get("/orders", params={"size": 1, "page": 2}).json()
    assert len(second["items"]) == 1
    assert second["items"][0]["id"] != first["items"][0]["id"]
    assert api.get("/orders", params={"size": 0}).status_code == 422
    assert api.get("/orders", params={"page": 0}).status_code == 422


def test_unknown_status_is_rejected(api, seeded) -> None:
    assert api.get("/orders", params={"status": "nope"}).status_code == 422


def test_export_returns_an_xlsx_download(api, seeded) -> None:
    response = api.get("/orders/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == _XLSX_MIME
    assert "orders.xlsx" in response.headers["content-disposition"]

    sheet = load_workbook(io.BytesIO(response.content)).active
    text = {str(cell.value) for row in sheet.iter_rows() for cell in row}
    assert seeded["first"].email in text
    assert OrderStatus.over_target.value in text


# --- stats --------------------------------------------------------------------
def test_stats_counts(api, seeded) -> None:
    assert api.get("/stats").json() == {
        "accounts": 2,
        "sessions_valid": 1,
        "coupons_applied": 1,  # the already-used run is not an applied coupon
        "orders_placed": 1,
        "orders_over_target": 1,
        "orders_failed": 0,
        "orders_cancelled": 0,
        "orders_cancelled_by_user": 0,
        "orders_unconfirmed": 0,
        "jobs_running": 1,
    }


def test_stats_on_an_empty_database(api) -> None:
    assert api.get("/stats").json() == {
        "accounts": 0,
        "sessions_valid": 0,
        "coupons_applied": 0,
        "orders_placed": 0,
        "orders_over_target": 0,
        "orders_failed": 0,
        "orders_cancelled": 0,
        "orders_cancelled_by_user": 0,
        "orders_unconfirmed": 0,
        "jobs_running": 0,
    }


def _unconfirmed(db, account_id: int) -> Order:
    row = Order(
        account_id=account_id,
        product_link="https://www.flipkart.com/p/1",
        qty=1,
        amount=17707.0,
        target=18000.0,
        status=OrderStatus.unconfirmed,
        placed_at=utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_an_unconfirmed_order_can_be_settled_as_placed_with_its_id(api, seeded, db) -> None:
    row = _unconfirmed(db, seeded["first"].id)
    response = api.post(
        f"/orders/{row.id}/settle", json={"status": "placed", "order_id": "OD338963864435840100"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "placed"
    assert body["order_id"] == "OD338963864435840100"
    assert body["account_email"] == seeded["first"].email
    db.expire_all()
    assert db.get(Order, row.id).placed_at is not None  # created when it was submitted


def test_an_unconfirmed_order_can_be_settled_as_failed(api, seeded, db) -> None:
    row = _unconfirmed(db, seeded["first"].id)
    response = api.post(f"/orders/{row.id}/settle", json={"status": "failed"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    db.expire_all()
    assert db.get(Order, row.id).placed_at is None  # its units go back


def test_settling_as_placed_needs_a_real_order_id(api, seeded, db) -> None:
    row = _unconfirmed(db, seeded["first"].id)
    assert api.post(f"/orders/{row.id}/settle", json={"status": "placed"}).status_code == 422
    assert (
        api.post(f"/orders/{row.id}/settle", json={"status": "placed", "order_id": "banana"}).status_code
        == 422
    )


def test_only_an_unconfirmed_order_can_be_settled(api, seeded, db) -> None:
    """Rewriting any other outcome by hand would falsify the record."""
    placed = db.query(Order).filter(Order.status == OrderStatus.placed).first()
    response = api.post(f"/orders/{placed.id}/settle", json={"status": "failed"})
    assert response.status_code == 409
    assert api.post("/orders/999999/settle", json={"status": "failed"}).status_code == 404


def test_unconfirmed_orders_are_counted_apart_from_failures(api, seeded, db) -> None:
    """Submitted but never confirmed: it may exist, so it is neither a failure
    nor a placed order, and the dashboard has to show it on its own."""
    db.add(
        Order(
            account_id=seeded["first"].id,
            product_link="https://www.flipkart.com/p/1",
            qty=1,
            amount=17707.0,
            target=18000.0,
            status=OrderStatus.unconfirmed,
            placed_at=utcnow(),
        )
    )
    db.commit()

    stats = api.get("/stats").json()
    assert stats["orders_unconfirmed"] == 1
    assert stats["orders_failed"] == 0
    assert stats["orders_placed"] == 1
