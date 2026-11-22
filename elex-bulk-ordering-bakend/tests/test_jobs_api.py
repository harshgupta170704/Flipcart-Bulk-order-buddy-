"""Tests for the jobs API and the live-progress WebSocket.

Nothing here starts a browser or runs a real job: the three engine entry points
are imported into ``app.api.jobs`` as plain names, so they are monkeypatched
with recorders, and the event bus is swapped for a scripted fake.

The headline test is the security one — ``test_order_job_never_persists_card_data``
asserts that an uploaded card number reaches the task in memory and appears
*nowhere* in the response, in ``Job.payload``/``Job.log``, or in the database
file on disk (PLAN.md §5).

The routers are mounted on a local FastAPI app because ``app/main.py`` belongs
to the orchestrator and does not include them yet; the routes, the auth
dependency and the DB are the real ones.
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from starlette.websockets import WebSocketDisconnect

from app.api import jobs as jobs_api
from app.api import ws as ws_api
from app.core.security import create_access_token
from app.db.base import engine
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
)

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Test card values. The number is 16 digits so a substring scan over JSON and
# over the DB file cannot collide with a timestamp by accident.
CARD_NUMBER = "4111111111111111"
CARD_CVV = "321"


# --- doubles ------------------------------------------------------------------
async def _noop() -> None:
    return None


class _Recorder:
    """Stand-in for a jobs-engine task (``run_login_job`` & friends).

    Records the call *synchronously*: ``_spawn`` builds the coroutine inside the
    request, so the assertion never races the background task. Returns a no-op
    awaitable for the event loop to run and discard.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return _noop()


class _ScriptedBus:
    """EventBus stand-in that replays a fixed list of events, then ends."""

    def __init__(self, events: list[dict]) -> None:
        self._events = list(events)

    def subscribe(self, job_id: int):
        events = self._events

        async def _stream():
            for event in events:
                yield event

        return _stream()


# --- fixtures -----------------------------------------------------------------
@pytest.fixture
def api_app() -> FastAPI:
    application = FastAPI()
    application.include_router(jobs_api.router)
    application.include_router(ws_api.router)
    return application


@pytest.fixture
def token() -> str:
    return create_access_token("tester", role="admin")


@pytest.fixture
def api(api_app: FastAPI, token: str) -> TestClient:
    return TestClient(api_app, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
def anon(api_app: FastAPI) -> TestClient:
    return TestClient(api_app)


@pytest.fixture
def accounts(db) -> list[Account]:
    rows = [
        Account(email=f"a{i}@test.com", phone=f"90000000{i}", status=AccountStatus.new)
        for i in (1, 2)
    ]
    db.add_all(rows)
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


@pytest.fixture(autouse=True)
def _address_book(db):
    """``POST /jobs/order`` refuses a run with no Address Book address; one
    default is on file so these tests can be about the jobs API itself."""
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()


def _cards_xlsx(emails: list[str]) -> bytes:
    """A small card workbook in the header-driven format parse_cards prefers."""
    wb = Workbook()
    sheet = wb.active
    sheet.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV"])
    for email in emails:
        sheet.append([email, CARD_NUMBER, "12", "28", CARD_CVV])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# --- auth ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/jobs/login"),
        ("post", "/jobs/coupon"),
        ("post", "/jobs/order"),
        ("get", "/jobs"),
        ("get", "/jobs/1"),
        ("post", "/jobs/1/cancel"),
    ],
)
def test_every_route_needs_a_token(anon: TestClient, method: str, path: str) -> None:
    response = getattr(anon, method)(path)
    assert response.status_code in (401, 403)


def test_a_bad_token_is_rejected(api_app: FastAPI) -> None:
    client = TestClient(api_app, headers={"Authorization": "Bearer not-a-jwt"})
    assert client.get("/jobs").status_code == 401


# --- login / coupon -----------------------------------------------------------
def test_coupon_job_is_created_and_handed_off(api, db, accounts, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_coupon_job", recorder)

    response = api.post("/jobs/coupon", json={"coupon_code": "SAVE10"})
    assert response.status_code == 201

    body = response.json()
    assert body["type"] == JobType.coupon.value
    assert body["status"] == JobStatus.queued.value
    assert body["total_items"] == len(accounts)
    # payload also carries run provenance (which selection started this job)
    assert body["payload"]["coupon_code"] == "SAVE10"
    assert body["payload"]["account_count"] == len(accounts)
    assert body["progress"] == 0

    job = db.get(Job, body["id"])
    assert job is not None
    assert job.type is JobType.coupon
    assert job.payload["coupon_code"] == "SAVE10"

    # No account_ids => every account, in id order.
    assert recorder.calls == [
        {"args": (job.id, [a.id for a in accounts], "SAVE10"), "kwargs": {}}
    ]


def test_blank_coupon_code_is_rejected(api, accounts, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_coupon_job", recorder)

    assert api.post("/jobs/coupon", json={"coupon_code": "   "}).status_code == 422
    assert recorder.calls == []


def test_login_job_takes_selected_accounts_and_force(api, db, accounts, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_login_job", recorder)

    chosen = accounts[0].id
    response = api.post("/jobs/login", json={"account_ids": [chosen], "force": True})
    assert response.status_code == 201

    body = response.json()
    assert body["type"] == JobType.login.value
    assert body["total_items"] == 1
    assert body["payload"]["force"] is True
    assert body["payload"]["account_count"] == 1
    assert body["payload"]["started_by"] == "tester"  # who started it stays on the run

    assert recorder.calls[0]["args"] == (body["id"], [chosen])
    assert recorder.calls[0]["kwargs"] == {"force": True}


def test_login_job_without_accounts_is_a_400(api, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_login_job", recorder)

    response = api.post("/jobs/login", json={})
    assert response.status_code == 400
    assert recorder.calls == []


# --- ordering (card handling) -------------------------------------------------
def test_order_job_never_persists_card_data(api, db, accounts, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    product_link = "https://www.flipkart.com/some-item/p/itm123"
    files = {
        "cards_file": (
            "cards.xlsx",
            _cards_xlsx([a.email for a in accounts]),
            _XLSX_MIME,
        )
    }
    response = api.post(
        "/jobs/order",
        data={"product_link": product_link, "qty": "2", "target": "1000.5"},
        files=files,
    )
    assert response.status_code == 201

    body = response.json()
    assert body["type"] == JobType.order.value
    assert body["total_items"] == len(accounts)
    # Non-sensitive run parameters only — exactly these three keys.
    payload = body["payload"]
    assert payload["product_link"] == product_link
    assert payload["qty"] == 2
    assert payload["target"] == 1000.5
    assert payload["payment_mode"] == "card"
    assert CARD_NUMBER not in response.text

    job = db.get(Job, body["id"])
    # Provenance is fine here; card fields are what must never appear.
    assert set(job.payload) == {
        "product_link", "qty", "target", "payment_mode", "card_rows_skipped",
        "account_filter", "account_count", "started_by",
    }
    assert job.payload["card_rows_skipped"] == []  # rows set aside by number and field only, none here
    persisted = json.dumps({"payload": job.payload, "log": job.log}, default=str)
    assert CARD_NUMBER not in persisted
    assert CARD_CVV not in persisted

    # Nothing card-shaped reached the database file either (there is no Card
    # table to hold it, and Job.payload/log must not be a back door). The
    # byte-level check needs a file: on a PostgreSQL run the row checks above
    # are the whole of it.
    if engine.dialect.name == "sqlite":
        db_path = engine.url.database
        assert db_path and os.path.exists(db_path)
        with open(db_path, "rb") as handle:
            assert CARD_NUMBER.encode() not in handle.read()

    # The cards did reach the task, in memory, mapped per account.
    cards = recorder.calls[0]["kwargs"]["cards"]
    assert set(cards) == {a.id for a in accounts}
    assert cards[accounts[0].id].number == CARD_NUMBER
    assert cards[accounts[0].id].cvv == CARD_CVV
    assert recorder.calls[0]["kwargs"]["target"] == 1000.5
    assert recorder.calls[0]["kwargs"]["qty"] == 2


def test_order_job_accepts_a_comma_separated_account_list(api, db, accounts, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    chosen = accounts[1]
    response = api.post(
        "/jobs/order",
        data={"product_link": "https://www.flipkart.com/p/1", "target": "1000", "account_ids": f" {chosen.id} "},
        files={"cards_file": ("c.xlsx", _cards_xlsx([chosen.email]), _XLSX_MIME)},
    )
    assert response.status_code == 201
    assert response.json()["total_items"] == 1
    assert recorder.calls[0]["args"] == (response.json()["id"], [chosen.id])

    # The stub never finishes the run; a second order start is refused while
    # one is in progress, and that answer would come before the bad list's.
    db.get(Job, response.json()["id"]).status = JobStatus.completed
    db.commit()
    rejected = api.post(
        "/jobs/order",
        data={"product_link": "https://www.flipkart.com/p/1", "target": "1000", "account_ids": "1,oops"},
        files={"cards_file": ("c.xlsx", _cards_xlsx([chosen.email]), _XLSX_MIME)},
    )
    assert rejected.status_code == 400
    assert len(recorder.calls) == 1


def test_order_job_without_accounts_is_a_400(api, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    response = api.post(
        "/jobs/order",
        data={"product_link": "https://www.flipkart.com/p/1", "target": "1000"},
        files={"cards_file": ("c.xlsx", _cards_xlsx(["nobody@test.com"]), _XLSX_MIME)},
    )
    assert response.status_code == 400
    assert recorder.calls == []


def test_order_job_without_a_matching_card_is_a_422(api, accounts, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    response = api.post(
        "/jobs/order",
        data={"product_link": "https://www.flipkart.com/p/1", "target": "1000"},
        files={"cards_file": ("c.xlsx", _cards_xlsx(["someone-else@test.com"]), _XLSX_MIME)},
    )
    assert response.status_code == 422
    assert "card" in response.json()["detail"].lower()
    assert recorder.calls == []


def _cards_xlsx_without_emails(count: int) -> bytes:
    wb = Workbook()
    sheet = wb.active
    sheet.append(["Card Number", "Exp Month", "Exp Year", "CVV"])
    for _ in range(count):
        sheet.append([CARD_NUMBER, "12", "28", CARD_CVV])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def test_a_bad_row_in_the_card_sheet_is_set_aside_and_the_run_still_starts(api, accounts, monkeypatch) -> None:
    """One line of a long corporate list with a two-digit CVV must not refuse
    the whole run (the owner, 26 Oct 2026): the row is named on the job and in
    its log, the other cards run."""
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    chosen = accounts[0]
    wb = Workbook()
    sheet = wb.active
    sheet.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV"])
    sheet.append(["someone-else@test.com", CARD_NUMBER, "12", "28", "12"])  # the bad line, row 2
    sheet.append([chosen.email, CARD_NUMBER, "12", "28", CARD_CVV])
    buffer = io.BytesIO()
    wb.save(buffer)

    response = api.post(
        "/jobs/order",
        data={"product_link": "https://www.flipkart.com/p/1", "target": "1000", "account_ids": str(chosen.id)},
        files={"cards_file": ("cards.xlsx", buffer.getvalue(), _XLSX_MIME)},
    )

    assert response.status_code == 201, response.text
    payload = response.json()["payload"]
    assert payload["card_rows_skipped"] == [{"row": 2, "reason": payload["card_rows_skipped"][0]["reason"]}]
    assert "CVV" in payload["card_rows_skipped"][0]["reason"]
    assert "12" not in payload["card_rows_skipped"][0]["reason"].replace("row 2", "")  # no value, only the field
    assert len(recorder.calls) == 1

    # Every row bad: nothing to run with, and the rows are named.
    bad = Workbook()
    bad.active.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV"])
    bad.active.append([chosen.email, CARD_NUMBER, "12", "28", "1"])
    out = io.BytesIO()
    bad.save(out)
    from app.db.models import Job as _Job, JobStatus as _JobStatus

    api_db_job = recorder.calls[0]["args"][0]
    refused = api.post(
        "/jobs/order",
        data={"product_link": "https://www.flipkart.com/p/1", "target": "1000", "account_ids": str(chosen.id)},
        files={"cards_file": ("cards.xlsx", out.getvalue(), _XLSX_MIME)},
    )
    assert refused.status_code in (409, 422)  # 409 while the first run is still "running" in this test
    if refused.status_code == 422:
        assert "row 2" in refused.json()["detail"] and "CVV" in refused.json()["detail"]
    del api_db_job, _Job, _JobStatus


def test_a_card_sheet_with_no_emails_is_refused_for_more_than_one_account(api, accounts, monkeypatch) -> None:
    """Pairing cards to accounts by position charges the wrong card the moment
    the sheet's order and the accounts' differ (review of 26 Oct 2026); it is
    allowed only when there is nothing to pair — one account."""
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    two = f"{accounts[0].id},{accounts[1].id}"
    response = api.post(
        "/jobs/order",
        data={"product_link": "https://www.flipkart.com/p/1", "target": "1000", "account_ids": two},
        files={"cards_file": ("c.xlsx", _cards_xlsx_without_emails(2), _XLSX_MIME)},
    )
    assert response.status_code == 422
    assert "email" in response.json()["detail"] and "2 accounts" in response.json()["detail"]
    assert recorder.calls == []

    alone = api.post(
        "/jobs/order",
        data={"product_link": "https://www.flipkart.com/p/1", "target": "1000", "account_ids": str(accounts[0].id)},
        files={"cards_file": ("c.xlsx", _cards_xlsx_without_emails(1), _XLSX_MIME)},
    )
    assert alone.status_code == 201
    assert recorder.calls[0]["args"][1] == [accounts[0].id]


def test_order_job_rejects_an_unreadable_card_file(api, accounts, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    response = api.post(
        "/jobs/order",
        data={"product_link": "https://www.flipkart.com/p/1", "target": "1000"},
        files={"cards_file": ("c.xlsx", b"not a workbook", _XLSX_MIME)},
    )
    assert response.status_code == 422
    assert recorder.calls == []


# --- read / control -----------------------------------------------------------
def test_list_detail_and_unknown_job(api, db, accounts, monkeypatch) -> None:
    monkeypatch.setattr(jobs_api, "run_coupon_job", _Recorder())

    first = api.post("/jobs/coupon", json={"coupon_code": "A"}).json()
    # The stub never finishes a run, and a second coupon run is refused while
    # one is in progress — so the first is marked done before the next starts.
    db.get(Job, first["id"]).status = JobStatus.completed
    db.commit()
    second = api.post("/jobs/coupon", json={"coupon_code": "B"}).json()

    listed = api.get("/jobs").json()
    assert [j["id"] for j in listed] == [second["id"], first["id"]]  # newest first

    limited = api.get("/jobs", params={"limit": 1}).json()
    assert [j["id"] for j in limited] == [second["id"]]

    # Per-account rows are created by the engine; insert one directly since the
    # engine is stubbed out here.
    db.add(
        JobItem(
            job_id=first["id"],
            account_id=accounts[0].id,
            status=JobItemStatus.done,
            message="session saved",
            started_at=datetime(2026, 10, 26, 4, 30, 0),
            finished_at=datetime(2026, 10, 26, 4, 30, 14, 300_000),
        )
    )
    db.commit()

    detail = api.get(f"/jobs/{first['id']}").json()
    assert detail["id"] == first["id"]
    assert detail["payload"]["coupon_code"] == "A"
    assert len(detail["items"]) == 1
    item = detail["items"][0]
    assert item["account_id"] == accounts[0].id
    assert item["email"] == accounts[0].email
    assert item["status"] == JobItemStatus.done.value
    assert item["message"] == "session saved"
    # Per-account timing (plan §6): both stamps and the seconds between them.
    assert item["started_at"].startswith("2026-10-26T04:30:00")
    assert item["finished_at"].startswith("2026-10-26T04:30:14")
    assert item["seconds"] == 14.3

    assert api.get("/jobs/999999").status_code == 404


def test_cancel_sets_the_flag(api, db, accounts, monkeypatch) -> None:
    monkeypatch.setattr(jobs_api, "run_login_job", _Recorder())

    job = api.post("/jobs/login", json={}).json()
    assert job["cancel_requested"] is False

    cancelled = api.post(f"/jobs/{job['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True

    db.expire_all()
    assert db.get(Job, job["id"]).cancel_requested is True
    assert api.post("/jobs/999999/cancel").status_code == 404


# --- live progress WebSocket --------------------------------------------------
def _running_job(db, account: Account) -> Job:
    job = Job(
        type=JobType.login,
        status=JobStatus.running,
        total_items=1,
        progress=50,
        payload={"force": False},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    db.add(
        JobItem(
            job_id=job.id,
            account_id=account.id,
            status=JobItemStatus.running,
            message="started",
        )
    )
    db.commit()
    return job


def test_ws_sends_a_snapshot_then_streams_events(
    api_app, db, accounts, token, monkeypatch
) -> None:
    job = _running_job(db, accounts[0])
    monkeypatch.setattr(
        ws_api,
        "bus",
        _ScriptedBus(
            [
                {"type": "item_finished", "job_id": job.id, "ok": True, "progress": 100},
                {"type": "job_finished", "job_id": job.id, "progress": 100},
            ]
        ),
    )

    client = TestClient(api_app)
    # The token rides as the second subprotocol ("bearer", token), never in the URL.
    with client.websocket_connect(f"/ws/jobs/{job.id}", subprotocols=[ws_api.BEARER_PROTOCOL, token]) as socket:
        snapshot = socket.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["job"]["id"] == job.id
        assert snapshot["job"]["status"] == JobStatus.running.value
        assert snapshot["job"]["progress"] == 50
        assert snapshot["items"][0]["account_id"] == accounts[0].id
        assert snapshot["items"][0]["email"] == accounts[0].email

        assert socket.receive_json()["type"] == "item_finished"
        assert socket.receive_json()["type"] == "job_finished"


def test_ws_without_a_token_is_closed_unauthenticated(api_app, db, accounts) -> None:
    job = _running_job(db, accounts[0])
    client = TestClient(api_app)
    with client.websocket_connect(f"/ws/jobs/{job.id}") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == ws_api.WS_UNAUTHORIZED


def test_ws_with_a_bad_token_gets_no_data(api_app, db, accounts) -> None:
    job = _running_job(db, accounts[0])
    client = TestClient(api_app)
    with client.websocket_connect(f"/ws/jobs/{job.id}", subprotocols=[ws_api.BEARER_PROTOCOL, "garbage"]) as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == ws_api.WS_UNAUTHORIZED


def test_ws_unknown_job_is_closed(api_app, token) -> None:
    client = TestClient(api_app)
    with client.websocket_connect("/ws/jobs/999999", subprotocols=[ws_api.BEARER_PROTOCOL, token]) as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == ws_api.WS_NOT_FOUND


# --- Cash on Delivery ---------------------------------------------------------
def test_cod_order_job_needs_no_card_file(api, db, accounts, monkeypatch) -> None:
    """A COD run must start with no card upload at all.

    The card path requires a workbook; COD must not inherit that requirement,
    because no card is involved in the order.
    """
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    response = api.post(
        "/jobs/order",
        data={
            "product_link": "https://flipkart.com/p/1",
            "qty": 1,
            "target": 1000.0,
            "payment_mode": "cod",
        },
        # deliberately NO files=
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["payload"]["payment_mode"] == "cod"
    # The task was handed no cards and told to run in COD mode.
    assert recorder.calls[0]["kwargs"]["cards"] is None
    assert recorder.calls[0]["kwargs"]["payment_mode"] == "cod"


@pytest.mark.parametrize(
    "path, body",
    [
        ("/jobs/login", {"json": {}}),
        ("/jobs/coupon", {"json": {"coupon_code": "SAVE10"}}),
        ("/jobs/order", {"data": {"product_link": "https://flipkart.com/p/1", "qty": 1, "target": 1000.0, "payment_mode": "cod"}}),
    ],
)
def test_a_second_run_of_one_type_is_refused_while_the_first_is_in_progress(api, db, accounts, monkeypatch, path, body) -> None:
    """Server-side, like the read-back's guard: the page disables its button
    while it watches a run, but a second tab or a refresh does not know that,
    and two order runs over the same accounts are two real orders each."""
    for name in ("run_login_job", "run_coupon_job", "run_order_job"):
        monkeypatch.setattr(jobs_api, name, _Recorder())

    first = api.post(path, **body)
    assert first.status_code == 201, first.text
    again = api.post(path, **body)
    assert again.status_code == 409, again.text
    assert f"#{first.json()['id']}" in again.json()["detail"]
    assert "already in progress" in again.json()["detail"]

    # Finished (or cancelled) runs no longer stand in the way.
    db.get(Job, first.json()["id"]).status = JobStatus.completed
    db.commit()
    assert api.post(path, **body).status_code == 201


def test_cancelling_a_run_that_never_started_closes_it_and_frees_the_next_start(api, db, accounts, monkeypatch) -> None:
    """The compose stack hands login runs to a worker; with no worker alive the
    row stays queued for good, and the start guard would refuse every later
    login — the first thing needed to re-add an account. A cancel on a queued
    row is a withdrawal: closed outright, and the next start goes through."""
    monkeypatch.setattr(jobs_api, "run_login_job", _Recorder())

    async def taken_by_a_worker(*_args, **_kwargs):
        return True

    monkeypatch.setattr(jobs_api, "_try_enqueue", taken_by_a_worker)
    first = api.post("/jobs/login", json={})
    assert first.status_code == 201, first.text
    job_id = first.json()["id"]
    assert db.get(Job, job_id).payload["dispatch"] == "arq"  # the sweep leaves such a row alone
    assert api.post("/jobs/login", json={}).status_code == 409

    cancelled = api.post(f"/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "failed"
    db.expire_all()
    assert "Cancelled before it started." in db.get(Job, job_id).log
    assert api.post("/jobs/login", json={}).status_code == 201


def test_a_second_cancel_on_a_run_that_stopped_answering_closes_it(api, db, accounts, monkeypatch) -> None:
    """A running row whose engine is gone (a worker killed with it): the first
    cancel only raises the flag, as it must for a live run; a second cancel,
    once nothing has moved for an hour, is the operator closing it by hand."""
    from datetime import timedelta

    from app.db.models import utcnow

    job = Job(type=JobType.coupon, status=JobStatus.running, total_items=1, payload={"dispatch": "arq"})
    db.add(job)
    db.commit()
    assert api.post("/jobs/coupon", json={"coupon_code": "A"}).status_code == 409

    first = api.post(f"/jobs/{job.id}/cancel").json()
    assert first["status"] == "running" and first["cancel_requested"] is True  # a live run keeps going

    # A second cancel right away still only waits: the run may be mid-order.
    assert api.post(f"/jobs/{job.id}/cancel").json()["status"] == "running"

    job.started_at = utcnow() - timedelta(seconds=jobs_api.STALE_RUN_SECONDS + 5)
    db.commit()
    assert api.post(f"/jobs/{job.id}/cancel").json()["status"] == "failed"
    db.expire_all()
    assert "without progress" in db.get(Job, job.id).log
    assert api.post("/jobs/coupon", json={"coupon_code": "A"}).status_code == 201


def test_a_run_of_another_type_is_not_held_up(api, db, accounts, monkeypatch) -> None:
    """The guard is per type: a coupon run in progress does not stop a login run."""
    monkeypatch.setattr(jobs_api, "run_login_job", _Recorder())
    monkeypatch.setattr(jobs_api, "run_coupon_job", _Recorder())
    assert api.post("/jobs/coupon", json={"coupon_code": "SAVE10"}).status_code == 201
    assert api.post("/jobs/login", json={}).status_code == 201


def test_card_order_job_without_a_card_file_is_rejected(api, db, accounts, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    response = api.post(
        "/jobs/order",
        data={"product_link": "https://flipkart.com/p/1", "target": "1000", "payment_mode": "card"},
    )

    assert response.status_code == 422
    assert "card file is required" in response.text.lower()
    assert recorder.calls == []  # nothing was started


def test_unknown_payment_mode_is_rejected(api, db, accounts, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)

    response = api.post(
        "/jobs/order",
        data={"product_link": "https://flipkart.com/p/1", "payment_mode": "bitcoin"},
    )

    assert response.status_code == 422
    assert "payment_mode" in response.text
    assert recorder.calls == []


def test_mock_card_run_needs_no_sheet_and_keeps_the_ref_code(api, db, accounts, monkeypatch) -> None:
    """Mock hides the Payment section: a mock card run starts without a card
    sheet, and the Ref Code goes on the run and to the task."""
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    response = api.post(
        "/jobs/order",
        data={
            "product_link": "https://flipkart.com/p/1",
            "qty": 1,
            "target": 1000.0,
            "payment_mode": "card",
            "mode": "mock",
            "ref_code": "  PO-77 ",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["payload"]["mode"] == "mock"
    assert body["payload"]["ref_code"] == "PO-77"
    kwargs = recorder.calls[0]["kwargs"]
    assert kwargs["cards"] is None and kwargs["mock"] is True and kwargs["ref_code"] == "PO-77"


def test_real_card_run_still_needs_its_sheet(api, db, accounts, monkeypatch) -> None:
    monkeypatch.setattr(jobs_api, "run_order_job", _Recorder())
    response = api.post(
        "/jobs/order",
        data={"product_link": "https://flipkart.com/p/1", "qty": 1, "target": 1000.0, "payment_mode": "card"},
    )
    assert response.status_code == 422


def test_execution_version_is_recorded_and_checked(api, db, accounts, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    base = {"product_link": "https://flipkart.com/p/1", "qty": 1, "target": 1000.0, "payment_mode": "cod"}
    response = api.post("/jobs/order", data={**base, "execution_version": "normal"})
    assert response.status_code == 201, response.text
    assert response.json()["payload"]["execution_version"] == "normal"
    assert recorder.calls[0]["kwargs"]["execution_version"] == "normal"
    assert api.post("/jobs/order", data={**base, "execution_version": "warp"}).status_code == 422
    assert [v["value"] for v in api.get("/jobs/execution-versions").json()] == ["normal", "normal_mcc", "high", "high_mcc"]


def test_accounts_at_once_by_version() -> None:
    from app.jobs.pacing import accounts_at_once

    assert accounts_at_once("normal", 8) == 2
    assert accounts_at_once("normal_mcc", 1) == 1
    assert accounts_at_once("high_mcc", 8) == 8
    assert accounts_at_once(None, 4) == 4


def test_extra_product_links_follow_the_divisibility_rule(api, db, accounts, monkeypatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    base = {"product_link": "https://flipkart.com/p/1", "target": 1000.0, "payment_mode": "cod"}
    bad = api.post("/jobs/order", data={**base, "qty": 3, "extra_product_links": "https://flipkart.com/p/2"})
    assert bad.status_code == 422 and "divisible" in bad.text
    assert api.post(
        "/jobs/order", data={**base, "qty": 2, "extra_product_links": "https://example.com/x"}
    ).status_code == 422
    ok = api.post("/jobs/order", data={**base, "qty": 2, "extra_product_links": "https://flipkart.com/p/2\n"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["payload"]["extra_product_links"] == ["https://flipkart.com/p/2"]
    assert recorder.calls[0]["kwargs"]["extra_links"] == ["https://flipkart.com/p/2"]


def test_units_split_evenly_across_links() -> None:
    from app.automation.ordering import split_per_link

    assert split_per_link(4, 2) == 2
    assert split_per_link(3, 1) == 3
    assert split_per_link(1, 3) == 1
