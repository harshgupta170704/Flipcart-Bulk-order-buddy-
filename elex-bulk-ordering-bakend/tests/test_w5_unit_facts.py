"""Each unit's IMEI, the name its order was billed to and its last tracking
update, on the ledger page and in the CSV (spec FR-001, FR-013, FR-017,
SC-007; spec audit of 27 Oct 2026).

Read from what is already on file — the order's product invoices, and the
order page's timeline the read-back saved — and loaded for a whole page, or a
chunk of an export, at once: never one query per row.
"""

from __future__ import annotations

import contextlib
import csv
import io
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.api import orders as orders_api
from app.core import unit_facts
from app.core.security import create_access_token
from app.core.unit_facts import SCOPE_ORDER, SCOPE_POSITION, SCOPE_UNIT, ProductFacts
from app.db.base import engine
from app.db.models import Account, AccountStatus, Invoice, Order, OrderStatus

IMEI_1, IMEI_2, IMEI_3 = "358107262645105", "358107262645113", "358107262645121"


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token("tester", role="admin")
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def account(db) -> Account:
    account = Account(email="facts@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    return account


@contextlib.contextmanager
def _statements():
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _order(db, account: Account, order_id: str, *, qty: int = 1, **over) -> Order:
    order = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        order_id=order_id,
        qty=qty,
        amount=36000.0,
        model="vivo T5 Lite",
        placed_at=datetime(2026, 10, 24, 4, 30),
        **over,
    )
    db.add(order)
    db.commit()
    return order


def _invoice(db, order: Order, *, kind: str = "product", imeis: list[str] | None = None, buyer: str | None = None,
             day: int = 25, n: int = 0) -> Invoice:
    invoice = Invoice(
        order_pk=order.id,
        order_id=order.order_id,
        kind=kind,
        path=f"2026/10/{day}/{order.order_id}-{kind}{n}.pdf",
        size=1,
        sha256=f"{order.id:08d}{kind[:4]}{n}".ljust(64, "0"),
        source="upload",
        invoice_type="Product" if kind == "product" else "Fee",
        invoice_date=datetime(2026, 10, day),
        buyer_name=buyer,
        lines=[{"title": "vivo T5 Lite", "imei": text} for text in (imeis or [])],
    )
    db.add(invoice)
    db.commit()
    return invoice


# --- the rules ----------------------------------------------------------------------------
def test_an_orders_product_invoices_give_its_imeis_once_each_and_the_name_billed_to(db, account):
    order = _order(db, account, "OD338975799606623201", qty=3)
    _invoice(db, order, imeis=[f"{IMEI_1}"], buyer=None, day=24, n=1)
    _invoice(db, order, imeis=[f"{IMEI_2}, {IMEI_3}", IMEI_1], buyer="  Sk Enterprises  ", day=25, n=2)
    # A fee invoice lists no IMEI of the order's units, whatever digits it prints.
    _invoice(db, order, kind="fee", imeis=["999999999999999"], buyer="Someone Else", n=3)

    facts = unit_facts.product_facts(db, [order.id])

    assert facts == {order.id: ProductFacts((IMEI_1, IMEI_2, IMEI_3), "Sk Enterprises")}
    assert unit_facts.product_facts(db, []) == {}


@pytest.mark.parametrize(
    ("imeis", "position", "units", "expected"),
    [
        ((), 1, 1, (None, None)),
        ((IMEI_1,), 1, 1, (IMEI_1, SCOPE_UNIT)),
        ((IMEI_1, IMEI_2), 2, 2, (IMEI_2, SCOPE_POSITION)),
        ((IMEI_1, IMEI_2), 1, 2, (IMEI_1, SCOPE_POSITION)),
        # Fewer IMEIs than units: which is whose cannot be told, so the order's.
        ((IMEI_1,), 2, 2, (IMEI_1, SCOPE_ORDER)),
        ((IMEI_1, IMEI_2, IMEI_3), 1, 2, (f"{IMEI_1}, {IMEI_2}, {IMEI_3}", SCOPE_ORDER)),
    ],
)
def test_a_units_imei_is_its_own_only_when_it_can_be_told(imeis, position, units, expected):
    assert unit_facts.unit_imei(ProductFacts(imeis), position, units) == expected


def test_the_last_tracking_update_is_the_newest_step_that_had_happened_when_read(db, account):
    order = _order(db, account, "OD338975799606623202")
    unit = order.units[0]
    unit.timeline = [
        {"status": "Order Confirmed", "date": "2026-10-25T00:00:00"},
        {"status": "Shipped", "date": "2026-10-27T00:00:00"},  # still expected when read on the 26th
        {"status": "Delivery", "date": "2026-11-01T00:00:00"},
        {"status": "Out For Delivery", "date": None},
    ]
    unit.synced_at = datetime(2026, 10, 26, 6, 0)  # 11:30 IST on the 26th
    assert unit_facts.last_tracking_update(unit) == (datetime(2026, 10, 25), "Order Confirmed")

    unit.synced_at = datetime(2026, 10, 27, 12, 0)
    assert unit_facts.last_tracking_update(unit) == (datetime(2026, 10, 27), "Shipped")

    unit.timeline = None
    assert unit_facts.last_tracking_update(unit) is None


# --- on the page ------------------------------------------------------------------------------
def test_the_ledger_rows_carry_them_and_the_page_costs_no_query_per_order(db, api, account):
    one = _order(db, account, "OD338975799606623203", qty=2)
    _invoice(db, one, imeis=[f"{IMEI_1} {IMEI_2}"], buyer="Sk Enterprises")
    rows = {item["unit_position"]: item for item in api.get("/orders").json()["items"]}

    assert (rows[1]["imei"], rows[1]["imei_scope"]) == (IMEI_1, "position")
    assert (rows[2]["imei"], rows[2]["imei_scope"]) == (IMEI_2, "position")
    assert rows[1]["billing_name"] == "Sk Enterprises"
    assert rows[1]["unit_amount"] == 18000.0

    with _statements() as few:
        assert api.get("/orders").status_code == 200
    for n in range(12):
        order = _order(db, account, f"OD3389757996066233{n:02d}")
        _invoice(db, order, imeis=[IMEI_3], buyer="Sk Enterprises", n=n)
    with _statements() as many:
        assert len(api.get("/orders").json()["items"]) == 14
    assert len(many) == len(few)


# --- in the CSV ---------------------------------------------------------------------------------
def _csv(api) -> dict[str, dict]:
    text = api.get("/orders/export.csv").text.lstrip("﻿")
    return {row["Order ID"]: row for row in csv.DictReader(io.StringIO(text))}


def test_the_csv_fills_imei_billing_name_tracking_and_the_billed_gst(db, api, account):
    run = _order(db, account, "OD338975799606623204", gst_name="Run's Entity", gstin="09AAAAA0000A1Z5")
    _invoice(db, run, imeis=[IMEI_1], buyer="Sk Enterprises")
    unit = run.units[0]
    unit.timeline = [{"status": "Shipped", "date": "2026-10-27T00:00:00"}]
    unit.synced_at = datetime(2026, 10, 28, 6, 0)
    # An order handed in by id: no GST of a run's, only the one its page shows.
    handed_in = _order(db, account, "OD338975799606623205")
    handed_in.units[0].billed_gstin = "09BUAPA6939F1ZP"
    handed_in.units[0].billed_business_name = "Page Entity"
    db.commit()

    rows = _csv(api)
    first = rows['="OD338975799606623204"'] if '="OD338975799606623204"' in rows else rows["OD338975799606623204"]
    assert first["IMEI"] == f'="{IMEI_1}"'
    assert first["Billing Name"] == "Sk Enterprises"
    assert first["Last Tracking Update"] == "27-10-2026 Shipped"
    assert (first["Business Name"], first["Business GST No"]) == ("Run's Entity", "09AAAAA0000A1Z5")
    second = rows['="OD338975799606623205"'] if '="OD338975799606623205"' in rows else rows["OD338975799606623205"]
    assert (second["Business Name"], second["Business GST No"]) == ("Page Entity", "09BUAPA6939F1ZP")
    assert (second["IMEI"], second["Billing Name"], second["Last Tracking Update"]) == ("", "", "")


def test_the_csv_reads_the_invoices_once_per_chunk_not_once_per_line(db, api, account):
    for n in range(3):
        order = _order(db, account, f"OD3389757996066232{n:02d}")
        _invoice(db, order, imeis=[IMEI_1], n=n)
    with _statements() as few:
        _csv(api)
    for n in range(3, 25):
        order = _order(db, account, f"OD3389757996066232{n:02d}")
        _invoice(db, order, imeis=[IMEI_2], n=n)
    with _statements() as many:
        rows = _csv(api)
    assert len(rows) == 25
    assert len(many) == len(few)


def test_an_export_reads_the_invoices_chunk_by_chunk_while_its_rows_stream(db, account):
    """The facts of each chunk are read while the rows' own query is still
    being read (a server-side cursor on PostgreSQL): every row, in every
    chunk, gets its IMEI."""
    from app.core import order_filter

    for n in range(7):
        order = _order(db, account, f"OD3389757996066234{n:02d}")
        _invoice(db, order, imeis=[f"35810726264{n:04d}"], buyer=f"Buyer {n}", n=n)
    rows = list(order_filter.export_rows(db, order_filter.OrderFilter(), chunk=2))
    assert len(rows) == 7
    assert sorted(row.product.imeis[0] for row in rows) == [f"35810726264{n:04d}" for n in range(7)]
    assert all(row.product.billing_name.startswith("Buyer ") for row in rows)


def test_the_csv_gives_the_orders_own_wording_beside_each_units(db, api, account):
    """"Platform Order Status" was always blank: it is the order's own wording
    as the read-back mirrors it, beside each unit's Status."""
    order = _order(db, account, "OD338975799606623290", qty=2)
    order.platform_status = "Delivery expected by Nov 01"
    units = sorted(order.units, key=lambda u: u.position)
    units[0].platform_status = "Cancelled"
    units[1].platform_status = "Delivery expected by Nov 01"
    db.commit()
    text = api.get("/orders/export.csv").text.lstrip("﻿")
    rows = [row for row in csv.DictReader(io.StringIO(text)) if "OD338975799606623290" in row["Order ID"]]
    assert [(row["Status"], row["Platform Order Status"]) for row in rows] == [
        ("Cancelled", "Delivery expected by Nov 01"),
        ("Delivery expected by Nov 01", "Delivery expected by Nov 01"),
    ]
