"""Every step of a cancel or reschedule request is kept (spec C-4: "a status
log"; spec audit of 27 Oct 2026).

The request row holds only where it stands now, so opening a request again
used to erase who had closed it, when and with what note. Each step is now a
row of its own: opened, marked done or dropped, opened again, a note added
later. Requests older than the log get the steps their rows still tell at
start-up.
"""

from __future__ import annotations

import contextlib
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.api import requests as requests_api
from app.core import unit_requests as core
from app.core.security import create_access_token
from app.db.base import engine
from app.db.models import Account, AccountStatus, Order, OrderStatus, UnitRequest, UnitRequestEvent


def _client(subject: str, role: str | None = None) -> TestClient:
    application = FastAPI()
    application.include_router(requests_api.router)
    token = create_access_token(subject, role=role) if role else create_access_token(subject)
    return TestClient(application, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def units(db) -> list[int]:
    account = Account(email="log@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    orders = [
        Order(account_id=account.id, status=OrderStatus.placed, order_id=f"OD1000000000000001{n:02d}", platform_status="Shipped")
        for n in range(12)
    ]
    db.add_all(orders)
    db.commit()
    return [order.units[0].id for order in orders]


def _steps(item: dict) -> list[tuple]:
    return [(step["status"], step["actor"], step["note"]) for step in item["history"]]


def test_every_step_of_a_request_is_kept_a_reopen_erases_nothing(units):
    alice, bob = _client("alice@test.com"), _client("bob@test.com")
    created = alice.post("/requests", json={"unit_ids": [units[0]], "kind": "cancel", "reason": "wrong colour"})
    assert created.status_code == 201, created.text
    (item,) = created.json()["created"]
    assert _steps(item) == [("open", "alice@test.com", None)]

    rid = item["id"]
    assert bob.patch(f"/requests/{rid}", json={"status": "done", "note": "cancelled on Flipkart"}).status_code == 200
    assert alice.patch(f"/requests/{rid}", json={"status": "open"}).status_code == 200
    final = bob.patch(f"/requests/{rid}", json={"status": "dropped", "note": "kept after all"}).json()

    # The row says only the last close; the log keeps the first one too.
    assert (final["status"], final["closed_by"], final["note"]) == ("dropped", "bob@test.com", "kept after all")
    assert _steps(final) == [
        ("open", "alice@test.com", None),
        ("done", "bob@test.com", "cancelled on Flipkart"),
        ("open", "alice@test.com", None),
        ("dropped", "bob@test.com", "kept after all"),
    ]
    times = [step["at"] for step in final["history"]]
    assert times == sorted(times)


def test_a_note_added_later_is_a_step_of_its_own_and_a_repeat_is_none(units):
    api = _client("carol@test.com")
    rid = api.post("/requests", json={"unit_ids": [units[1]], "kind": "cancel", "reason": "x"}).json()["created"][0]["id"]
    api.patch(f"/requests/{rid}", json={"status": "done"})
    api.patch(f"/requests/{rid}", json={"status": "done"})  # repeats the status, no note: nothing to log
    after = api.patch(f"/requests/{rid}", json={"status": "done", "note": "refund seen"}).json()
    assert _steps(after) == [
        ("open", "carol@test.com", None),
        ("done", "carol@test.com", None),
        ("done", "carol@test.com", "refund seen"),
    ]


def test_requests_older_than_the_log_get_the_steps_their_rows_tell(db, units):
    closed = UnitRequest(
        unit_pk=units[2],
        kind="cancel",
        reason="old",
        status="done",
        requested_by="dave@test.com",
        requested_at=datetime(2026, 10, 25, 9, 0),
        closed_by="erin@test.com",
        closed_at=datetime(2026, 10, 25, 10, 0),
        note="done by hand",
    )
    still_open = UnitRequest(
        unit_pk=units[3], kind="reschedule", status="open", requested_by="dave@test.com",
        requested_at=datetime(2026, 10, 25, 9, 30),
    )
    db.add_all([closed, still_open])
    db.commit()
    db.query(UnitRequestEvent).delete()  # as before the log existed
    db.commit()

    assert core.backfill_history(db) == 2
    assert core.backfill_history(db) == 0  # once only

    steps = core.history(db, [closed.id, still_open.id])
    assert [(e.status, e.actor, e.at, e.note) for e in steps[closed.id]] == [
        ("open", "dave@test.com", datetime(2026, 10, 25, 9, 0), None),
        ("done", "erin@test.com", datetime(2026, 10, 25, 10, 0), "done by hand"),
    ]
    assert [(e.status, e.actor) for e in steps[still_open.id]] == [("open", "dave@test.com")]
    assert core.backfill_history_in_own_session() == 0


@contextlib.contextmanager
def _statements():
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def test_the_log_page_reads_every_requests_history_in_one_query(units):
    api = _client("frank@test.com")
    api.post("/requests", json={"unit_ids": units[:2], "kind": "cancel", "reason": "x"})
    with _statements() as few:
        page = api.get("/requests", params={"status": "all"}).json()
    assert all(len(item["history"]) == 1 for item in page["items"])
    api.post("/requests", json={"unit_ids": units[4:12], "kind": "cancel", "reason": "y"})
    with _statements() as many:
        page = api.get("/requests", params={"status": "all"}).json()
    assert len(page["items"]) == 10
    assert len(many) == len(few)
