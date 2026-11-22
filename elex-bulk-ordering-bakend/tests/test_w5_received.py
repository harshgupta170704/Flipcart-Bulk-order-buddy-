"""A unit's goods marked as in hand, and the mark taken back (spec FR-001,
FR-017: the ledger's "Received"; spec audit of 27 Oct 2026). Set by a person
only — who and when — and shown on the ledger row, the dialog and the CSV."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import orders as orders_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Order, OrderStatus


def _client(subject: str, role: str | None = None) -> TestClient:
    application = FastAPI()
    application.include_router(orders_api.router)
    token = create_access_token(subject, role=role) if role else create_access_token(subject)
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def unit_ids(db) -> list[int]:
    account = Account(email="hand@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    order = Order(account_id=account.id, status=OrderStatus.placed, order_id="OD338975799606623301", qty=2, model="vivo")
    db.add(order)
    db.commit()
    return [unit.id for unit in sorted(order.units, key=lambda u: u.position)]


def _rows(api) -> dict[int, dict]:
    return {item["id"]: item for item in api.get("/orders").json()["items"]}


def test_an_operator_marks_goods_received_and_takes_the_mark_back(unit_ids):
    ops = _client("ops@test.com")
    first, second = unit_ids

    out = ops.post("/orders/received", json={"unit_ids": [first, 99999], "received": True})
    assert out.status_code == 200, out.text
    assert out.json() == {"changed": 1, "missing": [99999]}
    rows = _rows(ops)
    assert rows[first]["received_by"] == "ops@test.com" and rows[first]["received_at"]
    assert rows[second]["received_at"] is None

    # Marked again: the first mark stands, nothing changes.
    marked_at = rows[first]["received_at"]
    again = _client("other@test.com").post("/orders/received", json={"unit_ids": [first], "received": True})
    assert again.json()["changed"] == 0
    assert _rows(ops)[first]["received_at"] == marked_at

    back = ops.post("/orders/received", json={"unit_ids": [first], "received": False})
    assert back.json()["changed"] == 1
    assert _rows(ops)[first]["received_at"] is None


def test_the_csv_says_yes_once_marked_and_nothing_before(unit_ids):
    api = _client("tester", role="admin")
    api.post("/orders/received", json={"unit_ids": [unit_ids[0]], "received": True})
    text = api.get("/orders/export.csv").text.lstrip("﻿")
    received = {row["Unit No"] if "Unit No" in row else row.get("Unit Position"): row["Received"] for row in csv.DictReader(io.StringIO(text))}
    assert sorted(received.values()) == ["", "Yes"]


@pytest.mark.parametrize("body", [{"unit_ids": [], "received": True}, {"unit_ids": [1], "received": True, "extra": 1}, {"unit_ids": [0], "received": True}])
def test_a_bad_request_is_refused(unit_ids, body):
    assert _client("ops@test.com").post("/orders/received", json=body).status_code == 422
