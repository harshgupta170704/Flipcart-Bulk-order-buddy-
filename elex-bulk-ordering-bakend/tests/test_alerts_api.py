"""Tests for the alerts API (Phase 11, ROADMAP §2).

The router is mounted on a local FastAPI app (``app/main.py`` is orchestrator
owned); the auth dependency and the DB are the real ones.

What is worth guarding here is the filtering, because the queue page lives or
dies by it: the default list is the **open** queue (the question the page is
opened to ask), a **blank** filter is no filter (what a UI sends when its
dropdowns read "any"), and an unknown non-blank value is a 422 naming the valid
ones rather than a silently empty table.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import alerts as alerts_api
from app.core import alerts as engine
from app.core.security import create_access_token
from app.db.models import (
    Account,
    AccountStatus,
    Alert,
    AlertSeverity,
    Order,
    OrderStatus,
    utcnow,
)


# --- fixtures -----------------------------------------------------------------
@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(alerts_api.router)
    return application


@pytest.fixture
def api(api_app: FastAPI) -> TestClient:
    token = create_access_token("tester", role="admin")
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anon(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


@pytest.fixture
def queue(db) -> dict:
    """Three alerts: one open warning, one open info, one already resolved.

    Staggered ``updated_at`` values, because "newest first" is only a real
    assertion when the rows do not share a timestamp.
    """
    now = utcnow()
    rows = {
        "warning": Alert(
            key="order_failed:order:1",
            kind="order_failed",
            severity=AlertSeverity.warning,
            message="Order 1 failed",
            order_id=None,
            details={"model": "Realme 2"},
            resolved=False,
            created_at=now - timedelta(hours=3),
            updated_at=now - timedelta(hours=3),
        ),
        "info": Alert(
            key="stale_sync:order:2",
            kind="stale_sync",
            severity=AlertSeverity.info,
            message="Order 2 has never been synced",
            resolved=False,
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
        ),
        "closed": Alert(
            key="session_invalid:account:9",
            kind="session_invalid",
            severity=AlertSeverity.warning,
            message="dead@test.com has an invalid session",
            resolved=True,
            created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(hours=1),
            resolved_at=now - timedelta(minutes=30),
        ),
    }
    db.add_all(rows.values())
    db.commit()
    state = {name: row.id for name, row in rows.items()}
    db.commit()  # release the read transaction before the API writes
    return state


@pytest.fixture
def findings(db) -> dict:
    """Real data for two rules to find: a failed order and a dead session."""
    account = Account(email="one@test.com", status=AccountStatus.new)
    dead = Account(email="dead@test.com", status=AccountStatus.session_invalid)
    db.add_all([account, dead])
    db.flush()
    order = Order(
        account_id=account.id,
        status=OrderStatus.failed,
        model="Realme 2",
        qty=1,
    )
    db.add(order)
    db.commit()
    state = {"account": account.id, "dead": dead.id, "order": order.id}
    db.commit()
    return state


def _keys(response) -> list[str]:
    return [row["key"] for row in response.json()]


def test_the_queue_names_the_account_rather_than_numbering_it(api, db):
    """The page's About column shows the email when the API sends it; until
    now it never did, and every row read "Account #<id>"."""
    account = Account(email="named@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    now = utcnow()
    db.add_all(
        [
            Alert(
                key=f"session_invalid:account:{account.id}",
                kind="session_invalid",
                severity=AlertSeverity.warning,
                message="named@test.com has an invalid session",
                account_id=account.id,
                resolved=False,
                created_at=now,
                updated_at=now,
            ),
            Alert(
                key="stale_sync:order:77",
                kind="stale_sync",
                severity=AlertSeverity.info,
                message="Order 77 has never been synced",
                resolved=False,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    db.commit()

    by_key = {row["key"]: row for row in api.get("/alerts").json()}
    assert by_key[f"session_invalid:account:{account.id}"]["account_email"] == "named@test.com"
    assert by_key["stale_sync:order:77"]["account_email"] is None


# --- auth ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/alerts"),
        ("get", "/alerts/summary"),
        ("post", "/alerts/evaluate"),
        ("post", "/alerts/1/resolve"),
        ("post", "/alerts/1/reopen"),
    ],
)
def test_every_route_needs_a_token(anon: TestClient, method: str, path: str) -> None:
    assert getattr(anon, method)(path).status_code in (401, 403)


def test_a_bad_token_is_rejected(api_app: FastAPI) -> None:
    client = TestClient(api_app, headers={"Authorization": "Bearer not-a-jwt"})
    assert client.get("/alerts").status_code == 401


# --- list ---------------------------------------------------------------------
def test_list_is_empty_to_start(api: TestClient) -> None:
    assert api.get("/alerts").json() == []


def test_list_defaults_to_the_open_queue(api: TestClient, queue: dict) -> None:
    """No parameters answers the question the page is opened to ask."""
    response = api.get("/alerts")
    assert response.status_code == 200, response.text
    assert set(_keys(response)) == {"order_failed:order:1", "stale_sync:order:2"}


def test_list_returns_the_whole_row(api: TestClient, queue: dict) -> None:
    row = next(r for r in api.get("/alerts").json() if r["id"] == queue["warning"])
    assert row["key"] == "order_failed:order:1"
    assert row["kind"] == "order_failed"
    assert row["severity"] == "warning"
    assert row["message"] == "Order 1 failed"
    assert row["details"] == {"model": "Realme 2"}
    assert row["resolved"] is False
    assert row["resolved_at"] is None
    assert row["created_at"] and row["updated_at"]


def test_list_is_newest_first(api: TestClient, queue: dict) -> None:
    assert _keys(api.get("/alerts")) == [
        "stale_sync:order:2",  # updated 2h ago
        "order_failed:order:1",  # updated 3h ago
    ]


def test_a_reopened_alert_comes_back_to_the_top(api: TestClient, queue: dict) -> None:
    """A problem that returned is news again, not a three-week-old row."""
    assert api.post(f"/alerts/{queue['closed']}/reopen").status_code == 200
    assert _keys(api.get("/alerts"))[0] == "session_invalid:account:9"


def test_resolved_true_lists_the_closed_ones(api: TestClient, queue: dict) -> None:
    assert _keys(api.get("/alerts?resolved=true")) == ["session_invalid:account:9"]


def test_a_blank_resolved_lists_everything(api: TestClient, queue: dict) -> None:
    """``?resolved=`` is what a UI sends for "any"; it must not be an error."""
    assert len(api.get("/alerts?resolved=").json()) == 3
    assert len(api.get("/alerts?resolved=any").json()) == 3


def test_an_unknown_resolved_is_422(api: TestClient, queue: dict) -> None:
    response = api.get("/alerts?resolved=maybe")
    assert response.status_code == 422
    assert "maybe" in response.json()["detail"]


def test_the_kind_filter_narrows(api: TestClient, queue: dict) -> None:
    assert _keys(api.get("/alerts?kind=stale_sync")) == ["stale_sync:order:2"]


def test_a_blank_kind_is_no_filter(api: TestClient, queue: dict) -> None:
    assert len(api.get("/alerts?kind=").json()) == 2


def test_an_unknown_kind_is_simply_empty(api: TestClient, queue: dict) -> None:
    """Kind is free text (a sync job may invent one), so it cannot be validated."""
    assert api.get("/alerts?kind=not_a_kind").json() == []


def test_the_severity_filter_narrows(api: TestClient, queue: dict) -> None:
    assert _keys(api.get("/alerts?severity=info")) == ["stale_sync:order:2"]
    assert _keys(api.get("/alerts?severity=warning")) == ["order_failed:order:1"]
    assert api.get("/alerts?severity=critical").json() == []


def test_a_blank_severity_is_no_filter(api: TestClient, queue: dict) -> None:
    assert len(api.get("/alerts?severity=").json()) == 2


def test_an_unknown_severity_is_422_naming_the_valid_ones(
    api: TestClient, queue: dict
) -> None:
    response = api.get("/alerts?severity=urgent")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "urgent" in detail
    assert "critical" in detail


def test_filters_combine(api: TestClient, queue: dict) -> None:
    rows = api.get("/alerts?resolved=true&severity=warning&kind=session_invalid").json()
    assert [row["key"] for row in rows] == ["session_invalid:account:9"]


def test_limit_caps_the_page(api: TestClient, queue: dict) -> None:
    assert len(api.get("/alerts?limit=1").json()) == 1


@pytest.mark.parametrize("limit", [0, -1, 1001, "lots"])
def test_a_bad_limit_is_422(api: TestClient, limit) -> None:
    assert api.get(f"/alerts?limit={limit}").status_code == 422


# --- evaluate -----------------------------------------------------------------
def test_evaluate_raises_the_alerts_the_data_implies(
    api: TestClient, findings: dict
) -> None:
    response = api.post("/alerts/evaluate")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["order_failed"] == 1
    assert body["counts"]["session_invalid"] == 1
    assert body["counts"]["delivery_overdue"] == 0
    assert body["open"] == 2
    assert set(_keys(api.get("/alerts"))) == {
        f"order_failed:order:{findings['order']}",
        f"session_invalid:account:{findings['dead']}",
    }


def test_evaluate_reports_every_kind_even_with_no_data(api: TestClient) -> None:
    body = api.post("/alerts/evaluate").json()
    assert set(body["counts"]) == set(engine.RULE_KINDS)
    assert body["open"] == 0


def test_evaluate_twice_does_not_duplicate(api: TestClient, findings: dict) -> None:
    first = api.post("/alerts/evaluate").json()
    second = api.post("/alerts/evaluate").json()
    assert first["counts"] == second["counts"]
    assert len(api.get("/alerts").json()) == 2


def test_evaluate_counts_open_across_every_kind(
    api: TestClient, db, findings: dict
) -> None:
    """``open`` is the queue total, which includes alerts no rule owns."""
    engine.raise_alert(
        db,
        key="manual_note:order:1",
        kind="manual_note",  # no rule owns it: it stays open across passes
        message="Wrong entity",
    )
    body = api.post("/alerts/evaluate").json()
    assert sum(body["counts"].values()) == 2
    assert body["open"] == 3


# --- resolve / reopen ---------------------------------------------------------
def test_resolve_closes_an_alert(api: TestClient, queue: dict) -> None:
    response = api.post(f"/alerts/{queue['warning']}/resolve")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == queue["warning"]
    assert body["resolved"] is True
    assert body["resolved_at"] is not None
    # And it leaves the queue.
    assert _keys(api.get("/alerts")) == ["stale_sync:order:2"]


def test_resolving_an_already_closed_alert_is_a_no_op(
    api: TestClient, queue: dict
) -> None:
    first = api.post(f"/alerts/{queue['closed']}/resolve").json()
    second = api.post(f"/alerts/{queue['closed']}/resolve").json()
    assert second["resolved"] is True
    assert second["resolved_at"] == first["resolved_at"]  # not re-stamped


def test_reopen_puts_an_alert_back(api: TestClient, queue: dict) -> None:
    response = api.post(f"/alerts/{queue['closed']}/reopen")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resolved"] is False
    assert body["resolved_at"] is None
    assert "session_invalid:account:9" in _keys(api.get("/alerts"))


def test_reopening_an_open_alert_is_a_no_op(api: TestClient, queue: dict) -> None:
    body = api.post(f"/alerts/{queue['warning']}/reopen").json()
    assert body["resolved"] is False


def test_a_reopened_alert_is_closed_again_by_the_next_evaluate(
    api: TestClient, queue: dict
) -> None:
    """The rules stay the authority on their own kinds."""
    api.post(f"/alerts/{queue['closed']}/reopen")
    api.post("/alerts/evaluate")
    row = next(
        r for r in api.get("/alerts?resolved=any").json() if r["id"] == queue["closed"]
    )
    assert row["resolved"] is True


@pytest.mark.parametrize("action", ["resolve", "reopen"])
def test_acting_on_a_missing_alert_is_404(api: TestClient, action: str) -> None:
    assert api.post(f"/alerts/999/{action}").status_code == 404


def test_resolve_does_not_delete_the_row(api: TestClient, db, queue: dict) -> None:
    """Resolved, not deleted: the same problem coming back reopens this row."""
    api.post(f"/alerts/{queue['warning']}/resolve")
    assert db.scalar(select(Alert.key).where(Alert.id == queue["warning"])) is not None


# --- summary ------------------------------------------------------------------
def test_summary_counts_the_queue(api: TestClient, queue: dict) -> None:
    response = api.get("/alerts/summary")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "open": 2,
        "resolved": 1,
        "by_severity": {"info": 1, "warning": 1, "critical": 0},
        "by_kind": {"order_failed": 1, "stale_sync": 1},
    }


def test_summary_is_zeroed_when_there_is_nothing(api: TestClient) -> None:
    assert api.get("/alerts/summary").json() == {
        "open": 0,
        "resolved": 0,
        "by_severity": {"info": 0, "warning": 0, "critical": 0},
        "by_kind": {},
    }


def test_summary_follows_a_resolve(api: TestClient, queue: dict) -> None:
    api.post(f"/alerts/{queue['warning']}/resolve")
    body = api.get("/alerts/summary").json()
    assert body == {
        "open": 1,
        "resolved": 2,
        "by_severity": {"info": 1, "warning": 0, "critical": 0},
        "by_kind": {"stale_sync": 1},
    }


def test_summary_matches_the_listed_rows(api: TestClient, findings: dict) -> None:
    api.post("/alerts/evaluate")
    body = api.get("/alerts/summary").json()
    assert body["open"] == len(api.get("/alerts").json())
    assert body["resolved"] == len(api.get("/alerts?resolved=true").json())
