"""Tests for the live-progress WebSocket.

The UI re-opens this socket for historical runs (clicking a past run in the
history list), so the already-finished case matters as much as the live one.
The token travels as the second WebSocket subprotocol ("bearer", token), never
in the URL, where every access log on the way would keep it.
"""

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api.ws import BEARER_PROTOCOL, WS_NOT_FOUND, WS_POLICY_VIOLATION, WS_UNAUTHORIZED
from app.core import users
from app.core.security import hash_password
from app.db.models import (
    Account,
    AccountStatus,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    User,
    UserRole,
)


def _finished_job(db):
    account = Account(email="done@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    job = Job(
        type=JobType.coupon,
        status=JobStatus.completed,
        progress=100,
        total_items=1,
        done_items=1,
        failed_items=0,
        skipped_items=0,
    )
    db.add(job)
    db.commit()
    db.add(
        JobItem(
            job_id=job.id,
            account_id=account.id,
            status=JobItemStatus.done,
            message="claimed",
        )
    )
    db.commit()
    return job, account


def _connect(client, job_id, token=None):
    """Open the stream the way the browser does: the token offered as the
    subprotocol after "bearer", nothing in the URL."""
    protocols = [BEARER_PROTOCOL, token] if token is not None else None
    return client.websocket_connect(f"/ws/jobs/{job_id}", subprotocols=protocols)


def _operator(db, email, **fields):
    user = User(
        email=email,
        name="Ops",
        password_hash=hash_password("x", iterations=1000),
        role=UserRole.operator,
        active=True,
        **fields,
    )
    db.add(user)
    db.commit()
    return user


def test_finished_job_sends_a_snapshot_then_closes(client, db, admin_token):
    """A past run must render, not hang on "connecting" forever.

    It also must not leave the socket (and its bus queue) open: a finished job
    will never publish another event.
    """
    job, account = _finished_job(db)

    with _connect(client, job.id, admin_token) as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["job"]["id"] == job.id
        assert snapshot["job"]["status"] == JobStatus.completed.value
        # The per-account rows come with the snapshot, so the UI can render the
        # whole run without waiting for any event.
        assert [i["account_id"] for i in snapshot["items"]] == [account.id]
        assert snapshot["items"][0]["status"] == JobItemStatus.done.value

        # ...and the server hangs up rather than waiting for events that can
        # never arrive.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_the_server_answers_with_the_bearer_subprotocol(client, db, admin_token):
    """A browser fails the connection unless the server picks one of the
    protocols it offered, so the answer must name "bearer" — never the token."""
    job, _ = _finished_job(db)
    with _connect(client, job.id, admin_token) as ws:
        assert ws.accepted_subprotocol == BEARER_PROTOCOL
        assert ws.receive_json()["type"] == "snapshot"


def test_running_job_stays_open_for_events(client, db, admin_token):
    """A live run must NOT be closed early — it still has events coming."""
    _, account = _finished_job(db)
    job = Job(type=JobType.login, status=JobStatus.running, total_items=1)
    db.add(job)
    db.commit()

    with _connect(client, job.id, admin_token) as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["job"]["status"] == JobStatus.running.value
        # Still connected: closing here is the client's choice, not the server's.


def test_websocket_requires_a_valid_token(client, db, admin_token):
    """No token, a token that is not one, or a real token offered any other
    way than right after "bearer": all 4401 after the handshake, so the UI
    can read the code and send the operator to the login screen."""
    job, _ = _finished_job(db)

    attempts = [None, [BEARER_PROTOCOL], [BEARER_PROTOCOL, "not-a-real-token"], ["other", admin_token]]
    for protocols in attempts:
        with client.websocket_connect(f"/ws/jobs/{job.id}", subprotocols=protocols) as ws:
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_json()
            assert excinfo.value.code == WS_UNAUTHORIZED, protocols


def test_a_token_in_the_query_string_is_not_accepted(client, db, admin_token):
    """It used to be — and every connect wrote the 8-hour token into uvicorn's
    and the proxy's access logs. The URL opens nothing now."""
    job, _ = _finished_job(db)
    with client.websocket_connect(f"/ws/jobs/{job.id}?token={admin_token}") as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == WS_UNAUTHORIZED


def test_unknown_job_is_rejected(client, db, admin_token):
    with _connect(client, 999999, admin_token) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == WS_NOT_FOUND


def test_websocket_refuses_a_deactivated_users_token(client, db):
    """The socket reads the same users row as every REST request: a user
    taken out keeps no live stream on a token they still hold."""
    job, _ = _finished_job(db)
    user = _operator(db, "gone@test.com")
    token = users.issue_token(user)
    with _connect(client, job.id, token) as ws:
        assert ws.receive_json()["type"] == "snapshot"

    user.active = False
    db.commit()
    with _connect(client, job.id, token) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == WS_UNAUTHORIZED


def test_websocket_refuses_a_reset_link_token(client, db):
    """A reset token has one purpose. As a session it would outlive the reset
    it was mailed for, and open every stream."""
    job, _ = _finished_job(db)
    user = _operator(db, "resetting@test.com")
    with _connect(client, job.id, users.reset_token(user)) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == WS_UNAUTHORIZED


def test_websocket_refuses_a_user_still_on_a_temporary_password(client, db):
    """The gate every REST route has: until they choose their own password a
    stream is as closed to them as everything else — with 1008 (policy), not
    4401, so the UI does not read it as "signed out"."""
    job, _ = _finished_job(db)
    user = _operator(db, "fresh@test.com", must_change_password=True)
    with _connect(client, job.id, users.issue_token(user)) as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == WS_POLICY_VIOLATION

    user.must_change_password = False
    db.commit()
    with _connect(client, job.id, users.issue_token(user)) as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_live_events_reach_the_client(client, db, admin_token):
    """An event published while the socket is open is delivered."""
    import asyncio

    from app.jobs.tracker import JobTracker

    account = Account(email="live@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()
    job = Job(type=JobType.coupon, status=JobStatus.queued)
    db.add(job)
    db.commit()

    tracker = JobTracker(job.id)
    tracker.start([account.id])  # sets status=running so the socket stays open

    with _connect(client, job.id, admin_token) as ws:
        assert ws.receive_json()["type"] == "snapshot"

        asyncio.run(tracker.item_finished(account.id, ok=True, message="claimed"))
        event = ws.receive_json()
        assert event["type"] == "item_finished"
        assert event["account_id"] == account.id
        assert event["ok"] is True
        assert event["progress"] == 100
