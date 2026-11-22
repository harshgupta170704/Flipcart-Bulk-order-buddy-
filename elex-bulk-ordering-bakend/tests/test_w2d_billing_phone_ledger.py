"""The Billing Phone on the ledger (package D, 26 Oct 2026).

The old portal showed "Business GST No" and "Billing Phone" on every ledger
row; the CSV here wrote Billing Phone blank on every row. Each order now
records the phone it was placed with (``Order.billing_phone``), and the ledger
shows it: the CSV's Billing Phone cell, ``billing_phone`` on every row of the
list, and a column a saved view may hold.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Job,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
)
from app.io import orders_csv
from app.jobs import tasks
from tests.fakes import FakePage


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def account(db) -> Account:
    row = Account(email="ledger@test.com", phone="9000000001", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    return row


def _order(db, account: Account, *, billing_phone: str | None, order_id: str) -> Order:
    row = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        order_id=order_id,
        model="vivo T5 Lite",
        payment_mode="cod",
        billing_phone=billing_phone,
        placed_at=datetime(2026, 10, 26, 4, 30),
    )
    db.add(row)
    db.commit()
    return row


def _csv_rows(api: TestClient, **params) -> dict[str, dict]:
    text = api.get("/orders/export.csv", params=params).text.lstrip("﻿")
    return {row["Order ID"]: row for row in csv.DictReader(io.StringIO(text))}


# --- the CSV cell -----------------------------------------------------------------
def test_the_billing_phone_cell_holds_the_phone_the_order_was_placed_with(api, db, account):
    _order(db, account, billing_phone="9876543210", order_id="OD100000000001")
    _order(db, account, billing_phone="+91 98765 43210", order_id="OD100000000002")
    _order(db, account, billing_phone=None, order_id="OD100000000003")

    rows = _csv_rows(api)

    # Digits kept as text, so a spreadsheet keeps every one (NFR-008).
    assert rows["OD100000000001"]["Billing Phone"] == '="9876543210"'
    # A phone as a person types it, kept as text exactly as typed: never read
    # as a formula (audit F-36), nor pushed along by a space (SC-007).
    assert rows["OD100000000002"]["Billing Phone"] == '="+91 98765 43210"'
    # Rows a read-back or an id sheet made hold none: blank, not a guess.
    assert rows["OD100000000003"]["Billing Phone"] == ""
    # The account's own phone is still the Phone Last 6 column, as before.
    assert rows["OD100000000001"]["Phone Last 6"] == '="000001"'


def test_an_identifier_cell_never_starts_like_a_formula():
    assert orders_csv.identifier_cell("=HYPERLINK(\"http://x\")") == ' =HYPERLINK("http://x")'
    assert orders_csv.identifier_cell("-123") == " -123"
    # What the column held before is written exactly as before.
    assert orders_csv.identifier_cell("4111111111111111") == '="4111111111111111"'
    assert orders_csv.identifier_cell("0123") == '="0123"'
    assert orders_csv.identifier_cell("n/a") == "n/a"
    assert orders_csv.identifier_cell(None) == ""


def test_a_runs_order_reaches_the_csv_with_the_accounts_billing_phone(api, db, monkeypatch):
    """From the account to the file: the run places the order with the billing
    phone, records it, and the export writes it."""
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    account = Account(
        email="run@test.com", phone="9000000001", billing_phone="8000000002", status=AccountStatus.session_valid
    )
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_):
        page = FakePage()
        yield page.context, page

    async def fake_run_order(page, *, address, target=None, qty=1, **_):
        assert address.phone == "8000000002"
        return {"status": OrderStatus.placed, "order_id": "OD200000000001", "amount": 500.0, "target": target, "qty": qty}

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_order", fake_run_order)
    asyncio.run(
        tasks.run_order_job(job.id, [account.id], product_link="https://www.flipkart.com/x/p/itm1", payment_mode="cod")
    )

    assert _csv_rows(api)["OD200000000001"]["Billing Phone"] == '="8000000002"'


# --- the list and the saved view -----------------------------------------------------
def test_every_ledger_row_carries_its_billing_phone(api, db, account):
    _order(db, account, billing_phone="9876543210", order_id="OD100000000001")
    _order(db, account, billing_phone=None, order_id="OD100000000002")

    items = {row["order_id"]: row for row in api.get("/orders").json()["items"]}

    assert items["OD100000000001"]["billing_phone"] == "9876543210"
    assert items["OD100000000002"]["billing_phone"] is None
    # Beside what the row already carried (the rest of OrderOut is unchanged).
    assert items["OD100000000001"]["timeline"] is None
    assert items["OD100000000001"]["account_email"] == "ledger@test.com"


def test_a_saved_view_may_show_the_billing_phone_column(api):
    saved = api.put("/orders/view", json={"columns": ["order_id", "billing_phone", "gst_name"], "filters": {}})

    assert saved.status_code == 200, saved.text
    assert api.get("/orders/view").json()["columns"] == ["order_id", "billing_phone", "gst_name"]
    # Still only the columns the page has.
    assert api.put("/orders/view", json={"columns": ["billing_phones"], "filters": {}}).status_code == 422
