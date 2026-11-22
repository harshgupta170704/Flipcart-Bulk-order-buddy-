"""Tests for the tags API (Phase 7, ROADMAP §2).

The load-bearing test is ``test_delete_untags_accounts_and_deletes_none``: a tag
is a label, so removing it must empty the column on its accounts and lose
nothing. Everything else guards the invariants a filter depends on — unique
names, no blank names.

Both routers are mounted on a local FastAPI app (``app/main.py`` is orchestrator
owned); the auth dependency and the DB are the real ones.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api import accounts as accounts_api
from app.api import tags as tags_api
from app.core.security import create_access_token
from app.db.base import SessionLocal
from app.db.models import Account, AccountStatus, Tag


# --- fixtures -----------------------------------------------------------------
@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(tags_api.router)
    application.include_router(accounts_api.router)
    return application


@pytest.fixture
def api(api_app: FastAPI) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anon(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


@pytest.fixture
def tagged(db) -> dict:
    """Two tags over 30 accounts: 12 VIP, 8 BULK, 10 untagged."""
    vip, bulk = Tag(name="VIP"), Tag(name="BULK")
    db.add_all([vip, bulk])
    db.flush()

    accounts = []
    for i in range(30):
        tag = vip if i < 12 else (bulk if i < 20 else None)
        accounts.append(
            Account(
                email=f"t{i:03d}@test.com",
                phone=f"6{i:06d}",
                status=AccountStatus.new,
                tag=tag,
            )
        )
    db.add_all(accounts)
    db.commit()

    state = {
        "vip": vip.id,
        "bulk": bulk.id,
        "vip_accounts": [a.id for a in accounts[:12]],
        "bulk_accounts": [a.id for a in accounts[12:20]],
        "untagged_accounts": [a.id for a in accounts[20:]],
    }
    db.commit()  # release the read transaction before the API writes
    return state


# --- helpers ------------------------------------------------------------------
def _tag_state() -> dict[int, int | None]:
    with SessionLocal() as session:
        return {
            int(account_id): (None if tag_id is None else int(tag_id))
            for account_id, tag_id in session.execute(
                select(Account.id, Account.tag_id)
            ).all()
        }


def _counts() -> tuple[int, int]:
    """(accounts, tags) straight from the DB."""
    with SessionLocal() as session:
        return (
            int(session.scalar(select(func.count()).select_from(Account)) or 0),
            int(session.scalar(select(func.count()).select_from(Tag)) or 0),
        )


# --- auth ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/tags"),
        ("post", "/tags"),
        ("patch", "/tags/1"),
        ("delete", "/tags/1"),
    ],
)
def test_every_route_needs_a_token(anon: TestClient, method: str, path: str) -> None:
    assert getattr(anon, method)(path).status_code in (401, 403)


def test_a_bad_token_is_rejected(api_app: FastAPI) -> None:
    client = TestClient(api_app, headers={"Authorization": "Bearer not-a-jwt"})
    assert client.get("/tags").status_code == 401


# --- create -------------------------------------------------------------------
def test_create_a_tag(api) -> None:
    response = api.post("/tags", json={"name": "Verified"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Verified"
    assert body["account_count"] == 0
    assert isinstance(body["id"], int)


def test_create_trims_the_name(api) -> None:
    assert api.post("/tags", json={"name": "  Verified  "}).json()["name"] == "Verified"


def test_a_duplicate_name_is_409(api) -> None:
    assert api.post("/tags", json={"name": "Verified"}).status_code == 201
    duplicate = api.post("/tags", json={"name": "Verified"})
    assert duplicate.status_code == 409
    # And the whitespace-trimmed form is the same name, not a second tag.
    assert api.post("/tags", json={"name": " Verified "}).status_code == 409
    assert len(api.get("/tags").json()) == 1


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_a_blank_name_is_422(api, name: str) -> None:
    assert api.post("/tags", json={"name": name}).status_code == 422
    assert api.get("/tags").json() == []


def test_a_missing_or_oversized_name_is_422(api) -> None:
    assert api.post("/tags", json={}).status_code == 422
    assert api.post("/tags", json={"name": "x" * 65}).status_code == 422
    assert api.post("/tags", json={"name": "ok", "colour": "red"}).status_code == 422


# --- list ---------------------------------------------------------------------
def test_list_tags_with_account_counts(api, tagged) -> None:
    body = api.get("/tags").json()
    assert [t["name"] for t in body] == ["BULK", "VIP"]  # alphabetical
    counts = {t["name"]: t["account_count"] for t in body}
    assert counts == {"VIP": 12, "BULK": 8}


def test_list_is_empty_to_start(api) -> None:
    assert api.get("/tags").json() == []


def test_a_new_tag_lists_with_a_zero_count(api, tagged) -> None:
    api.post("/tags", json={"name": "Fresh"})
    body = {t["name"]: t["account_count"] for t in api.get("/tags").json()}
    assert body["Fresh"] == 0


# --- rename -------------------------------------------------------------------
def test_rename_a_tag_keeps_its_accounts(api, tagged) -> None:
    response = api.patch(f"/tags/{tagged['vip']}", json={"name": "Premium"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"id": tagged["vip"], "name": "Premium", "account_count": 12}
    # The accounts still point at the same tag id.
    state = _tag_state()
    assert all(state[i] == tagged["vip"] for i in tagged["vip_accounts"])


def test_renaming_onto_an_existing_name_is_409(api, tagged) -> None:
    response = api.patch(f"/tags/{tagged['vip']}", json={"name": "BULK"})
    assert response.status_code == 409
    assert api.get("/tags").json()[1]["name"] == "VIP"  # unchanged


def test_renaming_a_tag_to_its_own_name_is_fine(api, tagged) -> None:
    response = api.patch(f"/tags/{tagged['vip']}", json={"name": "VIP"})
    assert response.status_code == 200
    assert response.json()["name"] == "VIP"


def test_rename_rejects_a_blank_name(api, tagged) -> None:
    assert api.patch(f"/tags/{tagged['vip']}", json={"name": "  "}).status_code == 422


def test_renaming_a_missing_tag_is_404(api) -> None:
    assert api.patch("/tags/999", json={"name": "Nope"}).status_code == 404


# --- delete -------------------------------------------------------------------
def test_delete_untags_accounts_and_deletes_none(api, tagged) -> None:
    accounts_before, tags_before = _counts()

    response = api.delete(f"/tags/{tagged['vip']}")
    assert response.status_code == 204
    assert response.content == b""

    accounts_after, tags_after = _counts()
    assert accounts_after == accounts_before  # no account was harmed
    assert tags_after == tags_before - 1

    state = _tag_state()
    assert all(state[i] is None for i in tagged["vip_accounts"])
    # The other tag's accounts are untouched.
    assert all(state[i] == tagged["bulk"] for i in tagged["bulk_accounts"])
    assert [t["name"] for t in api.get("/tags").json()] == ["BULK"]


def test_deleted_tag_disappears_from_the_account_rows(api, tagged) -> None:
    api.delete(f"/tags/{tagged['vip']}")
    listing = api.get("/accounts", params={"size": 500}).json()
    by_id = {item["id"]: item for item in listing["items"]}
    assert all(by_id[i]["tag"] is None for i in tagged["vip_accounts"])
    assert api.get(
        "/accounts", params={"tag_id": tagged["vip"]}
    ).json()["total"] == 0
    assert api.get("/accounts", params={"untagged": "true"}).json()["total"] == 22


def test_deleting_an_unused_tag_works(api) -> None:
    tag_id = api.post("/tags", json={"name": "Fresh"}).json()["id"]
    assert api.delete(f"/tags/{tag_id}").status_code == 204
    assert api.get("/tags").json() == []


def test_deleting_a_missing_tag_is_404(api) -> None:
    assert api.delete("/tags/999").status_code == 404


def test_the_name_is_free_again_after_a_delete(api, tagged) -> None:
    api.delete(f"/tags/{tagged['vip']}")
    assert api.post("/tags", json={"name": "VIP"}).status_code == 201


# --- round trip with the accounts router --------------------------------------
def test_a_tag_created_here_can_tag_accounts_by_filter(api, tagged) -> None:
    tag_id = api.post("/tags", json={"name": "Fresh"}).json()["id"]
    response = api.post(
        "/accounts/tag", json={"tag_id": tag_id, "filter": {"include_untagged": True}}
    )
    assert response.json() == {"updated": len(tagged["untagged_accounts"])}
    assert {t["name"]: t["account_count"] for t in api.get("/tags").json()}["Fresh"] == 10
