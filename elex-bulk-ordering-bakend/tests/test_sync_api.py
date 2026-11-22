"""Tests for ``POST /jobs/sync`` (ROADMAP §10).

Nothing here starts a browser: ``run_sync_job`` is imported into
``app.api.sync`` as a plain name, so it is monkeypatched with a recorder and the
assertions are about what the endpoint *resolved* and handed over.

The router is mounted on a local FastAPI app because ``app/main.py`` belongs to
the orchestrator and does not include it yet; the route, the auth dependency and
the DB are the real ones.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Job, JobType, Tag

# The router pulls in app.jobs.sync_tasks, which imports the sibling scraper.
pytest.importorskip(
    "app.automation.sync", reason="app/automation/sync.py (sibling) not present yet"
)
from app.api import sync as sync_api  # noqa: E402  (must follow the importorskip)


# --- doubles ------------------------------------------------------------------
async def _noop() -> None:
    return None


class _Recorder:
    """Stand-in for ``run_sync_job``.

    Records synchronously: ``_spawn`` builds the coroutine inside the request, so
    an assertion never races the background task.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return _noop()


# --- fixtures -----------------------------------------------------------------
@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(sync_api.router)
    return application


@pytest.fixture
def api(api_app: FastAPI) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anon(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


@pytest.fixture
def sync_task(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(sync_api, "run_sync_job", recorder)
    return recorder


@pytest.fixture
def seeded(db) -> dict[str, Account]:
    """Four accounts worth filtering: two statuses, one tag, one subscription."""
    verified = Tag(name="VERIFIED")
    db.add(verified)
    db.commit()
    rows = {
        "gold": Account(
            email="gold@test.com",
            status=AccountStatus.session_valid,
            subscription_type="ACTIVE-PREMIUM-GOLD",
            tag_id=verified.id,
        ),
        "silver": Account(
            email="silver@test.com", status=AccountStatus.session_valid
        ),
        "blocked": Account(email="blocked@test.com", status=AccountStatus.blocked),
        "new": Account(email="new@test.com", status=AccountStatus.new),
    }
    db.add_all(rows.values())
    db.commit()
    for row in rows.values():
        db.refresh(row)
    return rows


@pytest.fixture
def ceiling(monkeypatch):
    """Lower ``max_accounts_per_run`` for one test.

    Patching the cached Settings object exercises the real config path the
    endpoint reads, and monkeypatch puts the value back afterwards.
    """

    def _set(limit: int) -> None:
        monkeypatch.setattr(get_settings(), "max_accounts_per_run", limit)

    return _set


# --- auth ---------------------------------------------------------------------
def test_sync_needs_a_token(anon: TestClient) -> None:
    response = anon.post("/jobs/sync", json={})
    assert response.status_code in (401, 403), response.text


def test_a_bad_token_is_rejected(api_app: FastAPI) -> None:
    client = TestClient(api_app, headers={"Authorization": "Bearer not-a-token"})
    assert client.post("/jobs/sync", json={}).status_code == 401


# --- which accounts -----------------------------------------------------------
def test_account_ids_select_a_subset(api, seeded, sync_task) -> None:
    chosen = [seeded["blocked"].id]
    response = api.post("/jobs/sync", json={"account_ids": chosen})
    assert response.status_code == 201, response.text
    assert response.json()["total_items"] == 1
    assert sync_task.calls[0]["args"] == (response.json()["id"], chosen)


def test_a_filter_selects_exactly_the_matching_accounts(api, seeded, sync_task) -> None:
    response = api.post(
        "/jobs/sync", json={"account_filter": {"statuses": ["session_valid"]}}
    )
    assert response.status_code == 201, response.text

    expected = sorted([seeded["gold"].id, seeded["silver"].id])
    assert response.json()["total_items"] == 2
    # The task is handed the resolved ids, not the filter.
    assert sync_task.calls[0]["args"][1] == expected


@pytest.mark.parametrize("body", [{}, {"account_ids": None, "account_filter": None}])
def test_neither_filter_nor_ids_means_every_account(
    api, seeded, sync_task, body: dict
) -> None:
    response = api.post("/jobs/sync", json=body)
    assert response.status_code == 201, response.text
    assert sync_task.calls[0]["args"][1] == sorted(row.id for row in seeded.values())


def test_the_filter_wins_when_both_are_supplied(api, seeded, sync_task) -> None:
    """One resolution order, the same as every other run: filter, ids, all."""
    response = api.post(
        "/jobs/sync",
        json={
            "account_ids": [seeded["new"].id],
            "account_filter": {"statuses": ["blocked"]},
        },
    )
    assert response.status_code == 201, response.text
    assert sync_task.calls[0]["args"][1] == [seeded["blocked"].id]


def test_an_empty_selection_is_a_400(api, db, sync_task) -> None:
    """No accounts at all: nothing to sync, and no job row left looking queued."""
    response = api.post("/jobs/sync", json={})
    assert response.status_code == 400, response.text
    assert sync_task.calls == []
    assert db.scalars(select(Job)).all() == []


def test_over_the_ceiling_is_a_422_naming_the_count_and_the_limit(
    api, db, seeded, sync_task, ceiling
) -> None:
    ceiling(2)

    response = api.post("/jobs/sync", json={})  # all four accounts
    assert response.status_code == 422, response.text

    detail = response.json()["detail"]
    assert "4" in detail and "2" in detail, detail
    # Nothing started, and no job row was left behind to look queued forever.
    assert sync_task.calls == []
    assert db.scalars(select(Job)).all() == []


# --- the job row --------------------------------------------------------------
def test_the_job_is_recorded_as_a_sync_with_its_parameters(
    api, db, seeded, sync_task
) -> None:
    response = api.post(
        "/jobs/sync",
        json={
            "account_ids": [seeded["gold"].id, seeded["silver"].id],
            "limit_per_account": 5,
            "import_unknown": True,
        },
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["type"] == JobType.sync.value
    assert body["status"] == "queued"
    assert body["total_items"] == 2

    job = db.get(Job, body["id"])
    assert job.type == JobType.sync
    assert job.total_items == 2
    assert job.payload["limit_per_account"] == 5
    assert job.payload["import_unknown"] is True
    # Provenance, recorded the same way as every other run: a historical sync
    # still says which accounts it covered.
    assert job.payload["account_count"] == 2
    assert job.payload["account_filter"] == {
        "ids": sorted([seeded["gold"].id, seeded["silver"].id])
    }


def test_the_filter_is_recorded_on_the_job(api, db, seeded, sync_task) -> None:
    response = api.post(
        "/jobs/sync", json={"account_filter": {"statuses": ["blocked"]}}
    )
    assert response.status_code == 201, response.text
    job = db.get(Job, response.json()["id"])
    assert job.payload["account_filter"] == {"statuses": ["blocked"]}
    assert job.payload["account_count"] == 1


def test_the_parameters_reach_the_task(api, seeded, sync_task) -> None:
    response = api.post(
        "/jobs/sync",
        json={
            "account_ids": [seeded["gold"].id],
            "limit_per_account": 7,
            "import_unknown": True,
        },
    )
    assert response.status_code == 201, response.text
    assert sync_task.calls[0]["kwargs"] == {
        "limit_per_account": 7,
        "import_unknown": True,
        "order_ids": None,  # the recent list, not a handed-in list of ids
        "follow_open": True,  # ...and the orders on their way it does not reach
    }


def test_the_defaults_are_twenty_orders_and_no_importing(
    api, seeded, sync_task
) -> None:
    """Importing must be opt-in: on by default it would fill the orders table
    with everything these accounts ever bought."""
    response = api.post("/jobs/sync", json={"account_ids": [seeded["gold"].id]})
    assert response.status_code == 201, response.text
    assert sync_task.calls[0]["kwargs"] == {
        "limit_per_account": 20,
        "import_unknown": False,
        "order_ids": None,
        "follow_open": True,
    }


def test_a_read_of_listed_ids_reads_just_those(api, db, seeded, sync_task) -> None:
    from app.db.models import Order, OrderStatus

    db.add(Order(account_id=seeded["gold"].id, product_link="https://www.flipkart.com/p", qty=1, status=OrderStatus.placed, order_id="OD1234567890123"))
    db.commit()
    response = api.post("/jobs/sync", json={"order_ids": ["OD1234567890123"]})
    assert response.status_code == 201, response.text
    kwargs = sync_task.calls[0]["kwargs"]
    assert kwargs["order_ids"] == ["OD1234567890123"] and kwargs["follow_open"] is False


# --- request validation -------------------------------------------------------
@pytest.mark.parametrize("limit", [0, -1, 201])
def test_an_out_of_range_limit_is_a_422(api, seeded, sync_task, limit: int) -> None:
    response = api.post(
        "/jobs/sync",
        json={"account_ids": [seeded["gold"].id], "limit_per_account": limit},
    )
    assert response.status_code == 422, response.text
    assert sync_task.calls == []


def test_a_mistyped_field_is_a_422_rather_than_a_silent_default(
    api, seeded, sync_task
) -> None:
    """``import_unknows`` would otherwise be dropped and quietly import nothing;
    a mistyped ``account_filter`` would widen the run to every account."""
    response = api.post(
        "/jobs/sync",
        json={"account_ids": [seeded["gold"].id], "import_unknows": True},
    )
    assert response.status_code == 422, response.text
    assert sync_task.calls == []


def test_a_mistyped_filter_field_is_a_422(api, seeded, sync_task) -> None:
    response = api.post("/jobs/sync", json={"account_filter": {"statuss": ["new"]}})
    assert response.status_code == 422, response.text
    assert sync_task.calls == []
