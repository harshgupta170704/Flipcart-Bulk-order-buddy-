"""The cancel / reschedule request log (spec FR-020, decision C-4).

A request is a row for a person to action on Flipkart by hand; nothing here
touches the platform. These tests pin what the API promises: one row per unit
with the units it passed over named with a reason (a cancel on any settled
unit among them), the one-open-per-kind rule — and its 409 when two operators
slip past the check at once — what each kind must carry, the list's filter and
count, who closed a row and when, that a repeated status leaves the note
alone, that an operator may do all of it, and that the ledger's rows say
which requests are open on them.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.api import requests as requests_api
from app.core.security import create_access_token
from app.db.base import SessionLocal
from app.db.models import Account, AccountStatus, Order, OrderStatus, OrderUnit, UnitRequest


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
def anon(app_under_test) -> TestClient:
    return TestClient(app_under_test)


class _Seeded:
    """Three units by name: one delivered, one on its way, one never read back."""

    def __init__(self, account: Account, units: dict[str, int], orders: dict[str, Order]) -> None:
        self.account = account
        self.units = units
        self.orders = orders


@pytest.fixture
def seeded(db) -> _Seeded:
    account = Account(email="units@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    delivered = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        model="vivo T5x 5G (Glossy Black, 128 GB)",
        order_id="OD100000000000000001",
        unit_id="U-DELIVERED",
        platform_status="Delivered",
        synced_at=datetime(2026, 10, 20),
        placed_at=datetime(2026, 10, 18, 9, 0),
    )
    shipped = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        model="vivo T5x 5G (Glossy Black, 128 GB)",
        order_id="OD100000000000000002",
        unit_id="U-SHIPPED",
        platform_status="Shipped",
        synced_at=datetime(2026, 10, 20),
        placed_at=datetime(2026, 10, 19, 9, 0),
    )
    unread = Order(
        account_id=account.id,
        status=OrderStatus.placed,
        model="HP Backpack",
        order_id="OD100000000000000003",
        placed_at=datetime(2026, 10, 21, 9, 0),
    )
    db.add_all([delivered, shipped, unread])
    db.commit()
    for row in (delivered, shipped, unread):
        db.refresh(row)
    units = {
        "delivered": delivered.units[0].id,
        "shipped": shipped.units[0].id,
        "unread": unread.units[0].id,
    }
    return _Seeded(account, units, {"delivered": delivered, "shipped": shipped, "unread": unread})


# --- creating ---------------------------------------------------------------------
def test_a_cancel_skips_a_delivered_unit_and_says_why(api, seeded):
    response = api.post(
        "/requests",
        json={
            "unit_ids": [seeded.units["delivered"], seeded.units["shipped"]],
            "kind": "cancel",
            "reason": "Ordered twice by mistake",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert [row["unit_pk"] for row in body["created"]] == [seeded.units["shipped"]]
    assert body["skipped"] == [{"unit_id": seeded.units["delivered"], "why": "already delivered"}]

    created = body["created"][0]
    assert created["kind"] == "cancel"
    assert created["status"] == "open"
    assert created["reason"] == "Ordered twice by mistake"
    assert created["requested_by"] == "tester"
    assert created["requested_for"] is None
    assert created["closed_by"] is None and created["closed_at"] is None
    # Enough of the unit alongside to read the row on its own.
    assert created["order_id"] == "OD100000000000000002"
    assert created["unit_id"] == "U-SHIPPED"
    assert created["product"] == "vivo T5x 5G (Glossy Black, 128 GB)"
    assert created["platform_status"] == "Shipped"
    assert created["delivery_state"] == "in_transit"
    assert created["account_email"] == "units@test.com"
    assert created["order_row_id"] == seeded.orders["shipped"].id


def test_a_cancelled_unit_is_skipped_too(api, db, seeded):
    unit = db.get(type(seeded.orders["shipped"].units[0]), seeded.units["shipped"])
    unit.platform_status = "Cancelled - Order shipped"
    db.commit()
    response = api.post(
        "/requests", json={"unit_ids": [seeded.units["shipped"]], "kind": "cancel", "reason": "x"}
    )
    assert response.status_code == 201
    assert response.json()["created"] == []
    assert response.json()["skipped"] == [{"unit_id": seeded.units["shipped"], "why": "already cancelled"}]


@pytest.mark.parametrize(
    ("platform_status", "state"),
    [
        ("Return started", "returned"),
        ("Refund completed", "returned"),
        ("Undelivered", "failed"),
        ("Returned to origin", "failed"),
        ("Delivered", "delivered"),
        ("Cancelled - Order shipped", "cancelled"),
    ],
)
def test_a_cancel_is_refused_on_every_settled_unit_naming_the_state(api, db, seeded, platform_status, state):
    """FR-020: cancel only on undelivered rows. A returned or failed unit is
    settled too — nothing is left to cancel — so it is skipped like a
    delivered or cancelled one, with the state in the reason."""
    unit = db.get(OrderUnit, seeded.units["shipped"])
    unit.platform_status = platform_status
    db.commit()
    response = api.post("/requests", json={"unit_ids": [seeded.units["shipped"]], "kind": "cancel", "reason": "x"})
    assert response.status_code == 201
    assert response.json()["created"] == []
    assert response.json()["skipped"] == [{"unit_id": seeded.units["shipped"], "why": f"already {state}"}]


def test_a_second_open_request_of_the_same_kind_is_skipped(api, seeded):
    body = {"unit_ids": [seeded.units["shipped"]], "kind": "cancel", "reason": "Wrong colour"}
    first = api.post("/requests", json=body)
    assert first.status_code == 201 and len(first.json()["created"]) == 1

    second = api.post("/requests", json=body)
    assert second.status_code == 201, second.text
    assert second.json()["created"] == []
    assert second.json()["skipped"] == [
        {"unit_id": seeded.units["shipped"], "why": "already has an open cancel request"}
    ]

    # A different kind on the same unit is a different request.
    other = api.post(
        "/requests",
        json={"unit_ids": [seeded.units["shipped"]], "kind": "reschedule", "requested_for": "2026-11-02"},
    )
    assert other.status_code == 201
    assert len(other.json()["created"]) == 1


def test_two_operators_recording_the_same_cancel_at_once_cannot_both_win(api, db, seeded, monkeypatch):
    """The one-open-per-kind check is a read before a write. A rival's row
    landing in between is caught by the database's own rule (a unique index on
    open rows) and answered as a 409 — never two open cancels on one unit."""
    unit = seeded.units["shipped"]
    real = requests_api.core.create_requests

    def rival_lands_in_between(db_, **kwargs):
        created, skipped = real(db_, **kwargs)
        with SessionLocal() as rival:
            rival.add(UnitRequest(unit_pk=unit, kind="cancel", reason="rival", status="open", requested_by="rival@test.com"))
            rival.commit()
        return created, skipped

    monkeypatch.setattr(requests_api.core, "create_requests", rival_lands_in_between)
    response = api.post("/requests", json={"unit_ids": [unit], "kind": "cancel", "reason": "x"})
    assert response.status_code == 409, response.text
    assert "open cancel request" in response.json()["detail"]
    assert db.query(UnitRequest).filter(UnitRequest.unit_pk == unit, UnitRequest.status == "open").count() == 1


def test_a_done_request_can_be_followed_by_a_fresh_one(api, seeded):
    body = {"unit_ids": [seeded.units["shipped"]], "kind": "cancel", "reason": "Wrong colour"}
    first = api.post("/requests", json=body).json()["created"][0]
    assert api.patch(f"/requests/{first['id']}", json={"status": "done"}).status_code == 200

    again = api.post("/requests", json=body)
    assert again.status_code == 201
    assert len(again.json()["created"]) == 1


def test_a_unit_that_does_not_exist_is_skipped_not_an_error(api, seeded):
    response = api.post(
        "/requests",
        json={"unit_ids": [999_999, seeded.units["unread"]], "kind": "cancel", "reason": "Not needed"},
    )
    assert response.status_code == 201
    body = response.json()
    assert [row["unit_pk"] for row in body["created"]] == [seeded.units["unread"]]
    assert body["skipped"] == [{"unit_id": 999_999, "why": "no such unit"}]
    # A unit never read back has no state yet, so it can still be cancelled.
    assert body["created"][0]["delivery_state"] == "unknown"


def test_a_reschedule_records_the_wanted_date(api, seeded):
    response = api.post(
        "/requests",
        json={"unit_ids": [seeded.units["shipped"]], "kind": "reschedule", "requested_for": "2026-11-02"},
    )
    assert response.status_code == 201, response.text
    created = response.json()["created"][0]
    assert created["kind"] == "reschedule"
    assert created["requested_for"] == "2026-11-02"
    assert created["reason"] is None


def test_a_reschedule_may_be_recorded_on_a_delivered_unit_only_a_cancel_is_refused(api, seeded):
    response = api.post(
        "/requests",
        json={"unit_ids": [seeded.units["delivered"]], "kind": "reschedule", "requested_for": "2026-11-02"},
    )
    assert response.status_code == 201
    assert len(response.json()["created"]) == 1


@pytest.mark.parametrize(
    "body",
    [
        # a reschedule without the new date
        {"unit_ids": [1], "kind": "reschedule"},
        {"unit_ids": [1], "kind": "reschedule", "reason": "later please"},
        # a cancel without a reason, or with a blank one
        {"unit_ids": [1], "kind": "cancel"},
        {"unit_ids": [1], "kind": "cancel", "reason": "   "},
        # nothing to act on, or an unknown kind
        {"unit_ids": [], "kind": "cancel", "reason": "x"},
        {"unit_ids": [1], "kind": "return", "reason": "x"},
        # an id the database cannot bind
        {"unit_ids": [2**40], "kind": "cancel", "reason": "x"},
        # a typo must be loud
        {"unit_ids": [1], "kind": "cancel", "reason": "x", "reasons": "x"},
    ],
)
def test_what_each_kind_needs_is_a_422(api, seeded, body):
    assert api.post("/requests", json=body).status_code == 422


def test_more_than_500_units_in_one_call_is_a_422(api, seeded):
    response = api.post(
        "/requests", json={"unit_ids": list(range(1, 502)), "kind": "cancel", "reason": "x"}
    )
    assert response.status_code == 422


def test_repeats_in_the_list_count_once(api, seeded):
    unit = seeded.units["shipped"]
    response = api.post("/requests", json={"unit_ids": [unit, unit], "kind": "cancel", "reason": "x"})
    assert response.status_code == 201
    assert len(response.json()["created"]) == 1
    assert response.json()["skipped"] == []


# --- listing ----------------------------------------------------------------------
def _seed_requests(api, seeded) -> dict[str, int]:
    cancel = api.post(
        "/requests", json={"unit_ids": [seeded.units["shipped"]], "kind": "cancel", "reason": "Wrong colour"}
    ).json()["created"][0]["id"]
    reschedule = api.post(
        "/requests",
        json={"unit_ids": [seeded.units["unread"]], "kind": "reschedule", "requested_for": "2026-11-02"},
    ).json()["created"][0]["id"]
    return {"cancel": cancel, "reschedule": reschedule}


def test_the_list_defaults_to_open_newest_first_with_the_total(api, seeded):
    ids = _seed_requests(api, seeded)

    page = api.get("/requests").json()
    assert page["total"] == 2
    assert [row["id"] for row in page["items"]] == [ids["reschedule"], ids["cancel"]]

    api.patch(f"/requests/{ids['cancel']}", json={"status": "done", "note": "Cancelled on the app"})

    assert api.get("/requests", params={"status": "open"}).json()["total"] == 1
    done = api.get("/requests", params={"status": "done"}).json()
    assert done["total"] == 1
    assert done["items"][0]["id"] == ids["cancel"]
    assert done["items"][0]["note"] == "Cancelled on the app"
    assert api.get("/requests", params={"status": "dropped"}).json()["total"] == 0
    assert api.get("/requests", params={"status": "all"}).json()["total"] == 2
    # Blank is what a dropdown on "all" sends.
    assert api.get("/requests", params={"status": ""}).json()["total"] == 2


def test_the_list_filters_by_kind_and_pages(api, seeded):
    ids = _seed_requests(api, seeded)

    only_cancel = api.get("/requests", params={"kind": "cancel"}).json()
    assert only_cancel["total"] == 1
    assert only_cancel["items"][0]["id"] == ids["cancel"]
    assert api.get("/requests", params={"kind": ""}).json()["total"] == 2

    first = api.get("/requests", params={"page": 1, "size": 1}).json()
    second = api.get("/requests", params={"page": 2, "size": 1}).json()
    assert first["total"] == second["total"] == 2
    assert [first["items"][0]["id"], second["items"][0]["id"]] == [ids["reschedule"], ids["cancel"]]


@pytest.mark.parametrize("params", [{"status": "closed"}, {"kind": "return"}, {"size": 0}, {"page": 0}])
def test_an_unknown_filter_value_is_a_422(api, seeded, params):
    assert api.get("/requests", params=params).status_code == 422


# --- closing and reopening ------------------------------------------------------------
def test_marking_done_records_who_and_when_and_reopening_clears_it(api, db, seeded):
    ids = _seed_requests(api, seeded)

    done = api.patch(f"/requests/{ids['cancel']}", json={"status": "done", "note": "Cancelled on the app"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "done"
    assert body["closed_by"] == "tester"
    assert body["closed_at"] is not None
    assert body["note"] == "Cancelled on the app"
    assert db.get(UnitRequest, ids["cancel"]).closed_by == "tester"

    reopened = api.patch(f"/requests/{ids['cancel']}", json={"status": "open"})
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"
    assert reopened.json()["closed_by"] is None
    assert reopened.json()["closed_at"] is None
    assert reopened.json()["note"] is None
    assert api.get("/requests").json()["total"] == 2


def test_dropping_is_a_close_too(api, seeded):
    ids = _seed_requests(api, seeded)
    dropped = api.patch(f"/requests/{ids['reschedule']}", json={"status": "dropped", "note": "Not needed"})
    assert dropped.status_code == 200
    assert dropped.json()["status"] == "dropped"
    assert dropped.json()["closed_by"] == "tester"
    assert api.get("/requests", params={"status": "dropped"}).json()["total"] == 1


def test_closing_twice_keeps_the_first_close(api, seeded):
    ids = _seed_requests(api, seeded)
    first = api.patch(f"/requests/{ids['cancel']}", json={"status": "done", "note": "first"}).json()
    again = api.patch(f"/requests/{ids['cancel']}", json={"status": "done"}).json()
    assert again["closed_at"] == first["closed_at"]
    assert again["note"] == "first"


def test_repeating_open_on_an_open_request_leaves_its_note_alone_unless_one_is_written(api, seeded):
    ids = _seed_requests(api, seeded)
    noted = api.patch(f"/requests/{ids['cancel']}", json={"status": "open", "note": "Seller asked us to wait"}).json()
    assert (noted["status"], noted["note"], noted["closed_by"]) == ("open", "Seller asked us to wait", None)

    again = api.patch(f"/requests/{ids['cancel']}", json={"status": "open"}).json()
    assert (again["status"], again["note"]) == ("open", "Seller asked us to wait")

    rewritten = api.patch(f"/requests/{ids['cancel']}", json={"status": "open", "note": "Cancelling after all"}).json()
    assert rewritten["note"] == "Cancelling after all"


def test_a_reopen_is_refused_while_another_open_request_of_the_kind_exists(api, seeded):
    body = {"unit_ids": [seeded.units["shipped"]], "kind": "cancel", "reason": "x"}
    first = api.post("/requests", json=body).json()["created"][0]["id"]
    api.patch(f"/requests/{first}", json={"status": "done"})
    second = api.post("/requests", json=body).json()["created"][0]["id"]
    assert second != first

    refused = api.patch(f"/requests/{first}", json={"status": "open"})
    assert refused.status_code == 409
    assert "open cancel request" in refused.json()["detail"]


def test_a_reopen_that_slips_past_the_check_is_refused_all_the_same(api, db, seeded, monkeypatch):
    body = {"unit_ids": [seeded.units["shipped"]], "kind": "cancel", "reason": "x"}
    first = api.post("/requests", json=body).json()["created"][0]["id"]
    api.patch(f"/requests/{first}", json={"status": "done"})
    api.post("/requests", json=body)  # a fresh open one on the unit
    # The check looked a moment too early and saw nothing...
    monkeypatch.setattr(requests_api.core, "has_other_open", lambda _db, _row: False)

    refused = api.patch(f"/requests/{first}", json={"status": "open"})

    # ...and the database's own rule still said no.
    assert refused.status_code == 409
    assert "open cancel request" in refused.json()["detail"]
    db.expire_all()
    assert db.get(UnitRequest, first).status == "done"


def test_an_unknown_request_is_a_404(api, seeded):
    assert api.patch("/requests/424242", json={"status": "done"}).status_code == 404


@pytest.mark.parametrize("body", [{"status": "closed"}, {"status": "done", "notes": "x"}, {}])
def test_a_bad_patch_body_is_a_422(api, seeded, body):
    ids = _seed_requests(api, seeded)
    assert api.patch(f"/requests/{ids['cancel']}", json=body).status_code == 422


# --- who may ----------------------------------------------------------------------------
def test_an_operator_may_record_list_and_close_requests(ops, seeded):
    created = ops.post(
        "/requests", json={"unit_ids": [seeded.units["shipped"]], "kind": "cancel", "reason": "x"}
    )
    assert created.status_code == 201, created.text
    row = created.json()["created"][0]
    assert row["requested_by"] == "ops@test.com"

    assert ops.get("/requests").json()["total"] == 1

    done = ops.patch(f"/requests/{row['id']}", json={"status": "done"})
    assert done.status_code == 200
    assert done.json()["closed_by"] == "ops@test.com"


def test_anonymous_is_refused(anon, seeded):
    assert anon.get("/requests").status_code == 401
    assert anon.post("/requests", json={"unit_ids": [1], "kind": "cancel", "reason": "x"}).status_code == 401
    assert anon.patch("/requests/1", json={"status": "done"}).status_code == 401


# --- the ledger's rows ----------------------------------------------------------------------
def test_the_ledger_rows_carry_their_open_request_kinds(api, seeded):
    api.post("/requests", json={"unit_ids": [seeded.units["shipped"]], "kind": "cancel", "reason": "x"})
    api.post(
        "/requests",
        json={"unit_ids": [seeded.units["shipped"]], "kind": "reschedule", "requested_for": "2026-11-02"},
    )
    dropped = api.post(
        "/requests",
        json={"unit_ids": [seeded.units["unread"]], "kind": "reschedule", "requested_for": "2026-11-02"},
    ).json()["created"][0]["id"]
    api.patch(f"/requests/{dropped}", json={"status": "dropped"})

    by_unit = {row["id"]: row["open_request_kinds"] for row in api.get("/orders").json()["items"]}
    assert by_unit[seeded.units["shipped"]] == ["cancel", "reschedule"]
    # A closed request is not a tag; a unit with none has an empty list, not null.
    assert by_unit[seeded.units["unread"]] == []
    assert by_unit[seeded.units["delivered"]] == []
