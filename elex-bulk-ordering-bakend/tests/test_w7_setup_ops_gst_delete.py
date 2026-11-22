"""Deleting a GST says what it unlinked (review of 27 Oct 2026).

The delete answered 204 with nothing in it, so the Address Book worded its
note ("its 3 addresses are no longer linked to a GST") from the list it had
loaded earlier — out of date as soon as an address or an account was linked
or unlinked in between, by another admin or in another tab. The delete now
answers 200 with the counts its own UPDATEs made, which is what the page words
its note from. Every value here is made up.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import accounts as accounts_api
from app.api import addresses as addresses_api
from app.api import gsts as gsts_api
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus


@pytest.fixture
def api() -> TestClient:
    application = FastAPI()
    for router in (gsts_api.router, addresses_api.router, accounts_api.router):
        application.include_router(router)
    return TestClient(application, headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"})


def _gst(api: TestClient, label: str = "SONIC") -> int:
    body = {"label": label, "gstin": "33AAECS4321F1ZK", "business_name": "Sonic Kommerce"}
    response = api.post("/gsts", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _address(api: TestClient, label: str, gst_id: int | None = None) -> int:
    body = {"label": label, "name": "Warehouse", "line1": "12 MG Road", "pincode": "560001", "gst_id": gst_id}
    response = api.post("/addresses", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _accounts(db, count: int) -> list[int]:
    rows = [Account(email=f"buyer{i}@test.com", status=AccountStatus.session_valid) for i in range(count)]
    db.add_all(rows)
    db.commit()
    return [row.id for row in rows]


def test_the_delete_answers_how_many_addresses_and_accounts_it_unlinked(api, db):
    gst_id = _gst(api)
    for i in range(2):
        _address(api, f"WH-{i}", gst_id)
    _address(api, "LOOSE")
    linked = api.post("/accounts/gst", json={"gst_id": gst_id, "account_ids": _accounts(db, 3)})
    assert linked.status_code == 200, linked.text

    response = api.delete(f"/gsts/{gst_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {"unlinked_addresses": 2, "unlinked_accounts": 3}


def test_the_counts_are_the_servers_at_the_delete_not_the_lists_from_before(api, db):
    """The page's list said one address; another was linked since."""
    gst_id = _gst(api)
    _address(api, "WH-0", gst_id)
    listed = {row["id"]: row for row in api.get("/gsts").json()}[gst_id]
    assert (listed["address_count"], listed["account_count"]) == (1, 0)

    later = _address(api, "WH-1")
    assert api.patch(f"/addresses/{later}", json={"gst_id": gst_id}).status_code == 200
    account_ids = _accounts(db, 1)
    assert api.patch(f"/accounts/{account_ids[0]}", json={"gst_id": gst_id}).status_code == 200

    assert api.delete(f"/gsts/{gst_id}").json() == {"unlinked_addresses": 2, "unlinked_accounts": 1}


def test_an_unused_gst_unlinks_nothing(api):
    gst_id = _gst(api)

    response = api.delete(f"/gsts/{gst_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {"unlinked_addresses": 0, "unlinked_accounts": 0}
    assert api.delete(f"/gsts/{gst_id}").status_code == 404
