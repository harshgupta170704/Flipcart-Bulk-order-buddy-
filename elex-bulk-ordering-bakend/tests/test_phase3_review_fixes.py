"""What the adversarial review of the unit grain found, pinned.

Each test names the behaviour that was wrong on 24 Oct 2026 and the one that
replaced it: phantom units, an order row decided from one pass, cross-account
ids, unconfirmed reservations, undated uploads, the order-details shape.
"""

import asyncio
import contextlib
import io
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.api import order_details
from app.api import orders as orders_api
from app.api import sync as sync_api
from app.automation import sync
from app.automation.sync import ScrapedOrder
from app.core.order_units import apply_observations
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Gst, Job, JobStatus, JobType, Order, OrderStatus
from app.db.models import Session as SessionModel
from app.io import order_ids
from app.jobs import sync_tasks
from tests.fakes import FakeContext, FakePage
from tests.test_sync_scrape import DETAIL_0, _SyncPage


# --- helpers ----------------------------------------------------------------------
def _account(db, email: str) -> Account:
    row = Account(email=email, status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    db.add(SessionModel(account_id=row.id, cookies=[{"name": "SN", "value": email}], valid=True))
    db.commit()
    return row


def _order(db, account, **fields) -> Order:
    row = Order(account_id=account.id, **{"status": OrderStatus.placed, **fields})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _sync(db, monkeypatch, account: Account, scraped) -> None:
    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def fake_scrape_orders(page, *, limit=20, open_details=True, should_stop=None):
        return list(scraped)

    monkeypatch.setattr(sync_tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(sync_tasks, "scrape_orders", fake_scrape_orders)
    job = Job(type=JobType.sync, status=JobStatus.queued)
    db.add(job)
    db.commit()
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))


def _reload(db, row):
    db.expire_all()
    return db.get(type(row), row.id)


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    application.include_router(order_details.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


# --- C1: an id-less observation is a report on an existing unit -------------------
def test_an_id_less_observation_updates_the_next_unit_instead_of_adding_one(db):
    account = _account(db, "idless@test.com")
    order = _order(db, account, order_id="OD500", qty=1)
    apply_observations(db, order, [ScrapedOrder(order_id="OD500", unit_id="UNIT-KEEP-ME", platform_status="Shipped")])
    db.commit()

    apply_observations(db, order, [ScrapedOrder(order_id="OD500", unit_id=None, platform_status="Delivered")])
    db.commit()
    db.refresh(order)

    assert [(u.position, u.unit_id, u.platform_status) for u in order.units] == [(1, "UNIT-KEEP-ME", "Delivered")]


def test_a_two_unit_order_read_by_id_without_unit_links_gains_no_row(db):
    account = _account(db, "twoid@test.com")
    order = _order(db, account, order_id="OD600", qty=2)
    apply_observations(
        db,
        order,
        [ScrapedOrder(order_id="OD600", unit_id="A1", platform_status="Shipped"), ScrapedOrder(order_id="OD600", unit_id="A2", platform_status="Shipped")],
    )
    db.commit()

    apply_observations(db, order, [ScrapedOrder(order_id="OD600", platform_status="Delivered")])
    db.commit()
    db.refresh(order)

    assert len(order.units) == 2
    assert [u.platform_status for u in order.units] == ["Delivered", "Shipped"]


# --- C2: the order row is decided from all its units ------------------------------
def test_a_partial_list_after_a_full_one_does_not_fail_the_order(db, monkeypatch):
    account = _account(db, "partial@test.com")
    order = _order(db, account, order_id="OD700", qty=2)
    _sync(db, monkeypatch, account, [
        ScrapedOrder(order_id="OD700", unit_id="B1", platform_status="Shipped"),
        ScrapedOrder(order_id="OD700", unit_id="B2", platform_status="Shipped"),
    ])
    # The next pass lists only the first unit, now cancelled.
    _sync(db, monkeypatch, account, [ScrapedOrder(order_id="OD700", unit_id="B1", platform_status="Cancelled")])

    row = _reload(db, order)
    assert row.status == OrderStatus.placed
    assert (row.platform_status, row.delivery_state) == ("Shipped", "in_transit")
    assert [u.delivery_state for u in row.units] == ["cancelled", "in_transit"]


def test_a_partial_list_showing_one_delivered_unit_does_not_settle_the_order(db, monkeypatch):
    account = _account(db, "partial2@test.com")
    fifteen_days_ago = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=15)
    order = _order(db, account, order_id="OD800", qty=2, placed_at=fifteen_days_ago)
    _sync(db, monkeypatch, account, [
        ScrapedOrder(order_id="OD800", unit_id="C1", platform_status="Shipped"),
        ScrapedOrder(order_id="OD800", unit_id="C2", platform_status="Shipped"),
    ])
    _sync(db, monkeypatch, account, [ScrapedOrder(order_id="OD800", unit_id="C1", platform_status="Delivered")])

    row = _reload(db, order)
    assert row.delivery_state == "in_transit"  # C2 is still on its way


def test_every_observed_unit_cancelled_still_fails_the_order(db, monkeypatch):
    account = _account(db, "allcancel@test.com")
    order = _order(db, account, order_id="OD900", qty=2)
    _sync(db, monkeypatch, account, [
        ScrapedOrder(order_id="OD900", unit_id="D1", platform_status="Cancelled"),
        ScrapedOrder(order_id="OD900", unit_id="D2", platform_status="Cancelled"),
    ])
    assert _reload(db, order).status == OrderStatus.failed


# --- C3 / C4 / C19 / C5: the id sheet -------------------------------------------------
def test_an_id_under_another_account_is_refused_not_duplicated(api, db):
    a = _account(db, "a@x.com")
    _account(db, "b@x.com")
    _order(db, a, order_id="OD9000000000001")

    out = order_ids.import_sheet(db, order_ids.read_id_sheet(b"Email,Order ID\nb@x.com,OD9000000000001\n", "ids.csv"))

    assert out.created == 0
    assert [r.reason for r in out.rejected] == ["OD9000000000001 is already in the ledger under a@x.com"]
    assert db.query(Order).filter(Order.order_id == "OD9000000000001").count() == 1


def test_an_account_with_an_unconfirmed_reservation_has_its_row_refused_with_a_pointer(db):
    c = _account(db, "c@x.com")
    reservation = _order(db, c, status=OrderStatus.unconfirmed, order_id=None, qty=2)

    out = order_ids.import_sheet(db, order_ids.read_id_sheet(b"Email,Order ID,Qty\nc@x.com,OD1234567890,2\n", "ids.csv"))

    assert out.created == 0
    assert f"#{reservation.id}" in out.rejected[0].reason and "settle it" in out.rejected[0].reason
    assert db.query(Order).count() == 1


def test_a_fractional_quantity_is_refused():
    sheet = order_ids.read_id_sheet(b"Email,Order ID,Qty\na@x.com,OD1234567890,2.5\n", "ids.csv")
    assert sheet.rows == []
    assert "whole number" in sheet.rejected[0].reason


def test_an_order_date_in_the_sheet_dates_the_order(db):
    a = _account(db, "dated@x.com")
    sheet = order_ids.read_id_sheet(
        b"Email,Order ID,Order Date\ndated@x.com,OD1234567890,17-04-2025\ndated@x.com,OD1234567891,garbage\n", "ids.csv"
    )
    assert [r.reason for r in sheet.rejected] == ["not a date: 'garbage' (use DD-MM-YYYY)"]

    order_ids.import_sheet(db, sheet)

    row = db.query(Order).filter(Order.order_id == "OD1234567890").one()
    assert row.placed_at == datetime(2025, 4, 16, 18, 30)  # 17 Apr 2025 00:00 IST, stored as UTC
    assert row.account_id == a.id


# --- C6 / C7 / C9 / C11: order details --------------------------------------------------
def test_undated_orders_are_matched_on_their_recorded_date(api, db):
    e = _account(db, "e@x.com")
    _order(db, e, order_id="OD1111111111", placed_at=datetime(2025, 4, 17, 9, 0))
    order_ids.import_sheet(db, order_ids.read_id_sheet(b"Email,Order ID\ne@x.com,OD2222222222\n", "ids.csv"))

    out = api.post("/orders/orderDetails", json={"emailid": "e@x.com", "fromDate": "2020-01-01", "toDate": "2099-12-31"}).json()

    assert {entry["orderId"] for entry in out["data"]} == {"OD1111111111", "OD2222222222"}
    assert "recordedDate" in out["filters"]["dateField"]


def test_an_unknown_payment_mode_is_not_reported_as_captured(api, db):
    _account(db, "f@x.com")
    order_ids.import_sheet(db, order_ids.read_id_sheet(b"Email,Order ID\nf@x.com,OD4000000000\n", "ids.csv"))

    listed = api.post("/orders/orderDetails", json={"emailid": "f@x.com"}).json()["data"][0]
    assert (listed["paymentMethod"], listed["paymentStatus"]) == ("UNKNOWN", "UNKNOWN")
    one = api.post("/orders/orderDetails", json={"orderId": "OD4000000000"}).json()["data"]
    assert one["payments"][0]["status"] == "UNKNOWN"
    assert one["metadata"]["source"] == "UPLOAD"


def test_two_registrations_under_one_name_give_no_gstin_unless_the_order_recorded_it(api, db):
    g = _account(db, "g@x.com")
    db.add_all([
        Gst(label="UP", gstin="09AAAAA0000A1Z5", business_name="Kikaku Exim"),
        Gst(label="DL", gstin="07BBBBB0000B1Z5", business_name="Kikaku Exim"),
    ])
    db.commit()
    by_name = _order(db, g, order_id="OD5000000001", gst_name="Kikaku Exim")
    recorded = _order(db, g, order_id="OD5000000002", gst_name="Kikaku Exim", gstin="09AAAAA0000A1Z5")

    assert api.post("/orders/orderDetails", json={"orderId": by_name.order_id}).json()["data"]["customer"]["gstNumber"] is None
    assert api.post("/orders/orderDetails", json={"orderId": recorded.order_id}).json()["data"]["customer"]["gstNumber"] == "09AAAAA0000A1Z5"


# --- C8: the CSV's Cash on Delivery column -------------------------------------------
def test_the_csv_leaves_cash_on_delivery_blank_when_the_mode_is_unknown(api, db):
    _account(db, "h@x.com")
    order_ids.import_sheet(db, order_ids.read_id_sheet(b"Email,Order ID\nh@x.com,OD6000000000\n", "ids.csv"))

    lines = api.get("/orders/export.csv").text.splitlines()
    header = lines[0].lstrip("﻿").split(",")
    assert lines[1].split(",")[header.index("Cash on Delivery")] == ""
    assert lines[1].split(",")[header.index("Payment Mode")] == "unknown"


# --- C12: the workbook tells units apart ---------------------------------------------
def test_the_workbook_tells_the_units_of_one_order_apart(api, db):
    i = _account(db, "i@x.com")
    order = _order(db, i, order_id="OD3000000000", qty=3, amount=1000.0)
    apply_observations(db, order, [ScrapedOrder(order_id="OD3000000000", unit_id=f"W{n}") for n in (1, 2, 3)])
    db.commit()

    sheet = load_workbook(io.BytesIO(api.get("/orders/export").content)).active
    header = [c.value for c in sheet[1]]
    rows = [[c.value for c in r] for r in sheet.iter_rows(min_row=2)]
    assert [(r[header.index("Unit No")], r[header.index("Unit ID")]) for r in rows] == [(1, "W1"), (2, "W2"), (3, "W3")]


# --- C10 / C14 / C16: the by-id read-back request ----------------------------------------
@pytest.fixture
def sync_client(monkeypatch) -> TestClient:
    application = FastAPI()
    application.include_router(sync_api.router)
    monkeypatch.setattr(sync_api, "_spawn", lambda factory, job_id: None)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


def test_blank_or_malformed_ids_are_refused_rather_than_widening_the_run(sync_client, db):
    _account(db, "j@x.com")
    assert sync_client.post("/jobs/sync", json={"order_ids": [" ", ""]}).status_code == 422
    assert sync_client.post("/jobs/sync", json={"order_ids": ["not-an-id"]}).status_code == 422
    assert db.query(Job).count() == 0


def test_a_by_id_run_respects_the_accounts_ceiling(sync_client, db, monkeypatch):
    from app.api import jobs as jobs_api

    k = _account(db, "k@x.com")
    m = _account(db, "m@x.com")
    _order(db, k, order_id="OD7000000001")
    _order(db, m, order_id="OD7000000002")
    settings = jobs_api.get_settings()
    monkeypatch.setattr(settings, "max_accounts_per_run", 1)

    response = sync_client.post("/jobs/sync", json={"order_ids": ["OD7000000001", "OD7000000002"]})
    assert response.status_code == 422


def test_more_than_five_hundred_ids_are_accepted_in_one_run(sync_client, db):
    n = _account(db, "n@x.com")
    ids = [f"OD{1000000000 + i}" for i in range(600)]
    db.add_all([Order(account_id=n.id, order_id=value, status=OrderStatus.placed) for value in ids])
    db.commit()

    response = sync_client.post("/jobs/sync", json={"order_ids": ids})
    assert response.status_code == 201
    assert len(db.get(Job, response.json()["id"]).payload["order_ids"]) == 600


# --- C17: the by-id fallback keeps the ids the landed address carries --------------------
def test_the_no_links_fallback_reads_the_unit_ids_off_the_landed_url():
    landed = "https://www.flipkart.com/order_details?order_id=OD1137&item_id=IT9&unit_id=UN9"
    page = _SyncPage({landed: DETAIL_0})
    page.redirect_on[sync.detail_url("OD1137")] = landed

    [unit] = asyncio.run(sync.scrape_order(page, "OD1137"))

    assert (unit.order_id, unit.item_id, unit.unit_id, unit.detail_href) == ("OD1137", "IT9", "UN9", landed)


# --- C24: multi-unit paging and agreement across page, count, CSV and XLSX ---------------
def test_units_of_one_order_stay_together_across_pages(api, db):
    p = _account(db, "p@x.com")
    for n in range(7):
        order = _order(db, p, order_id=f"OD80000000{n}", qty=3, placed_at=datetime(2026, 10, 1 + n))
        apply_observations(db, order, [ScrapedOrder(order_id=order.order_id, unit_id=f"P{n}-{k}") for k in (1, 2, 3)])
    db.commit()

    seen: list[tuple[str, int]] = []
    for page_number in range(1, 6):
        page = api.get("/orders", params={"page": page_number, "size": 5}).json()
        seen.extend((row["order_id"], row["unit_position"]) for row in page["items"])
    assert len(seen) == 21 and len(set(seen)) == 21
    # Within an order, positions ascend and are never interleaved with another order.
    for order_id in {o for o, _ in seen}:
        assert [pos for o, pos in seen if o == order_id] == [1, 2, 3]


class _Seen:
    """An observation with a tracking id, which the scraper does not surface yet."""

    def __init__(self, **fields):
        for name in ("order_id", "unit_id", "item_id", "platform_status", "delivery_date", "seller", "tracking_id", "invoice_url", "delivery_otp"):
            setattr(self, name, None)
        self.__dict__.update(fields)


def test_unit_level_filters_agree_across_page_count_csv_and_xlsx(api, db):
    q = _account(db, "q@x.com")
    order = _order(db, q, order_id="OD9100000000", qty=3)
    apply_observations(
        db,
        order,
        [
            _Seen(order_id=order.order_id, unit_id="Q1", platform_status="Delivered", tracking_id="TRKQ1", delivery_date=datetime(2026, 10, 20)),
            _Seen(order_id=order.order_id, unit_id="Q2", platform_status="Shipped", tracking_id="TRKQ2"),
            _Seen(order_id=order.order_id, unit_id="Q3", platform_status="Cancelled"),
        ],
    )
    db.commit()

    for params, expected in (
        ({"delivery_state": "delivered"}, 1),
        ({"platform_status": "Shipped"}, 1),
        ({"tracking_id": "trkq1 trkq2"}, 2),
        ({"date_field": "delivery_date", "date_from": "2026-10-20", "date_to": "2026-10-20"}, 1),
    ):
        page = api.get("/orders", params=params).json()
        csv_rows = api.get("/orders/export.csv", params=params).text.splitlines()[1:]
        xlsx_rows = load_workbook(io.BytesIO(api.get("/orders/export", params=params).content)).active.max_row - 1
        assert (page["total"], len(page["items"]), len(csv_rows), xlsx_rows) == (expected,) * 4, params
