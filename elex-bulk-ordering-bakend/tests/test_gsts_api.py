"""Tests for the GSTs API (Phase 8, ROADMAP §2).

The load-bearing test is ``test_delete_unlinks_addresses_and_deletes_none``: a
GST is a registration an address bills under, not its owner, so removing one must
empty the column on its addresses and lose no address row.

After that come the two invariants the ordering run depends on — exactly one
default (enforced in SQL, asserted against the table, not the response) and a
GSTIN that is really 15 characters, because a wrong one bills the wrong entity
on an order that otherwise succeeds.

Both routers are mounted on a local FastAPI app (``app/main.py`` is orchestrator
owned); the auth dependency and the DB are the real ones.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from app.api import addresses as addresses_api
from app.api import gsts as gsts_api
from app.core.security import create_access_token
from app.db.base import SessionLocal, engine
from app.db.models import Address, Gst

GSTIN_A = "27AAAAA0000A1Z5"
GSTIN_B = "29BBBBB1111B2Z6"


# --- fixtures -----------------------------------------------------------------
@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(gsts_api.router)
    application.include_router(addresses_api.router)
    return application


@pytest.fixture
def api(api_app: FastAPI) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anon(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


def _gst_body(label: str = "AARTHBODHA", **overrides) -> dict:
    body = {
        "label": label,
        "gstin": GSTIN_A,
        "business_name": "Aarthbodha Pvt Ltd",
    }
    body.update(overrides)
    return body


def _address_body(label: str, gst_id: int | None = None, **overrides) -> dict:
    body = {
        "label": label,
        "name": "Warehouse",
        "line1": "12 MG Road",
        "pincode": "560001",
    }
    if gst_id is not None:
        body["gst_id"] = gst_id
    body.update(overrides)
    return body


@pytest.fixture
def entities(api: TestClient) -> dict:
    """Two GSTs; three addresses on the first, one on the second."""
    first = api.post("/gsts", json=_gst_body("AARTHBODHA")).json()
    second = api.post(
        "/gsts", json=_gst_body("BLAZECOM", gstin=GSTIN_B, business_name="Blazecom LLP")
    ).json()
    on_first = [
        api.post("/addresses", json=_address_body(f"AB-{i}", first["id"])).json()["id"]
        for i in range(3)
    ]
    on_second = [api.post("/addresses", json=_address_body("BC-0", second["id"])).json()["id"]]
    loose = [api.post("/addresses", json=_address_body("LOOSE")).json()["id"]]
    return {
        "first": first["id"],
        "second": second["id"],
        "on_first": on_first,
        "on_second": on_second,
        "loose": loose,
    }


# --- direct DB reads ----------------------------------------------------------
def _defaults() -> list[int]:
    """The ids of every GST row flagged default, read straight from the table."""
    with SessionLocal() as session:
        return [
            int(gst_id)
            for gst_id in session.scalars(
                select(Gst.id).where(Gst.is_default.is_(True))
            ).all()
        ]


def _row_counts() -> tuple[int, int]:
    """(gsts, addresses) straight from the DB."""
    with SessionLocal() as session:
        return (
            int(session.scalar(select(func.count()).select_from(Gst)) or 0),
            int(session.scalar(select(func.count()).select_from(Address)) or 0),
        )


@contextmanager
def _captured_sql() -> Iterator[list[str]]:
    """Every statement the engine runs inside the block.

    The only convincing proof that unlinking is a bulk UPDATE, and that the
    default rule is not a per-row loop, is that the statement count stays flat
    as the number of rows grows.
    """
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _verbs(statements: list[str], verb: str) -> list[str]:
    return [s for s in statements if s.lstrip().upper().startswith(verb)]


def _links() -> dict[int, int | None]:
    with SessionLocal() as session:
        return {
            int(address_id): (None if gst_id is None else int(gst_id))
            for address_id, gst_id in session.execute(
                select(Address.id, Address.gst_id)
            ).all()
        }


# --- auth ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/gsts"),
        ("post", "/gsts"),
        ("patch", "/gsts/1"),
        ("delete", "/gsts/1"),
    ],
)
def test_every_route_needs_a_token(anon: TestClient, method: str, path: str) -> None:
    # 401, never 403: "not signed in" is one answer, "not allowed" another.
    assert getattr(anon, method)(path).status_code == 401


def test_a_bad_token_is_rejected(api_app: FastAPI) -> None:
    client = TestClient(api_app, headers={"Authorization": "Bearer not-a-jwt"})
    assert client.get("/gsts").status_code == 401


# --- create -------------------------------------------------------------------
def test_create_a_gst(api: TestClient) -> None:
    response = api.post("/gsts", json=_gst_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["label"] == "AARTHBODHA"
    assert body["gstin"] == GSTIN_A
    assert body["business_name"] == "Aarthbodha Pvt Ltd"
    assert body["is_default"] is False
    assert body["address_count"] == 0  # fresh GST, nothing billing under it yet
    assert isinstance(body["id"], int)
    assert body["created_at"]


def test_create_trims_whitespace(api: TestClient) -> None:
    body = api.post(
        "/gsts",
        json=_gst_body("  AARTHBODHA  ", gstin=f" {GSTIN_A} ", business_name=" Ltd "),
    ).json()
    assert body["label"] == "AARTHBODHA"
    assert body["gstin"] == GSTIN_A
    assert body["business_name"] == "Ltd"


def test_a_duplicate_label_is_409(api: TestClient) -> None:
    assert api.post("/gsts", json=_gst_body()).status_code == 201
    assert api.post("/gsts", json=_gst_body()).status_code == 409
    # The whitespace-trimmed form is the same label, not a second GST.
    assert api.post("/gsts", json=_gst_body(" AARTHBODHA ")).status_code == 409
    assert len(api.get("/gsts").json()) == 1


@pytest.mark.parametrize("field", ["label", "gstin", "business_name"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_required_field_is_422(api: TestClient, field: str, blank: str) -> None:
    assert api.post("/gsts", json=_gst_body(**{field: blank})).status_code == 422
    assert api.get("/gsts").json() == []


@pytest.mark.parametrize(
    "gstin",
    ["27AAAAA0000A1Z", "27AAAAA0000A1Z56", "", "27AAAAA0000A1Z5 extra"],
)
def test_a_gstin_of_the_wrong_length_is_422(api: TestClient, gstin: str) -> None:
    assert api.post("/gsts", json=_gst_body(gstin=gstin)).status_code == 422
    assert api.get("/gsts").json() == []


def test_missing_oversized_and_unknown_fields_are_422(api: TestClient) -> None:
    assert api.post("/gsts", json={}).status_code == 422
    assert api.post("/gsts", json=_gst_body("x" * 65)).status_code == 422
    assert api.post("/gsts", json=_gst_body(business_name="b" * 256)).status_code == 422
    assert api.post("/gsts", json=_gst_body(colour="red")).status_code == 422


# --- list ---------------------------------------------------------------------
def test_list_is_empty_to_start(api: TestClient) -> None:
    assert api.get("/gsts").json() == []


def test_list_is_alphabetical_with_address_counts(api: TestClient, entities: dict) -> None:
    body = api.get("/gsts").json()
    assert [g["label"] for g in body] == ["AARTHBODHA", "BLAZECOM"]
    counts = {g["label"]: g["address_count"] for g in body}
    assert counts == {"AARTHBODHA": 3, "BLAZECOM": 1}  # the loose address counts for neither


def test_a_new_gst_lists_with_a_zero_count(api: TestClient, entities: dict) -> None:
    api.post("/gsts", json=_gst_body("DIGIDRIVE", gstin=GSTIN_B))
    counts = {g["label"]: g["address_count"] for g in api.get("/gsts").json()}
    assert counts["DIGIDRIVE"] == 0


# --- patch --------------------------------------------------------------------
def test_patch_writes_only_the_fields_sent(api: TestClient) -> None:
    created = api.post("/gsts", json=_gst_body()).json()
    response = api.patch(f"/gsts/{created['id']}", json={"gstin": GSTIN_B})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gstin"] == GSTIN_B
    # The two fields we did not send are untouched.
    assert body["label"] == "AARTHBODHA"
    assert body["business_name"] == "Aarthbodha Pvt Ltd"


def test_patch_keeps_the_address_count(api: TestClient, entities: dict) -> None:
    body = api.patch(
        f"/gsts/{entities['first']}", json={"business_name": "Renamed Ltd"}
    ).json()
    assert body["business_name"] == "Renamed Ltd"
    assert body["address_count"] == 3


def test_renaming_onto_an_existing_label_is_409(api: TestClient, entities: dict) -> None:
    response = api.patch(f"/gsts/{entities['first']}", json={"label": "BLAZECOM"})
    assert response.status_code == 409
    assert api.get("/gsts").json()[0]["label"] == "AARTHBODHA"  # unchanged


def test_renaming_a_gst_to_its_own_label_is_fine(api: TestClient, entities: dict) -> None:
    response = api.patch(f"/gsts/{entities['first']}", json={"label": "AARTHBODHA"})
    assert response.status_code == 200
    assert response.json()["label"] == "AARTHBODHA"


@pytest.mark.parametrize(
    "body",
    [
        {"label": "  "},
        {"label": None},
        {"gstin": "too-short"},
        {"gstin": None},
        {"business_name": ""},
        {"label": "x" * 65},
        {"colour": "red"},
    ],
)
def test_patch_rejects_bad_values(api: TestClient, body: dict) -> None:
    created = api.post("/gsts", json=_gst_body()).json()
    assert api.patch(f"/gsts/{created['id']}", json=body).status_code == 422
    # Nothing was written.
    after = api.get("/gsts").json()[0]
    assert after["label"] == "AARTHBODHA"
    assert after["gstin"] == GSTIN_A


def test_patching_a_missing_gst_is_404(api: TestClient) -> None:
    assert api.patch("/gsts/999", json={"label": "Nope"}).status_code == 404


# --- exactly one default ------------------------------------------------------
def test_creating_a_default_clears_it_everywhere_else(api: TestClient) -> None:
    first = api.post("/gsts", json=_gst_body("AARTHBODHA", is_default=True)).json()
    assert first["is_default"] is True
    assert _defaults() == [first["id"]]

    second = api.post(
        "/gsts", json=_gst_body("BLAZECOM", gstin=GSTIN_B, is_default=True)
    ).json()
    assert second["is_default"] is True
    # Exactly one row holds it, and it is the new one.
    assert _defaults() == [second["id"]]
    assert [g["is_default"] for g in api.get("/gsts").json()] == [False, True]


def test_patching_a_default_clears_it_everywhere_else(api: TestClient, entities: dict) -> None:
    api.patch(f"/gsts/{entities['first']}", json={"is_default": True})
    assert _defaults() == [entities["first"]]

    response = api.patch(f"/gsts/{entities['second']}", json={"is_default": True})
    assert response.status_code == 200
    assert response.json()["is_default"] is True
    assert _defaults() == [entities["second"]]


def test_a_default_can_be_cleared_leaving_none(api: TestClient, entities: dict) -> None:
    api.patch(f"/gsts/{entities['first']}", json={"is_default": True})
    api.patch(f"/gsts/{entities['first']}", json={"is_default": False})
    assert _defaults() == []  # no row is promoted in its place


def test_patching_other_fields_leaves_the_default_alone(api: TestClient, entities: dict) -> None:
    api.patch(f"/gsts/{entities['first']}", json={"is_default": True})
    api.patch(f"/gsts/{entities['second']}", json={"business_name": "Blazecom Ltd"})
    assert _defaults() == [entities["first"]]


@pytest.mark.parametrize("others", [1, 12])
def test_create_clears_the_other_defaults_in_one_update(api: TestClient, others: int) -> None:
    """The clear costs one UPDATE whether there is 1 other row or 12."""
    for i in range(others):
        api.post(
            "/gsts", json=_gst_body(f"OTHER-{i:02d}", is_default=True)
        )

    with _captured_sql() as statements:
        created = api.post("/gsts", json=_gst_body("TARGET", is_default=True))

    assert created.status_code == 201, created.text
    assert len(_verbs(statements, "INSERT")) == 1
    assert len(_verbs(statements, "UPDATE")) == 1, statements  # just the bulk clear
    assert _defaults() == [created.json()["id"]]


@pytest.mark.parametrize("others", [1, 12])
def test_patch_clears_the_other_defaults_in_one_update(api: TestClient, others: int) -> None:
    target = api.post("/gsts", json=_gst_body("TARGET")).json()
    for i in range(others):
        api.post("/gsts", json=_gst_body(f"OTHER-{i:02d}", is_default=True))

    with _captured_sql() as statements:
        response = api.patch(f"/gsts/{target['id']}", json={"is_default": True})

    assert response.status_code == 200, response.text
    # One ORM UPDATE for the row itself, one bulk clear for the rest — and the
    # same two however many rows there are, so nothing is looping.
    assert len(_verbs(statements, "UPDATE")) == 2, statements
    assert _defaults() == [target["id"]]


# --- delete -------------------------------------------------------------------
def test_delete_unlinks_addresses_and_deletes_none(api: TestClient, entities: dict) -> None:
    gsts_before, addresses_before = _row_counts()

    response = api.delete(f"/gsts/{entities['first']}")
    assert response.status_code == 200
    # What it unlinked, for the page's note (review of 27 Oct 2026).
    assert response.json() == {"unlinked_addresses": 3, "unlinked_accounts": 0}

    gsts_after, addresses_after = _row_counts()
    assert addresses_after == addresses_before  # no address was harmed
    assert gsts_after == gsts_before - 1

    links = _links()
    assert all(links[i] is None for i in entities["on_first"])
    # The other GST and its addresses are untouched.
    assert all(links[i] == entities["second"] for i in entities["on_second"])
    assert [g["label"] for g in api.get("/gsts").json()] == ["BLAZECOM"]


def test_the_unlinked_addresses_still_list_with_a_null_gst(api: TestClient, entities: dict) -> None:
    api.delete(f"/gsts/{entities['first']}")
    by_id = {a["id"]: a for a in api.get("/addresses").json()}
    assert len(by_id) == 5
    assert all(by_id[i]["gst"] is None for i in entities["on_first"])
    assert by_id[entities["on_second"][0]]["gst"]["label"] == "BLAZECOM"


@pytest.mark.parametrize("linked", [1, 12])
def test_delete_unlinks_every_address_in_one_update(api: TestClient, linked: int) -> None:
    gst = api.post("/gsts", json=_gst_body()).json()
    for i in range(linked):
        api.post("/addresses", json=_address_body(f"A-{i:02d}", gst["id"]))

    with _captured_sql() as statements:
        assert api.delete(f"/gsts/{gst['id']}").status_code == 200

    # One UPDATE per table that can name a GST (the addresses' unlink, then the
    # accounts' own GSTs) and one DELETE (the GST row) — never one per
    # address, and never a SELECT of every linked address to de-associate it.
    updates = _verbs(statements, "UPDATE")
    assert [statement.split()[1] for statement in updates] == ["addresses", "accounts"], statements
    assert len(_verbs(statements, "DELETE")) == 1, statements
    assert _row_counts() == (0, linked)  # every address survived
    assert set(_links().values()) == {None}


def test_deleting_an_unused_gst_works(api: TestClient) -> None:
    gst_id = api.post("/gsts", json=_gst_body()).json()["id"]
    assert api.delete(f"/gsts/{gst_id}").status_code == 200
    assert api.get("/gsts").json() == []


def test_deleting_a_missing_gst_is_404(api: TestClient) -> None:
    assert api.delete("/gsts/999").status_code == 404


def test_the_label_is_free_again_after_a_delete(api: TestClient, entities: dict) -> None:
    api.delete(f"/gsts/{entities['first']}")
    assert api.post("/gsts", json=_gst_body("AARTHBODHA")).status_code == 201


def test_deleting_the_default_leaves_no_default(api: TestClient, entities: dict) -> None:
    api.patch(f"/gsts/{entities['first']}", json={"is_default": True})
    api.delete(f"/gsts/{entities['first']}")
    assert _defaults() == []


# --- default_gst --------------------------------------------------------------
def test_default_gst_returns_the_flagged_row(api: TestClient, entities: dict) -> None:
    api.patch(f"/gsts/{entities['second']}", json={"is_default": True})
    with SessionLocal() as session:
        found = gsts_api.default_gst(session)
    assert found is not None
    assert found.id == entities["second"]
    assert found.gstin == GSTIN_B


def test_default_gst_is_none_when_nothing_is_flagged(api: TestClient, entities: dict) -> None:
    with SessionLocal() as session:
        assert gsts_api.default_gst(session) is None


def test_default_gst_is_none_on_an_empty_table() -> None:
    with SessionLocal() as session:
        assert gsts_api.default_gst(session) is None
