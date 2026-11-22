"""Tests for the unit-limits API (Phase 9, ROADMAP §2).

The rules worth guarding are the ones that would otherwise fail quietly:

* a duplicate ``(scope, value)`` is a 409 **case-insensitively** — two rows
  that differ only in case both apply, so the operator would be shown one cap
  as two half-counts;
* ``max_units``/``window_days`` below 1 are 422 — a cap of zero units blocks
  every account and a zero-day window counts nothing, and both read as "the
  limit I set is not working";
* ``/check`` reports ``used``/``remaining``/``blocked``, which is what decides
  whether a run is worth starting at all.

The router is mounted on a local FastAPI app (``app/main.py`` is orchestrator
owned); the auth dependency and the DB are the real ones.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import unit_limits as unit_limits_api
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    LimitScope,
    Order,
    OrderStatus,
    Tag,
    UnitLimit,
    utcnow,
)


# --- fixtures -----------------------------------------------------------------
@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(unit_limits_api.router)
    return application


@pytest.fixture
def api(api_app: FastAPI) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anon(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


@pytest.fixture
def seeded(db) -> dict:
    """One account that has already bought 12 vivo handsets, 4 of them T5x 5G.

    Plus a second, untouched account under a tag, so a filtered check has
    something to narrow to.
    """
    tag = Tag(name="VIP")
    db.add(tag)
    db.flush()

    spent = Account(email="spent@test.com", phone="6000000001", status=AccountStatus.new)
    fresh = Account(
        email="fresh@test.com", phone="6000000002", status=AccountStatus.new, tag=tag
    )
    db.add_all([spent, fresh])
    db.flush()

    db.add_all(
        [
            Order(
                account_id=spent.id,
                qty=4,
                status=OrderStatus.placed,
                brand="vivo",
                category="Mobile",
                model="T5x 5G",
                placed_at=utcnow() - timedelta(days=2),
            ),
            Order(
                account_id=spent.id,
                qty=8,
                status=OrderStatus.placed,
                brand="VIVO",  # same brand, as the platform spelled it
                category="Mobile",
                model="Y28",
                placed_at=utcnow() - timedelta(days=3),
            ),
            # Outside every window, and a failure: neither may count.
            Order(
                account_id=spent.id,
                qty=50,
                status=OrderStatus.placed,
                brand="vivo",
                category="Mobile",
                model="T5x 5G",
                placed_at=utcnow() - timedelta(days=400),
            ),
            Order(
                account_id=spent.id,
                qty=50,
                status=OrderStatus.failed,
                brand="vivo",
                category="Mobile",
                model="T5x 5G",
                placed_at=utcnow() - timedelta(days=1),
            ),
        ]
    )
    limits = {
        "category": UnitLimit(
            scope=LimitScope.category, value="Mobile", max_units=20, window_days=31
        ),
        "brand": UnitLimit(
            scope=LimitScope.brand, value="vivo", max_units=16, window_days=31
        ),
        "model": UnitLimit(
            scope=LimitScope.model, value="T5x 5G", max_units=5, window_days=30
        ),
    }
    db.add_all(limits.values())
    db.commit()

    state = {
        "spent": spent.id,
        "fresh": fresh.id,
        "tag": tag.id,
        **{f"{name}_limit": limit.id for name, limit in limits.items()},
    }
    db.commit()  # release the read transaction before the API writes
    return state


def _handset(**extra) -> dict:
    """A check body for the seeded vivo T5x handset."""
    return {"brand": "vivo", "category": "Mobile", "model": "T5x 5G", **extra}


# --- auth ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/unit-limits"),
        ("post", "/unit-limits"),
        ("post", "/unit-limits/check"),
        ("patch", "/unit-limits/1"),
        ("delete", "/unit-limits/1"),
    ],
)
def test_every_route_needs_a_token(anon: TestClient, method: str, path: str) -> None:
    # 401, never 403: "not signed in" is one answer, "not allowed" another.
    assert getattr(anon, method)(path).status_code == 401


def test_a_bad_token_is_rejected(api_app: FastAPI) -> None:
    client = TestClient(api_app, headers={"Authorization": "Bearer not-a-jwt"})
    assert client.get("/unit-limits").status_code == 401


# --- create -------------------------------------------------------------------
def test_create_a_limit(api: TestClient) -> None:
    response = api.post(
        "/unit-limits",
        json={"scope": "category", "value": "Mobile", "max_units": 20, "window_days": 31},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["scope"] == "category"
    assert body["value"] == "Mobile"
    assert body["max_units"] == 20
    assert body["window_days"] == 31
    assert body["enabled"] is True
    assert isinstance(body["id"], int)
    assert body["created_at"]


def test_create_defaults_to_a_wildcard_and_thirty_days(api: TestClient) -> None:
    body = api.post("/unit-limits", json={"scope": "category", "max_units": 30}).json()
    assert body["value"] == ""  # every category
    assert body["window_days"] == 30
    assert body["enabled"] is True


def test_create_trims_the_value(api: TestClient) -> None:
    body = api.post(
        "/unit-limits", json={"scope": "brand", "value": "  vivo  ", "max_units": 16}
    ).json()
    assert body["value"] == "vivo"


def test_a_whitespace_value_is_the_wildcard(api: TestClient) -> None:
    body = api.post(
        "/unit-limits", json={"scope": "brand", "value": "   ", "max_units": 16}
    ).json()
    assert body["value"] == ""


def test_a_disabled_limit_can_be_created(api: TestClient) -> None:
    body = api.post(
        "/unit-limits",
        json={"scope": "brand", "value": "vivo", "max_units": 16, "enabled": False},
    ).json()
    assert body["enabled"] is False


def test_a_duplicate_pair_is_409(api: TestClient) -> None:
    rule = {"scope": "brand", "value": "vivo", "max_units": 16}
    assert api.post("/unit-limits", json=rule).status_code == 201
    assert api.post("/unit-limits", json={**rule, "max_units": 5}).status_code == 409
    assert len(api.get("/unit-limits").json()) == 1


@pytest.mark.parametrize("value", ["VIVO", "ViVo", "  vivo "])
def test_a_duplicate_is_detected_case_insensitively(api: TestClient, value: str) -> None:
    """Matching ignores case, so these would be one cap counted twice."""
    api.post("/unit-limits", json={"scope": "brand", "value": "vivo", "max_units": 16})
    duplicate = api.post(
        "/unit-limits", json={"scope": "brand", "value": value, "max_units": 16}
    )
    assert duplicate.status_code == 409, duplicate.text
    assert len(api.get("/unit-limits").json()) == 1


def test_two_wildcards_in_one_scope_are_409(api: TestClient) -> None:
    api.post("/unit-limits", json={"scope": "category", "max_units": 30})
    assert api.post("/unit-limits", json={"scope": "category", "max_units": 25}).status_code == 409


def test_the_same_value_in_another_scope_is_fine(api: TestClient) -> None:
    api.post("/unit-limits", json={"scope": "brand", "value": "vivo", "max_units": 16})
    second = api.post(
        "/unit-limits", json={"scope": "model", "value": "vivo", "max_units": 5}
    )
    assert second.status_code == 201
    assert len(api.get("/unit-limits").json()) == 2


@pytest.mark.parametrize("max_units", [0, -1, -99])
def test_a_max_units_below_one_is_422(api: TestClient, max_units: int) -> None:
    response = api.post(
        "/unit-limits", json={"scope": "brand", "value": "vivo", "max_units": max_units}
    )
    assert response.status_code == 422
    assert api.get("/unit-limits").json() == []


@pytest.mark.parametrize("window_days", [0, -5])
def test_a_window_below_one_day_is_422(api: TestClient, window_days: int) -> None:
    response = api.post(
        "/unit-limits",
        json={
            "scope": "brand",
            "value": "vivo",
            "max_units": 16,
            "window_days": window_days,
        },
    )
    assert response.status_code == 422
    assert api.get("/unit-limits").json() == []


@pytest.mark.parametrize(
    "body",
    [
        {},  # no scope, no max_units
        {"scope": "brand", "value": "vivo"},  # max_units is required
        {"scope": "colour", "max_units": 5},  # not a scope
        {"scope": "brand", "max_units": "lots"},
        {"scope": "brand", "max_units": 5, "value": "x" * 129},  # column width
        {"scope": "brand", "max_units": 5, "per_month": 3},  # typo => forbidden
    ],
)
def test_a_malformed_body_is_422(api: TestClient, body: dict) -> None:
    assert api.post("/unit-limits", json=body).status_code == 422


# --- list ---------------------------------------------------------------------
def test_list_is_empty_to_start(api: TestClient) -> None:
    assert api.get("/unit-limits").json() == []


def test_list_is_ordered_by_scope_then_value(api: TestClient, seeded: dict) -> None:
    api.post("/unit-limits", json={"scope": "brand", "value": "oppo", "max_units": 9})
    api.post("/unit-limits", json={"scope": "category", "max_units": 30})

    rows = api.get("/unit-limits").json()
    assert [(row["scope"], row["value"]) for row in rows] == [
        ("brand", "oppo"),
        ("brand", "vivo"),
        ("category", ""),
        ("category", "Mobile"),
        ("model", "T5x 5G"),
    ]


# --- patch --------------------------------------------------------------------
def test_patch_one_field_leaves_the_rest_alone(api: TestClient, seeded: dict) -> None:
    response = api.patch(f"/unit-limits/{seeded['brand_limit']}", json={"max_units": 12})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["max_units"] == 12
    assert (body["value"], body["window_days"], body["enabled"]) == ("vivo", 31, True)


def test_patch_can_disable_a_limit(api: TestClient, seeded: dict) -> None:
    body = api.patch(
        f"/unit-limits/{seeded['model_limit']}", json={"enabled": False}
    ).json()
    assert body["enabled"] is False
    # And it stops applying: only the category and brand caps are left.
    scopes = {row["scope"] for row in api.post("/unit-limits/check", json=_handset()).json()}
    assert scopes == {"category", "brand"}


def test_patch_can_rescope_a_limit(api: TestClient, seeded: dict) -> None:
    body = api.patch(
        f"/unit-limits/{seeded['model_limit']}",
        json={"scope": "brand", "value": "oppo"},
    ).json()
    assert (body["scope"], body["value"]) == ("brand", "oppo")


def test_patching_onto_an_existing_pair_is_409(api: TestClient, seeded: dict) -> None:
    response = api.patch(
        f"/unit-limits/{seeded['model_limit']}",
        json={"scope": "brand", "value": "VIVO"},
    )
    assert response.status_code == 409
    # Unchanged.
    rows = {row["id"]: row for row in api.get("/unit-limits").json()}
    assert rows[seeded["model_limit"]]["scope"] == "model"


def test_patching_a_limit_to_its_own_pair_is_fine(api: TestClient, seeded: dict) -> None:
    response = api.patch(
        f"/unit-limits/{seeded['brand_limit']}", json={"value": "vivo", "max_units": 18}
    )
    assert response.status_code == 200
    assert response.json()["max_units"] == 18


@pytest.mark.parametrize(
    "body",
    [{"max_units": 0}, {"window_days": 0}, {"scope": "colour"}, {"nope": 1}],
)
def test_a_malformed_patch_is_422(api: TestClient, seeded: dict, body: dict) -> None:
    assert api.patch(f"/unit-limits/{seeded['brand_limit']}", json=body).status_code == 422


def test_an_empty_patch_changes_nothing(api: TestClient, seeded: dict) -> None:
    before = {
        row["id"]: row
        for row in api.get("/unit-limits").json()
    }[seeded["brand_limit"]]
    assert api.patch(f"/unit-limits/{seeded['brand_limit']}", json={}).json() == before


def test_patching_a_missing_limit_is_404(api: TestClient) -> None:
    assert api.patch("/unit-limits/999", json={"max_units": 5}).status_code == 404


# --- delete -------------------------------------------------------------------
def test_delete_a_limit(api: TestClient, seeded: dict) -> None:
    response = api.delete(f"/unit-limits/{seeded['model_limit']}")
    assert response.status_code == 204
    assert response.content == b""
    assert seeded["model_limit"] not in {row["id"] for row in api.get("/unit-limits").json()}


def test_deleting_a_missing_limit_is_404(api: TestClient) -> None:
    assert api.delete("/unit-limits/999").status_code == 404


def test_the_pair_is_free_again_after_a_delete(api: TestClient, seeded: dict) -> None:
    api.delete(f"/unit-limits/{seeded['brand_limit']}")
    assert api.post(
        "/unit-limits", json={"scope": "brand", "value": "vivo", "max_units": 16}
    ).status_code == 201


# --- check --------------------------------------------------------------------
def test_check_reports_used_remaining_and_blocked(api: TestClient, seeded: dict) -> None:
    response = api.post(
        "/unit-limits/check", json=_handset(account_ids=[seeded["spent"]])
    )
    assert response.status_code == 200, response.text
    rows = {row["scope"]: row for row in response.json()}
    assert set(rows) == {"category", "brand", "model"}

    # 12 Mobile units in the last 31 days (the 400-day-old and failed rows do not count).
    assert rows["category"] == {
        "limit_id": seeded["category_limit"],
        "scope": "category",
        "value": "Mobile",
        "max_units": 20,
        "window_days": 31,
        "used": 12,
        "remaining": 8,
        "blocked": False,
    }
    # Both vivo rows count despite the "VIVO" spelling.
    assert (rows["brand"]["used"], rows["brand"]["remaining"]) == (12, 4)
    # Only the 4 T5x units, against a cap of 5.
    assert (rows["model"]["used"], rows["model"]["remaining"]) == (4, 1)
    assert not any(row["blocked"] for row in rows.values())


def test_check_reports_blocked_when_a_cap_is_spent(api: TestClient, seeded: dict) -> None:
    api.patch(f"/unit-limits/{seeded['model_limit']}", json={"max_units": 4})
    rows = {
        row["scope"]: row
        for row in api.post(
            "/unit-limits/check", json=_handset(account_ids=[seeded["spent"]])
        ).json()
    }
    assert rows["model"]["remaining"] == 0
    assert rows["model"]["blocked"] is True
    assert rows["brand"]["blocked"] is False


def test_check_on_an_untouched_account_is_the_full_cap(
    api: TestClient, seeded: dict
) -> None:
    rows = {
        row["scope"]: row
        for row in api.post(
            "/unit-limits/check", json=_handset(account_ids=[seeded["fresh"]])
        ).json()
    }
    assert [rows[scope]["used"] for scope in ("category", "brand", "model")] == [0, 0, 0]
    assert rows["model"]["remaining"] == 5
    assert rows["brand"]["remaining"] == 16


def test_check_accepts_an_account_filter(api: TestClient, seeded: dict) -> None:
    """The filter resolves server-side, so a check covers the rows the operator saw."""
    rows = {
        row["scope"]: row
        for row in api.post(
            "/unit-limits/check", json=_handset(filter={"tag_ids": [seeded["tag"]]})
        ).json()
    }
    # The tag holds only the untouched account.
    assert rows["model"]["used"] == 0
    assert rows["model"]["remaining"] == 5


def test_check_over_every_account_totals_the_selection(
    api: TestClient, seeded: dict
) -> None:
    """No selection means every account, so remaining is what the run can absorb."""
    rows = {
        row["scope"]: row for row in api.post("/unit-limits/check", json=_handset()).json()
    }
    assert rows["model"]["used"] == 4
    assert rows["model"]["remaining"] == 1 + 5  # spent account + untouched one
    assert rows["brand"]["remaining"] == 4 + 16


def test_check_returns_only_the_limits_that_apply(api: TestClient, seeded: dict) -> None:
    rows = api.post("/unit-limits/check", json={"brand": "oppo"}).json()
    assert rows == []  # nothing caps an oppo order


def test_check_matches_a_wildcard_limit_with_no_facts(
    api: TestClient, seeded: dict
) -> None:
    blanket = api.post("/unit-limits", json={"scope": "category", "max_units": 30}).json()
    rows = api.post("/unit-limits/check", json={"account_ids": [seeded["spent"]]}).json()
    assert [row["limit_id"] for row in rows] == [blanket["id"]]
    assert rows[0]["used"] == 12  # every category, within 30 days
    assert rows[0]["value"] == ""


def test_check_with_no_limits_configured_is_empty(api: TestClient) -> None:
    assert api.post("/unit-limits/check", json=_handset()).json() == []


def test_check_rejects_an_unknown_field(api: TestClient) -> None:
    assert api.post("/unit-limits/check", json={"brands": "vivo"}).status_code == 422
