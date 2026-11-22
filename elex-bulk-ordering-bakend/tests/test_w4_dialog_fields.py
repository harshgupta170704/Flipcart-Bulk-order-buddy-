"""Everything the row dialog shows of a unit (spec FR-013, 27 Oct 2026).

The dialog (the frontend's OrderDetail) is built from the ledger's row,
OrderOut, and that row left out fields the database holds: the GSTIN the run
billed under (the spec's "Business GST No"), when a read-back saw the delivery
address change, and the unit's platform. The unit's cancel and reschedule
requests showed nowhere in it; they are read once, when the dialog opens,
from GET /orders/{id}/requests, never with every row of the list.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.api import requests as requests_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus, OrderUnit, UnitRequest
from app.schemas.order import OrderOut


@pytest.fixture
def app_under_test() -> FastAPI:
    application = FastAPI()
    application.include_router(requests_api.router)
    application.include_router(orders_api.router)
    return application


@pytest.fixture
def api(app_under_test) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(app_under_test, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def ops(app_under_test) -> TestClient:
    """An operator: no role claim, not the configured admin."""
    token = create_access_token("ops@test.com")
    return TestClient(app_under_test, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def ledger(db) -> dict[str, int]:
    """A two-unit order a run placed under a GST, whose first unit's address
    changed, and another order that holds neither."""
    account = Account(email="dialog@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    run = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        model="vivo T5x 5G (Glossy Black, 128 GB)",
        order_id="OD100000000000000011",
        qty=2,
        gst_name="EXAMPLE TRADING",
        gstin="09AAAAA0000A1Z5",
        billing_phone="+91 98765 43210",
        placed_at=datetime(2026, 10, 24, 9, 0),
    )
    other = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        model="HP Backpack",
        order_id="OD100000000000000012",
        placed_at=datetime(2026, 10, 25, 9, 0),
    )
    db.add_all([run, other])
    db.commit()
    first, second = run.units
    first.delivery_address = "Home · 12 Park Street, Kolkata · 98xxxx1234"
    first.address_changed_at = datetime(2026, 10, 26, 7, 30)
    db.commit()
    return {
        "order": run.id,
        "first": first.id,
        "second": second.id,
        "other": other.id,
        "other_unit": other.units[0].id,
    }


# --- the row ----------------------------------------------------------------------
def test_order_out_carries_the_gstin_the_address_change_and_the_platform(db, ledger):
    unit = db.get(OrderUnit, ledger["first"])
    out = OrderOut.from_unit(unit, unit.order, "dialog@test.com")

    assert (out.gst_name, out.gstin) == ("EXAMPLE TRADING", "09AAAAA0000A1Z5")
    assert out.billing_phone == "+91 98765 43210"
    assert out.address_changed_at == datetime(2026, 10, 26, 7, 30)
    assert out.platform == "flipkart"


def test_every_ledger_row_carries_them_as_the_dialog_reads_them(api, ledger):
    rows = {row["id"]: row for row in api.get("/orders").json()["items"]}

    first = rows[ledger["first"]]
    assert first["gstin"] == "09AAAAA0000A1Z5"
    assert first["billing_phone"] == "+91 98765 43210"
    assert first["address_changed_at"] == "2026-10-26T07:30:00"
    assert first["platform"] == "flipkart"
    # The order's fields on each of its units; the address change is the unit's own.
    second = rows[ledger["second"]]
    assert (second["gstin"], second["address_changed_at"]) == ("09AAAAA0000A1Z5", None)
    # An order no run billed under a GST holds none.
    other = rows[ledger["other_unit"]]
    assert (other["gstin"], other["billing_phone"], other["platform"]) == (None, None, "flipkart")


# --- the unit's requests, once per dialog -------------------------------------------
def _seed_requests(db, ledger) -> dict[str, int]:
    rows = {
        # On this order's second unit: open.
        "cancel": UnitRequest(
            unit_pk=ledger["second"],
            kind="cancel",
            reason="Ordered twice by mistake",
            status="open",
            requested_by="ops@test.com",
            requested_at=datetime(2026, 10, 26, 10, 0),
        ),
        # On its first unit: done, with who closed it, when, and the note.
        "reschedule": UnitRequest(
            unit_pk=ledger["first"],
            kind="reschedule",
            requested_for=date(2026, 11, 2),
            status="done",
            requested_by="tester",
            requested_at=datetime(2026, 10, 25, 10, 0),
            closed_by="ops@test.com",
            closed_at=datetime(2026, 10, 25, 18, 0),
            note="Moved on the app",
        ),
        # On another order: never listed under this one.
        "elsewhere": UnitRequest(
            unit_pk=ledger["other_unit"],
            kind="cancel",
            reason="Not needed",
            status="open",
            requested_by="ops@test.com",
            requested_at=datetime(2026, 10, 27, 10, 0),
        ),
    }
    db.add_all(rows.values())
    db.commit()
    return {name: row.id for name, row in rows.items()}


def test_an_orders_requests_are_listed_newest_first_with_kind_status_when_and_by_whom(api, db, ledger):
    ids = _seed_requests(db, ledger)

    response = api.get(f"/orders/{ledger['order']}/requests")

    assert response.status_code == 200, response.text
    rows = response.json()
    # Every request on the order's units, open or closed, each naming its unit.
    assert [(row["id"], row["unit_pk"]) for row in rows] == [
        (ids["cancel"], ledger["second"]),
        (ids["reschedule"], ledger["first"]),
    ]
    cancel, reschedule = rows
    assert (cancel["kind"], cancel["status"], cancel["reason"]) == ("cancel", "open", "Ordered twice by mistake")
    assert (cancel["requested_by"], cancel["requested_at"]) == ("ops@test.com", "2026-10-26T10:00:00")
    assert (cancel["closed_by"], cancel["closed_at"], cancel["note"]) == (None, None, None)
    assert (reschedule["kind"], reschedule["status"], reschedule["requested_for"]) == ("reschedule", "done", "2026-11-02")
    assert (reschedule["requested_by"], reschedule["requested_at"]) == ("tester", "2026-10-25T10:00:00")
    assert (reschedule["closed_by"], reschedule["closed_at"], reschedule["note"]) == (
        "ops@test.com",
        "2026-10-25T18:00:00",
        "Moved on the app",
    )
    # In the very shape of the Requests log's rows.
    log = {row["id"]: row for row in api.get("/requests", params={"status": "all"}).json()["items"]}
    assert rows == [log[ids["cancel"]], log[ids["reschedule"]]]

    # The other order lists its own only; an order with none lists nothing.
    assert [row["id"] for row in api.get(f"/orders/{ledger['other']}/requests").json()] == [ids["elsewhere"]]
    db.query(UnitRequest).delete()
    db.commit()
    assert api.get(f"/orders/{ledger['order']}/requests").json() == []


def test_an_order_that_does_not_exist_is_a_404(api, ledger):
    missing = api.get("/orders/999999/requests")
    assert (missing.status_code, missing.json()["detail"]) == (404, "Order not found")
    # An id no row can have is the same answer, never the database's overflow.
    huge = api.get(f"/orders/{2**63}/requests")
    assert (huge.status_code, huge.json()["detail"]) == (404, "Order not found")


def test_an_operator_may_read_them(ops, db, ledger):
    _seed_requests(db, ledger)
    response = ops.get(f"/orders/{ledger['order']}/requests")
    assert response.status_code == 200
    assert len(response.json()) == 2


def test_they_are_behind_the_same_login_as_the_ledger(client, auth_headers, ledger):
    """On the real app, under the ledger's own router: no token, no list."""
    url = f"/orders/{ledger['order']}/requests"
    assert client.get("/orders").status_code == 401
    assert client.get(url).status_code == 401
    assert client.get(url, headers=auth_headers).status_code == 200
