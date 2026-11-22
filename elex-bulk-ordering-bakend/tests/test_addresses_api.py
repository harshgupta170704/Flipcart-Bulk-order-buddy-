"""Tests for the addresses API (Phase 8, ROADMAP §2).

What these guard is a *quiet* failure: an order with a wrong address succeeds and
ships to the wrong place. So the sharp tests are the ones about not writing
something half-right — an unknown ``gst_id`` writes nothing at all, a PATCH of
one field cannot blank another by omission, and a six-digit pincode is the only
pincode.

The default flag is asserted against the table rather than the response, because
"exactly one default" is a claim about the whole table and it is enforced with a
bulk UPDATE the response would not reveal.

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
from app.db.models import Address

GSTIN = "27AAAAA0000A1Z5"


# --- fixtures -----------------------------------------------------------------
@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(addresses_api.router)
    application.include_router(gsts_api.router)
    return application


@pytest.fixture
def api(api_app: FastAPI) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anon(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


def _body(label: str = "WAREHOUSE", **overrides) -> dict:
    body = {
        "label": label,
        "name": "Aarthbodha Warehouse",
        "phone": "9876543210",
        "line1": "12 MG Road, Indiranagar",
        "floor": "2nd Floor",
        "city": "Bengaluru",
        "state": "Karnataka",
        "pincode": "560001",
    }
    body.update(overrides)
    return body


@pytest.fixture
def gst_id(api: TestClient) -> int:
    created = api.post(
        "/gsts",
        json={
            "label": "AARTHBODHA",
            "gstin": GSTIN,
            "business_name": "Aarthbodha Pvt Ltd",
        },
    )
    assert created.status_code == 201, created.text
    return int(created.json()["id"])


# --- direct DB reads ----------------------------------------------------------
def _defaults() -> list[int]:
    """The ids of every address row flagged default, read from the table."""
    with SessionLocal() as session:
        return [
            int(address_id)
            for address_id in session.scalars(
                select(Address.id).where(Address.is_default.is_(True))
            ).all()
        ]


def _count() -> int:
    with SessionLocal() as session:
        return int(session.scalar(select(func.count()).select_from(Address)) or 0)


@contextmanager
def _captured_sql() -> Iterator[list[str]]:
    """Every statement the engine runs inside the block.

    Used to prove the "exactly one default" rule is a bulk UPDATE: the only
    convincing assertion is that the statement count does not grow with the
    number of rows, which is what a per-row loop would do.
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


# --- auth ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/addresses"),
        ("post", "/addresses"),
        ("patch", "/addresses/1"),
        ("delete", "/addresses/1"),
    ],
)
def test_every_route_needs_a_token(anon: TestClient, method: str, path: str) -> None:
    assert getattr(anon, method)(path).status_code in (401, 403)


def test_a_bad_token_is_rejected(api_app: FastAPI) -> None:
    client = TestClient(api_app, headers={"Authorization": "Bearer not-a-jwt"})
    assert client.get("/addresses").status_code == 401


# --- create -------------------------------------------------------------------
def test_create_an_address(api: TestClient) -> None:
    response = api.post("/addresses", json=_body())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["label"] == "WAREHOUSE"
    assert body["name"] == "Aarthbodha Warehouse"
    assert body["phone"] == "9876543210"
    assert body["line1"] == "12 MG Road, Indiranagar"
    assert body["floor"] == "2nd Floor"
    assert body["city"] == "Bengaluru"
    assert body["state"] == "Karnataka"
    assert body["pincode"] == "560001"
    assert body["is_default"] is False
    assert body["gst"] is None
    assert isinstance(body["id"], int)
    assert body["created_at"]


def test_create_with_only_the_required_fields(api: TestClient) -> None:
    response = api.post(
        "/addresses",
        json={"label": "MIN", "name": "Min", "line1": "1 Road", "pincode": "110001"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["phone"], body["floor"], body["city"], body["state"]) == (
        None,
        None,
        None,
        None,
    )


def test_create_links_a_gst_and_inlines_it(api: TestClient, gst_id: int) -> None:
    body = api.post("/addresses", json=_body(gst_id=gst_id)).json()
    assert body["gst"] == {
        "id": gst_id,
        "label": "AARTHBODHA",
        "gstin": GSTIN,
        "business_name": "Aarthbodha Pvt Ltd",
    }


def test_create_trims_whitespace_and_blanks_become_null(api: TestClient) -> None:
    body = api.post(
        "/addresses",
        json=_body("  WAREHOUSE  ", pincode=" 560001 ", floor="   ", phone=""),
    ).json()
    assert body["label"] == "WAREHOUSE"
    assert body["pincode"] == "560001"
    # A blank optional field means "not given", not an empty string.
    assert body["floor"] is None
    assert body["phone"] is None


def test_a_duplicate_label_is_409(api: TestClient) -> None:
    assert api.post("/addresses", json=_body()).status_code == 201
    assert api.post("/addresses", json=_body()).status_code == 409
    assert api.post("/addresses", json=_body(" WAREHOUSE ")).status_code == 409
    assert len(api.get("/addresses").json()) == 1


@pytest.mark.parametrize("field", ["label", "name", "line1"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_required_field_is_422(api: TestClient, field: str, blank: str) -> None:
    assert api.post("/addresses", json=_body(**{field: blank})).status_code == 422
    assert _count() == 0


@pytest.mark.parametrize(
    "pincode", ["", "   ", "12345", "1234567", "abcdef", "56000a", "56 001"]
)
def test_a_pincode_that_is_not_six_digits_is_422(api: TestClient, pincode: str) -> None:
    assert api.post("/addresses", json=_body(pincode=pincode)).status_code == 422
    assert _count() == 0


def test_missing_oversized_and_unknown_fields_are_422(api: TestClient) -> None:
    assert api.post("/addresses", json={}).status_code == 422
    assert api.post("/addresses", json=_body("x" * 65)).status_code == 422
    assert api.post("/addresses", json=_body(name="n" * 256)).status_code == 422
    assert api.post("/addresses", json=_body(phone="9" * 33)).status_code == 422
    assert api.post("/addresses", json=_body(city="c" * 129)).status_code == 422
    assert api.post("/addresses", json=_body(colour="red")).status_code == 422
    assert _count() == 0


def test_an_unknown_gst_id_is_422_and_writes_nothing(api: TestClient) -> None:
    response = api.post("/addresses", json=_body(gst_id=999))
    assert response.status_code == 422, response.text
    assert _count() == 0
    assert api.get("/addresses").json() == []


# --- list ---------------------------------------------------------------------
def test_list_is_empty_to_start(api: TestClient) -> None:
    assert api.get("/addresses").json() == []


def test_list_is_alphabetical_by_label(api: TestClient, gst_id: int) -> None:
    for label in ("ZEBRA", "ALPHA", "MIKE"):
        api.post("/addresses", json=_body(label, gst_id=gst_id))
    assert [a["label"] for a in api.get("/addresses").json()] == [
        "ALPHA",
        "MIKE",
        "ZEBRA",
    ]


def test_list_inlines_each_gst(api: TestClient, gst_id: int) -> None:
    api.post("/addresses", json=_body("LINKED", gst_id=gst_id))
    api.post("/addresses", json=_body("LOOSE"))
    by_label = {a["label"]: a for a in api.get("/addresses").json()}
    assert by_label["LINKED"]["gst"]["id"] == gst_id
    assert by_label["LOOSE"]["gst"] is None


# --- patch --------------------------------------------------------------------
def test_patch_writes_only_the_fields_sent(api: TestClient) -> None:
    created = api.post("/addresses", json=_body()).json()
    response = api.patch(f"/addresses/{created['id']}", json={"phone": "9000000000"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["phone"] == "9000000000"
    # Every field we did not send is unchanged — a PATCH of phone must not blank
    # floor by omission.
    assert body["floor"] == "2nd Floor"
    assert body["city"] == "Bengaluru"
    assert body["state"] == "Karnataka"
    assert body["line1"] == "12 MG Road, Indiranagar"
    assert body["pincode"] == "560001"
    assert body["label"] == "WAREHOUSE"
    assert body["name"] == "Aarthbodha Warehouse"


def test_patch_can_clear_a_nullable_field(api: TestClient) -> None:
    created = api.post("/addresses", json=_body()).json()
    body = api.patch(f"/addresses/{created['id']}", json={"floor": None}).json()
    assert body["floor"] is None
    assert body["city"] == "Bengaluru"  # and only that field


def test_patch_links_and_unlinks_a_gst(api: TestClient, gst_id: int) -> None:
    created = api.post("/addresses", json=_body()).json()
    linked = api.patch(f"/addresses/{created['id']}", json={"gst_id": gst_id}).json()
    assert linked["gst"]["id"] == gst_id

    unlinked = api.patch(f"/addresses/{created['id']}", json={"gst_id": None}).json()
    assert unlinked["gst"] is None
    assert unlinked["label"] == "WAREHOUSE"  # the row itself survives


def test_patching_to_an_unknown_gst_id_is_422_and_writes_nothing(api: TestClient) -> None:
    created = api.post("/addresses", json=_body()).json()
    response = api.patch(
        f"/addresses/{created['id']}", json={"gst_id": 999, "city": "Delhi"}
    )
    assert response.status_code == 422
    after = api.get("/addresses").json()[0]
    assert after["gst"] is None
    assert after["city"] == "Bengaluru"  # the city in the same body was not written


def test_renaming_onto_an_existing_label_is_409(api: TestClient) -> None:
    first = api.post("/addresses", json=_body("WAREHOUSE")).json()
    api.post("/addresses", json=_body("OFFICE"))
    response = api.patch(f"/addresses/{first['id']}", json={"label": "OFFICE"})
    assert response.status_code == 409
    by_id = {a["id"]: a for a in api.get("/addresses").json()}
    assert by_id[first["id"]]["label"] == "WAREHOUSE"  # unchanged


def test_renaming_an_address_to_its_own_label_is_fine(api: TestClient) -> None:
    created = api.post("/addresses", json=_body()).json()
    response = api.patch(f"/addresses/{created['id']}", json={"label": "WAREHOUSE"})
    assert response.status_code == 200
    assert response.json()["label"] == "WAREHOUSE"


@pytest.mark.parametrize(
    "body",
    [
        {"label": "  "},
        {"label": None},
        {"name": ""},
        {"name": None},
        {"line1": None},
        {"pincode": "12345"},
        {"pincode": "abcdef"},
        {"pincode": None},
        {"label": "x" * 65},
        {"colour": "red"},
    ],
)
def test_patch_rejects_bad_values(api: TestClient, body: dict) -> None:
    created = api.post("/addresses", json=_body()).json()
    assert api.patch(f"/addresses/{created['id']}", json=body).status_code == 422
    after = api.get("/addresses").json()[0]
    assert after["label"] == "WAREHOUSE"
    assert after["pincode"] == "560001"


def test_patching_a_missing_address_is_404(api: TestClient) -> None:
    assert api.patch("/addresses/999", json={"city": "Delhi"}).status_code == 404


# --- exactly one default ------------------------------------------------------
def test_creating_a_default_clears_it_everywhere_else(api: TestClient) -> None:
    first = api.post("/addresses", json=_body("WAREHOUSE", is_default=True)).json()
    assert first["is_default"] is True
    assert _defaults() == [first["id"]]

    second = api.post("/addresses", json=_body("OFFICE", is_default=True)).json()
    assert second["is_default"] is True
    # Exactly one row holds it, and it is the new one.
    assert _defaults() == [second["id"]]
    assert {a["label"]: a["is_default"] for a in api.get("/addresses").json()} == {
        "OFFICE": True,
        "WAREHOUSE": False,
    }


def test_patching_a_default_clears_it_everywhere_else(api: TestClient) -> None:
    first = api.post("/addresses", json=_body("WAREHOUSE", is_default=True)).json()
    second = api.post("/addresses", json=_body("OFFICE")).json()

    response = api.patch(f"/addresses/{second['id']}", json={"is_default": True})
    assert response.status_code == 200
    assert response.json()["is_default"] is True
    assert _defaults() == [second["id"]]
    assert first["id"] not in _defaults()


def test_a_default_can_be_cleared_leaving_none(api: TestClient) -> None:
    created = api.post("/addresses", json=_body(is_default=True)).json()
    api.patch(f"/addresses/{created['id']}", json={"is_default": False})
    assert _defaults() == []  # no row is promoted in its place


def test_patching_other_fields_leaves_the_default_alone(api: TestClient) -> None:
    first = api.post("/addresses", json=_body("WAREHOUSE", is_default=True)).json()
    second = api.post("/addresses", json=_body("OFFICE")).json()
    api.patch(f"/addresses/{second['id']}", json={"city": "Delhi"})
    assert _defaults() == [first["id"]]


@pytest.mark.parametrize("others", [1, 12])
def test_create_clears_the_other_defaults_in_one_update(api: TestClient, others: int) -> None:
    """The clear costs one UPDATE whether there is 1 other row or 12."""
    for i in range(others):
        api.post("/addresses", json=_body(f"OTHER-{i:02d}", is_default=True))

    with _captured_sql() as statements:
        created = api.post("/addresses", json=_body("TARGET", is_default=True))

    assert created.status_code == 201, created.text
    assert len(_verbs(statements, "INSERT")) == 1
    assert len(_verbs(statements, "UPDATE")) == 1, statements  # just the bulk clear
    assert _defaults() == [created.json()["id"]]


@pytest.mark.parametrize("others", [1, 12])
def test_patch_clears_the_other_defaults_in_one_update(api: TestClient, others: int) -> None:
    target = api.post("/addresses", json=_body("TARGET")).json()
    for i in range(others):
        api.post("/addresses", json=_body(f"OTHER-{i:02d}", is_default=True))

    with _captured_sql() as statements:
        response = api.patch(f"/addresses/{target['id']}", json={"is_default": True})

    assert response.status_code == 200, response.text
    # One ORM UPDATE for the row itself, one bulk clear for the rest — and the
    # same two however many rows there are, so nothing is looping.
    assert len(_verbs(statements, "UPDATE")) == 2, statements
    assert _defaults() == [target["id"]]


# --- delete -------------------------------------------------------------------
def test_delete_an_address(api: TestClient) -> None:
    created = api.post("/addresses", json=_body()).json()
    response = api.delete(f"/addresses/{created['id']}")
    assert response.status_code == 204
    assert response.content == b""
    assert api.get("/addresses").json() == []


def test_deleting_an_address_leaves_its_gst_alone(api: TestClient, gst_id: int) -> None:
    created = api.post("/addresses", json=_body(gst_id=gst_id)).json()
    api.delete(f"/addresses/{created['id']}")
    listing = api.get("/gsts").json()
    assert [g["id"] for g in listing] == [gst_id]
    assert listing[0]["address_count"] == 0


def test_deleting_a_missing_address_is_404(api: TestClient) -> None:
    assert api.delete("/addresses/999").status_code == 404


def test_the_label_is_free_again_after_a_delete(api: TestClient) -> None:
    created = api.post("/addresses", json=_body()).json()
    api.delete(f"/addresses/{created['id']}")
    assert api.post("/addresses", json=_body()).status_code == 201


def test_deleting_the_default_leaves_no_default(api: TestClient) -> None:
    created = api.post("/addresses", json=_body(is_default=True)).json()
    api.delete(f"/addresses/{created['id']}")
    assert _defaults() == []


# --- default_address ----------------------------------------------------------
def test_default_address_returns_the_flagged_row(api: TestClient, gst_id: int) -> None:
    api.post("/addresses", json=_body("WAREHOUSE", gst_id=gst_id))
    chosen = api.post("/addresses", json=_body("OFFICE", gst_id=gst_id, is_default=True)).json()

    with SessionLocal() as session:
        found = addresses_api.default_address(session)
        assert found is not None
        assert found.id == chosen["id"]
        assert found.pincode == "560001"
        # The GST travels with it — callers read address.gst from a session that
        # may already be closed, so it must be loaded here.
        assert found.gst is not None
        gst_label = found.gst.label
    assert gst_label == "AARTHBODHA"


def test_default_address_is_none_when_nothing_is_flagged(api: TestClient) -> None:
    api.post("/addresses", json=_body())
    with SessionLocal() as session:
        assert addresses_api.default_address(session) is None


def test_default_address_is_none_on_an_empty_table() -> None:
    with SessionLocal() as session:
        assert addresses_api.default_address(session) is None
