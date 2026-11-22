"""FastAPI application entrypoint.

Run locally:
    uvicorn app.main:app --reload

Route map (see docs/PLAN.md §10):
    public      GET  /            GET  /health        POST /auth/login
    protected   everything else, via a Bearer token from /auth/login
    websocket   /ws/jobs/{id}   (the token travels as the "bearer" WebSocket
                subprotocol — browsers cannot set headers, and the query
                string would land in every access log)

The browser normally reaches this app through a proxy that strips a ``/api``
prefix (Vite in dev, nginx in production), so there is a single origin and CORS
does not apply. The CORS middleware below only matters when something calls the
API directly on its own port.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders

from app import __version__
from app.api import (
    accounts,
    agents,
    master_lists,
    addresses,
    alerts,
    auth,
    changes,
    corpassist,
    health,
    jobs,
    gsts,
    invoices,
    order_details,
    orders,
    products,
    requests,
    settings as settings_api,
    stats,
    sync,
    tags,
    unit_limits,
    users,
    ws,
)
from app.config import get_settings
from app.core.archives import reclaim_in_own_session, sweep_in_own_session
from app.core.invoices import start_backfill_in_background
from app.api.jobs import CardUploadLimit, allow_runs, begin_shutdown, drain_background_tasks
from app.jobs.schedule import run_read_back_scheduler
from app.core.logging_setup import configure_logging
from app.core.security import check_security_config, refuse_default_secret, require_user
from app.core.alerts import evaluate_in_own_session
from app.db.base import init_db
from app.jobs.recovery import recover_orphaned_jobs
from app.core.session_store import drop_invalid_in_own_session
from app.core.unit_requests import backfill_history_in_own_session

log = logging.getLogger("ordering.http")

#: A request slower than this is logged by name. The ledger's target is a
#: filtered page in under 2s at the 95th percentile (spec C-12); a request
#: that takes half of that on a dev database is worth a line in the log.
SLOW_REQUEST_MS = 1000.0


class ServerTiming:
    """Measure every request, so speed is a number and not an impression.

    The time to the answer's first line goes back to the browser as a
    ``Server-Timing`` header (visible in devtools) and, past SLOW_REQUEST_MS,
    into the log with the route's name.

    A plain ASGI middleware, not ``@app.middleware("http")``: Starlette's
    BaseHTTPMiddleware finishes a streamed answer cleanly when the app fails
    part-way, so a CSV export cut short by an error reached the browser as a
    whole, shorter file and the page said it was downloaded (review of
    27 Oct 2026). Here every message and every error passes straight
    through: an answer that fails part-way stays unfinished, and the browser
    sees it broken off.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()

        async def timed_send(message) -> None:
            if message["type"] == "http.response.start":
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                MutableHeaders(scope=message).append("Server-Timing", f"app;dur={elapsed_ms:.1f}")
                if elapsed_ms >= SLOW_REQUEST_MS:
                    log.warning("slow request: %s %s took %.0f ms", scope.get("method"), scope.get("path"), elapsed_ms)
            await send(message)

        await self.app(scope, receive, timed_send)

#: How often compiled archives past their retention are swept while the app runs.
ARCHIVE_SWEEP_SECONDS = 24 * 3600


async def _sweep_archives_daily() -> None:
    while True:
        await asyncio.sleep(ARCHIVE_SWEEP_SECONDS)
        await asyncio.to_thread(sweep_in_own_session)


async def _agent_heartbeat() -> None:
    """Register the API process as an agent and keep its heartbeat fresh."""
    from app.core import fleet
    from app.db.base import SessionLocal

    while True:
        try:
            with SessionLocal() as db:
                fleet.heartbeat(db, "api")
        except Exception as exc:  # noqa: BLE001 — a missed beat must never stop the API
            logging.getLogger("ordering.fleet").warning("agent heartbeat failed: %s", exc.__class__.__name__)
        await asyncio.sleep(fleet.HEARTBEAT_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything else: outside dev mode the shipped signing key stops the
    # start (audit F-31). First, so a refused start leaves the database and the
    # run flag exactly as they were.
    refuse_default_secret()
    # A stop sets the "no new runs" flag; a start clears it (the suite starts
    # and stops many applications in one process).
    allow_runs()
    # Creates the tables a fresh database lacks and adds missing nullable
    # columns: enough for the laptop. A PostgreSQL deploy is migrated by
    # Alembic before the API starts (compose's migrate service, DEPLOY.md §5).
    init_db()
    # A job still queued/running now was left by a process that died mid-run.
    # Nothing can finish it, and while it stands no sync can be started.
    recover_orphaned_jobs()
    # Flipkart cookie sets marked invalid are of no use and may still sign in
    # until Flipkart expires them: gone at every start (spec NFR-003).
    drop_invalid_in_own_session()
    # Cancel and reschedule requests older than their log get the steps their
    # rows still tell (spec C-4); a request already logged is left alone.
    backfill_history_in_own_session()
    # The alert queue reflects the data as of now, not as of the last time
    # someone pressed "Evaluate now" before the restart.
    evaluate_in_own_session()
    # What a compilation interrupted by the last shutdown left on disk goes
    # first (no run can be in progress yet); then the archives past their
    # retention, now and once a day after that (spec FR-039 / C-11).
    reclaim_in_own_session()
    sweep_in_own_session()
    # Invoice PDFs the parser has not read (or read with an older version),
    # parsed in the background 500 rows at a time with a breather between, so
    # the first request is not held behind the PDF library and a large store
    # is read in full rather than 500 per start.
    backfill = start_backfill_in_background()
    sweeper = asyncio.create_task(_sweep_archives_daily())
    # Read-backs on a timer, when the Settings page asks for them: a delivery
    # shows up without anyone pressing the button (app.jobs.schedule).
    scheduler = asyncio.create_task(run_read_back_scheduler())
    # This process is an agent of the fleet (app.core.fleet): registered now,
    # heard from every half minute.
    heart = asyncio.create_task(_agent_heartbeat())
    # Shout at startup if this deployment is still on the default signing key
    # (only dev mode gets this far with it) or a plaintext admin password,
    # rather than quietly accepting them.
    check_security_config()
    yield
    # Stopping: no new run may start, and the ones in flight get the grace
    # period to finish — an order cut off between Pay and the confirmation
    # page is an "unconfirmed" row for a person to settle (audit F-32).
    begin_shutdown()
    scheduler.cancel()
    heart.cancel()
    await drain_background_tasks()
    # The refusal was for the drain window only. A real process is gone a
    # moment later; the test suite stops one application and starts the next
    # in the same process, and must not inherit a closed door.
    allow_runs()
    sweeper.cancel()
    backfill.cancel()


def create_app() -> FastAPI:
    # Before anything logs: under the shipped uvicorn command nothing else
    # gives the app's INFO lines — the audit trail among them — a handler.
    configure_logging()
    cfg = get_settings()
    # The interactive docs describe every route of an internet-facing app:
    # off unless ORDERING_EXPOSE_DOCS says otherwise (DEPLOY.md §4).
    app = FastAPI(
        title=cfg.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if cfg.expose_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if cfg.expose_docs else None,
    )

    # An over-size card or sheet upload is refused before its body is read:
    # reading a card form is when Starlette writes a large file part to disk,
    # and a sheet form is read in memory, which this keeps bounded (see
    # app.api.uploads). Added first, so it sits inside the middleware below
    # and its refusals carry their headers.
    app.add_middleware(CardUploadLimit)

    origins = [o.strip() for o in cfg.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_middleware(ServerTiming)

    # FastAPI's 422 quotes back the value that failed: a card sheet posted as
    # text instead of a file would come back whole. The card routes answer
    # without it (app.api.jobs.card_upload_validation_error).
    app.add_exception_handler(RequestValidationError, jobs.card_upload_validation_error)

    # --- public ---
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(users.router)

    # --- protected ---
    # Each of these routers declares require_user on itself, so a route added to
    # one later cannot ship unauthenticated by a wiring mistake here.
    app.include_router(accounts.router)
    app.include_router(tags.router)
    app.include_router(addresses.router)
    app.include_router(gsts.router)
    app.include_router(unit_limits.router)
    app.include_router(alerts.router)
    app.include_router(sync.router)
    app.include_router(products.router)
    app.include_router(settings_api.router)
    app.include_router(jobs.router)
    app.include_router(orders.router)
    app.include_router(requests.router)
    app.include_router(order_details.router)
    app.include_router(invoices.router)
    # The archive download: authorised by its own short-lived token (see
    # app.core.archives), because a browser navigation cannot carry the bearer.
    app.include_router(invoices.public_router)
    app.include_router(stats.router)
    app.include_router(changes.router)
    app.include_router(agents.router)
    app.include_router(master_lists.gift_router)
    app.include_router(corpassist.router)
    app.include_router(master_lists.phone_router)

    # --- websocket (authenticates via the "bearer" subprotocol) ---
    app.include_router(ws.router)

    return app


app = create_app()
