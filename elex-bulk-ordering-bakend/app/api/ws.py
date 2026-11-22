"""Live job progress over a WebSocket (PLAN.md §3 "live progress", §6).

Flow: authenticate → accept → one ``snapshot`` message so a client that
connects late still renders the full picture → then every event the jobs engine
publishes on the bus, until the run finishes or the client goes away.

The browser WebSocket API cannot set an ``Authorization`` header, so the token
travels as a WebSocket subprotocol instead: the browser opens
``new WebSocket(url, ["bearer", token])``, which puts both in the
``Sec-WebSocket-Protocol`` header, and the server answers with ``bearer``.
Not the query string: that lands in every access log on the way (uvicorn's,
the proxy's), where an 8-hour session does not belong. The token is the same
JWT the REST routes take, verified with the same ``decode_access_token`` and
checked against the same ``users`` row: a deactivated user, one whose password
changed since, one still on a temporary password, or a reset-link token gets
no stream.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from app.core.security import Principal, decode_access_token, is_session_token
from app.db.base import SessionLocal
from app.db.models import Job, JobItem, JobStatus
from app.jobs.events import bus
from app.schemas.job import JobItemOut, JobOut

log = logging.getLogger("ordering.api.ws")

router = APIRouter(tags=["jobs"])

# Application close codes (4000-4999 is the private range), and the standard
# code for a client that may connect but not do this (RFC 6455 §7.4.1).
WS_UNAUTHORIZED = 4401
WS_NOT_FOUND = 4404
WS_POLICY_VIOLATION = 1008

#: The subprotocol the browser offers in front of the token.
BEARER_PROTOCOL = "bearer"

# The engine's terminal event — after it there is nothing left to stream.
JOB_FINISHED = "job_finished"

# Job states that can never produce another event.
TERMINAL_STATUSES = frozenset({JobStatus.completed.value, JobStatus.failed.value})


def _is_terminal(snapshot: dict) -> bool:
    job = snapshot.get("job") or {}
    status = job.get("status")
    status = getattr(status, "value", status)  # Enum or plain string
    return status in TERMINAL_STATUSES


def _offered_subprotocols(websocket: WebSocket) -> list[str]:
    """What the client listed in ``Sec-WebSocket-Protocol``: the server fills
    ``scope["subprotocols"]`` from it, and the header itself is the fallback."""
    offered = websocket.scope.get("subprotocols")
    if offered is None:
        header = websocket.headers.get("sec-websocket-protocol", "")
        offered = [part.strip() for part in header.split(",")]
    return [part for part in offered if part]


def _bearer_token(offered: list[str]) -> str | None:
    """The entry right after ``bearer`` among the offered subprotocols, or None."""
    try:
        return offered[offered.index(BEARER_PROTOCOL) + 1]
    except (ValueError, IndexError):
        return None


def _authenticate(token: str | None) -> Principal | None:
    """The principal the token belongs to, or None.

    Runs in a thread (it reads the ``users`` row, like ``current_user`` does
    for every REST request), so that a stale token is refused here too.
    """
    from app.core.users import lookup_principal  # local: users imports security

    if not token:
        return None
    try:
        claims = decode_access_token(token)
    except ValueError as exc:
        log.info("websocket auth rejected: %s", exc)
        return None
    if not is_session_token(claims):
        log.info("websocket auth rejected: not a session token")
        return None
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        return None
    principal = lookup_principal(claims)
    if principal is None:
        log.info("websocket auth rejected: the session of %s is no longer valid", subject)
        return None
    return principal


def _load_snapshot(job_id: int) -> dict | None:
    """Current job + per-account rows, already JSON-safe. None if unknown.

    Uses a short-lived session rather than a request-scoped one: a socket can
    stay open for the whole run, and holding a DB session open that long would
    pin a connection for no reason.
    """
    db = SessionLocal()
    try:
        job = db.scalar(
            select(Job)
            .where(Job.id == job_id)
            .options(selectinload(Job.items).selectinload(JobItem.account))
        )
        if job is None:
            return None
        return {
            "type": "snapshot",
            "job": JobOut.model_validate(job).model_dump(mode="json"),
            "items": [
                JobItemOut.from_item(item).model_dump(mode="json")
                for item in job.items
            ],
        }
    finally:
        db.close()


async def _aclose(stream: Any) -> None:
    """Close the bus subscription so it stops buffering for a gone client."""
    closer = getattr(stream, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # noqa: BLE001 — teardown must not raise
        log.debug("could not close event subscription", exc_info=True)


async def _close_quietly(websocket: WebSocket, code: int = 1000) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        pass  # already closed by the client


@router.websocket("/ws/jobs/{job_id}")
async def job_progress(websocket: WebSocket, job_id: int) -> None:
    """Stream one job's progress to the dashboard."""
    offered = _offered_subprotocols(websocket)
    principal = await run_in_threadpool(_authenticate, _bearer_token(offered))
    # Accept first, then close with 4401: a close code sent during the handshake
    # never reaches the browser (it just sees a failed connection), whereas this
    # way the UI can read event.code and send the operator back to the login
    # screen. Nothing is ever sent on the socket before the token checks out.
    # The answer names "bearer" only when the client offered it: a browser
    # fails the connection over a subprotocol it did not ask for.
    await websocket.accept(subprotocol=BEARER_PROTOCOL if BEARER_PROTOCOL in offered else None)
    if principal is None:
        await _close_quietly(websocket, WS_UNAUTHORIZED)
        return
    if principal.must_change_password:
        # The gate every REST route has (security.current_user): a temporary
        # password opens nothing but changing it, live streams included.
        await _close_quietly(websocket, WS_POLICY_VIOLATION)
        return

    snapshot = await run_in_threadpool(_load_snapshot, job_id)
    if snapshot is None:
        await _close_quietly(websocket, WS_NOT_FOUND)
        return
    await websocket.send_json(snapshot)

    # A job that already finished will never publish another event, so
    # subscribing would hold the socket (and a bus queue) open forever — one
    # leak per time the operator opens a past run from the history list. The
    # snapshot above is the whole picture, so close straight away.
    if _is_terminal(snapshot):
        await _close_quietly(websocket)
        return

    stream = bus.subscribe(job_id)
    try:
        async for event in stream:
            await websocket.send_json(jsonable_encoder(event))
            if isinstance(event, dict) and event.get("type") == JOB_FINISHED:
                break
    except WebSocketDisconnect:
        log.debug("client left job %s stream", job_id)  # expected, no traceback
        return
    except RuntimeError as exc:
        # Send on an already-closed socket — also just a client that went away.
        log.debug("job %s stream ended: %s", job_id, exc)
        return
    except Exception:  # noqa: BLE001
        log.exception("job %s stream failed", job_id)
        await _close_quietly(websocket, 1011)
        return
    finally:
        await _aclose(stream)

    await _close_quietly(websocket)
