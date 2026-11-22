"""Jobs API — start and track login / coupon / order runs (PLAN.md §7 phase 4).

Every route is protected: the suite sits on a public web server, so nothing here
is reachable without a bearer token (``require_user``).

Dispatch policy
---------------
Starting a run must return immediately — browser work takes minutes, an HTTP
request must not. Work is therefore handed off and the endpoint answers with the
``Job`` row the client can then poll (``GET /jobs/{id}``) or follow live over
``/ws/jobs/{id}``.

Two execution paths exist, and which one a job takes is **not** a performance
decision, it is a security one:

* **login and coupon jobs** *may* be enqueued to arq/Redis when
  ``settings.redis_url`` is set and the worker side exposes
  ``app.jobs.queue.enqueue`` — that is how the worker pool scales out
  independently of the web tier (PLAN.md §3). If either is missing, or the
  enqueue fails, the job falls back to running in-process so a misconfigured
  Redis degrades throughput instead of silently dropping runs.
* **order jobs ALWAYS run in-process.** Never queued, not even when Redis is
  configured. Enqueuing an order run would mean serializing the uploaded card
  details into Redis, and card data is never persisted anywhere outside the
  memory of the process doing the run (PLAN.md §5). The card file is read
  straight from the upload into memory, mapped to accounts, and passed to the
  task as live objects — it is never written to disk, to ``Job.payload``, to
  ``Job.log``, or to the application log.

Selecting accounts
------------------
A run's accounts are chosen by a **filter**, not by a list of ids: the portal
holds ~37k accounts, so "select all matching" must not mean posting 37,000 ids
(ROADMAP §0). All three start endpoints resolve the selection through the single
helper ``_resolve_selection`` — filter first, then an explicit id list, then
every account — so they cannot drift apart, and the resolved count is checked
against ``settings.max_accounts_per_run`` before anything starts.

``Job.payload`` records non-sensitive run parameters only, so the UI can show
what a historical run was for — including the filter that chose the accounts
(``account_filter``) and how many it matched (``account_count``), and, for an
order run, which address and GST the operator picked (ROADMAP §8), the seller
it chose to buy from (``seller``) and, when it had no price cap, that this was
chosen on purpose (``no_cap``, audit F-23).

Per-account ``JobItem`` rows are owned by the jobs engine (``app.jobs.tasks``),
which creates and updates them as it walks the accounts; this module only reads
them back for ``GET /jobs/{id}``.
"""

from __future__ import annotations

import asyncio
import re
import io
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload
from starlette.formparsers import MultiPartParser
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.addresses import default_address
from app.config import get_settings
from app.core import account_filter as account_query
from app.api.products import is_flipkart_link
from app.io import cards as card_sheets
from app.core.security import require_user
from app.db.base import SessionLocal, get_db
from app.jobs import stopping
from app.jobs.recovery import DISPATCH_ARQ
from app.db.models import (
    Account,
    Address,
    Gst,
    Job,
    JobItem,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    utcnow,
)
from app.automation.ordering import (
    CARD_MODES,
    GIFT_CARD_MODES,
    GiftCardInfo,
    ORDER_CONFIRMATION_TIMEOUT_MS,
    PAYMENT_CARD,
    PAYMENT_COD,
    PAYMENT_MODES,
    QTY_MODE_UPTO,
    QTY_MODES,
    CardInfo,
)
from app.automation.sellers import MAX_SELLER_CHARS
from app.core.card_pool import FAILURE_CODE_NAMES, CardRules
from app.core.fleet import place_order_run
from app.jobs.pacing import DEFAULT_EXECUTION_VERSION, EXECUTION_VERSIONS, Pacing, parse_duration
from app.io.cards import map_cards_to_accounts, parse_cards
from app.jobs.tasks import run_balances_job, run_coupon_job, run_login_job, run_order_job
from app.schemas.account import MAX_ID, MAX_LISTED_IDS
from app.schemas.job import (
    AccountFilterIn,
    BulkOrderRunOut,
    CardPairingOut,
    CardPreviewOut,
    CouponJobRequest,
    JobDetail,
    JobOut,
    LoginJobRequest,
)

log = logging.getLogger("ordering.api.jobs")

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_user)],
)

# Strong references to in-flight background tasks. Without this the event loop
# only holds a weak reference and a long browser run can be garbage-collected
# mid-flight.
_BACKGROUND_TASKS: set[asyncio.Task] = set()

# A corporate-card sheet is a few kilobytes, and a bigger file is refused (413)
# without being parsed. The cap alone does not keep card data off disk (PLAN.md
# §5): Starlette writes a multipart file part over 1 MiB to a temp file while
# it reads the form, before any dependency or handler here runs. What keeps
# such a body from being read at all sits in front of the form: the page never
# sends a sheet over the cap, nginx refuses a card upload over 1m from the size
# it declares, and CardUploadLimit (below) refuses, unread, a card upload that
# declares more than the cap plus FORM_FIELDS_ALLOWANCE or declares no size.
# And the form reader keeps in memory any part as big as the guard lets a
# card upload be (below), so even another client, posting straight to the API
# a part over the cap by less than that allowance, has none written to disk.
MAX_CARD_FILE_BYTES = 1024 * 1024
CARD_FILE_TOO_LARGE = "Card file is too large — a corporate-card sheet is a few KB."
#: Room for the order form's other fields beside the sheet: up to
#: MAX_LISTED_IDS account ids, a filter, the link, the seller, and the
#: multipart framing.
FORM_FIELDS_ALLOWANCE = 64 * 1024
#: The two routes a card sheet is posted to (the proxies strip "/api").
CARD_UPLOAD_PATHS = frozenset({"/jobs/order", "/jobs/order/card-preview"})

# The form reader FastAPI runs for the two card routes keeps a file part in
# memory up to its class's spool size (1 MiB) and writes a bigger one to a
# temp file. Raised to the most a card upload may declare, so no part of a
# body the guard lets through — the sheet and the form's other fields
# together — can reach disk, whichever client sent it (review of 27 Oct 2026).
# Other routes keep parts as big in memory too: the sheet routes read their
# own forms (app.api.uploads), and invoice PDFs carry no card data.
MultiPartParser.spool_max_size = max(MultiPartParser.spool_max_size, MAX_CARD_FILE_BYTES + FORM_FIELDS_ALLOWANCE)

# An account sheet, and a sheet of order ids to track, can each be the owner's
# workbook in the legacy layout, whose columns T to W hold every account's
# full card number, expiry and CVV (PLAN.md §1). Neither route reads those
# columns, but they are in the upload, so it is never written to disk either:
# both routes read the form themselves, in memory (app.api.uploads), rather
# than with the reader that writes a file part over 1 MiB to disk. The
# workbook of every account (~37,000) is about 2 MB, so they take up to
# 25 MiB, and CardUploadLimit refuses, unread, a sheet upload that declares
# more than that plus FORM_FIELDS_ALLOWANCE, or declares no size.
MAX_SHEET_FILE_BYTES = 25 * 1024 * 1024
SHEET_FILE_TOO_LARGE = "The sheet is too large — at most 25 MB is read."
#: The routes a whole sheet is posted to (the proxies strip "/api"): the
#: account import, the order ids to track, and the order report.
SHEET_UPLOAD_PATHS = frozenset({"/accounts/import", "/orders/import-ids", "/orders/import-report"})
#: The most a sheet upload may declare, and so the most of it that is ever
#: in memory: the sheet at its cap, and the multipart framing around it.
SHEET_UPLOAD_LIMIT = MAX_SHEET_FILE_BYTES + FORM_FIELDS_ALLOWANCE


@dataclass(frozen=True)
class UploadLimit:
    """What CardUploadLimit lets a POST to one route declare, and how it
    refuses one that declares more (413) or no size at all (411)."""

    most: int
    too_large: str
    no_size: str


#: Every route CardUploadLimit looks at, by path. A card sheet is a few KB; a
#: sheet of accounts or order ids is larger, and the refusal names a sheet.
UPLOAD_LIMITS: dict[str, UploadLimit] = {
    **dict.fromkeys(
        CARD_UPLOAD_PATHS,
        UploadLimit(
            most=MAX_CARD_FILE_BYTES + FORM_FIELDS_ALLOWANCE,
            too_large=CARD_FILE_TOO_LARGE,
            no_size="A card upload must state its size (Content-Length).",
        ),
    ),
    **dict.fromkeys(
        SHEET_UPLOAD_PATHS,
        UploadLimit(
            most=SHEET_UPLOAD_LIMIT,
            too_large=SHEET_FILE_TOO_LARGE,
            no_size="A sheet upload must state its size (Content-Length).",
        ),
    ),
}


class CardUploadLimit:
    """Refuses an over-size card or sheet upload before a byte of its body is read.

    Pure ASGI, registered in ``app.main``: it answers before the form is
    read. FastAPI reads a card route's form, which is when Starlette writes a
    large file part to disk; a sheet route's handler reads its own form in
    memory, and keeps it there because no body larger than this lets through
    ever reaches it (app.api.uploads). A POST to a route in UPLOAD_LIMITS
    whose Content-Length is over that route's limit is answered 413; one with
    no Content-Length (a chunked body, whose size is known only once it has
    been read) is answered 411. Browsers always send the length of a form.
    Every other request, to any route, is passed on untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] in UPLOAD_LIMITS:
            limit = UPLOAD_LIMITS[scope["path"]]
            declared = _declared_length(scope)
            if declared is None:
                await JSONResponse({"detail": limit.no_size}, status_code=411)(scope, receive, send)
                return
            if declared > limit.most:
                await JSONResponse({"detail": limit.too_large}, status_code=413)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _declared_length(scope: Scope) -> int | None:
    """The body size a request declares; None when it declares none, or one
    that is not a number."""
    for name, value in scope["headers"]:
        if name == b"content-length":
            return int(value) if value.isdigit() else None
    return None


# --- Helpers ------------------------------------------------------------------


@dataclass(frozen=True)
class _Selection:
    """The accounts one run will touch, and how they were chosen.

    ``provenance`` is the fragment merged into ``Job.payload`` so a historical
    run still says what it covered — non-sensitive by construction (a filter and
    a count; never card data, PLAN.md §5).
    """

    ids: list[int]
    provenance: dict


def _resolve_selection(
    db: DbSession,
    *,
    account_ids: list[int] | None = None,
    account_filter: AccountFilterIn | None = None,
) -> _Selection:
    """Turn "which accounts?" into ids — the one path all three runs take.

    The order is fixed and identical everywhere, so the endpoints cannot
    diverge: an explicit **filter wins**, then a list of ids, then (neither
    given) every account. A filter is resolved here in SQL, which is the whole
    point — at 37k accounts the client cannot post the ids (ROADMAP §0), and
    resolving server-side means the run covers exactly the rows the operator
    was looking at.

    Ids that no longer exist are dropped rather than rejected: an account
    deleted since the operator ticked it should not block the other 49.
    """
    ceiling = get_settings().max_accounts_per_run
    provenance: dict | None  # the filter this run resolved; None => everything

    if account_filter is not None:
        flt = account_filter.to_filter()
        provenance = account_filter.as_provenance()
    elif account_ids:
        flt = account_query.AccountFilter(ids=tuple(account_ids))
        provenance = {"ids": sorted(set(account_ids))}
    else:
        flt = account_query.AccountFilter()  # empty filter => every account
        provenance = None

    # cap=ceiling asks for one row more than the limit, so an overflow is
    # detectable without materialising 37k ids.
    matched = account_query.ids(db, flt, cap=ceiling)
    if not matched:
        raise HTTPException(
            status_code=400,
            detail=(
                "No accounts to run — import accounts first, or widen the "
                "filter / pass valid account_ids."
            ),
        )
    if len(matched) > ceiling:
        # Only now is a COUNT worth paying for, and the operator needs the real
        # number to know how much to narrow by.
        total = account_query.count(db, flt)
        log.info("refused a run over the ceiling: %s accounts > %s", total, ceiling)
        raise HTTPException(
            status_code=422,
            detail=(
                f"This selection matches {total} accounts, over the limit of "
                f"{ceiling} for a single run. Narrow the filter and try again."
            ),
        )
    return _Selection(
        ids=matched,
        provenance={"account_filter": provenance, "account_count": len(matched)},
    )


def _selected_accounts(db: DbSession, selection: _Selection) -> list[Account]:
    """Full rows for a selection — needed only to map cards to accounts.

    Safe to load in one go: ``_resolve_selection`` has already capped the
    selection at ``max_accounts_per_run``, so this is bounded by that setting
    rather than by the size of the accounts table.
    """
    stmt = (
        select(Account).where(Account.id.in_(selection.ids)).order_by(Account.id.asc())
    )
    return list(db.scalars(stmt).all())


def _parse_id_csv(raw: str | None) -> list[int] | None:
    """Parse the multipart ``account_ids`` field ("1,2,3"); blank => all.

    Bounded as the JSON starts bound theirs (``app.schemas.account``): at
    most ``MAX_LISTED_IDS`` ids, each one the database can bind. Past either
    is a 422 here rather than a 500 from the query (audit F-40).
    """
    if raw is None or not raw.strip():
        return None
    try:
        ids = [int(part) for part in (p.strip() for p in raw.split(",")) if part]
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="account_ids must be a comma-separated list of integers",
        ) from None
    if len(ids) > MAX_LISTED_IDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"account_ids lists {len(ids):,} accounts; at most {MAX_LISTED_IDS:,} — "
                "select a larger set with a filter instead"
            ),
        )
    if any(not 1 <= value <= MAX_ID for value in ids):
        raise HTTPException(status_code=422, detail=f"account_ids must be between 1 and {MAX_ID}")
    return ids


def _parse_filter_json(raw: str | None) -> AccountFilterIn | None:
    """Parse the multipart ``account_filter`` field (JSON); blank => not given.

    ``/jobs/order`` is multipart because of the card upload, so the filter
    arrives as a JSON string instead of a nested body. A malformed one is a 422:
    falling back to "no filter" would quietly widen the run to every account.
    """
    if raw is None or not raw.strip():
        return None
    try:
        return AccountFilterIn.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors()
        reason = errors[0].get("msg", "invalid") if errors else "invalid"
        raise HTTPException(
            status_code=422,
            detail=f"account_filter is not a valid filter: {reason}",
        ) from None


def _check_destination(
    db: DbSession, address_id: int | None, gst_id: int | None
) -> None:
    """Reject an unknown address/GST id up front, naming the bad one — and a
    run with no address at all, since there is no fallback address any more.

    ``run_order_job`` refuses such a run too — it must, since it can also be
    called directly — but catching it here means the operator gets a straight
    422 instead of a job that starts and then fails, and in particular means
    the uploaded card file is never read for a run that cannot proceed
    (PLAN.md §5).
    """
    if address_id is not None and db.get(Address, address_id) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"address_id {address_id} does not exist — pick a delivery "
                f"address from the address book."
            ),
        )
    if gst_id is not None and db.get(Gst, gst_id) is None:
        raise HTTPException(
            status_code=422,
            detail=f"gst_id {gst_id} does not exist — pick a GST to bill under.",
        )
    if address_id is None and default_address(db) is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "No delivery address: pick an Address Book address for this run, "
                "or mark one as the default. There is no fallback address."
            ),
        )


@dataclass(frozen=True)
class _Quantity:
    """How much one order run should buy, and under what caps (ROADMAP §9).

    Parsed once and handed to both the COD and the card branch, so the two
    cannot drift apart on what they pass the engine or record on the job.

    Every field is optional and the raw "not given" is kept rather than
    defaulted, because a form always sends its fields: a run that set none of
    these must record none of them, so an old request's ``Job.payload`` reads
    exactly as it did before this phase.
    """

    total_quantity: int | None = None
    qty_per_order: int | None = None
    total_attempts: int | None = None
    cart_amount_limit: float | None = None
    max_delivery_days: int | None = None
    brand: str | None = None
    category: str | None = None
    #: The product's model name. Carried because a model-scoped unit limit
    #: ("5 of T5x 5G per 30 days") is checked BEFORE the order is placed, and
    #: Order.model is only known after the page is scraped — so without this
    #: the tightest cap the operator relies on could never fire.
    model: str | None = None
    size: str | None = None

    @property
    def task_kwargs(self) -> dict:
        """Keyword arguments for ``run_order_job``.

        ``qty_per_order`` falls back to 1 — one unit per order is what a run
        without the field has always done.
        """
        return {
            "total_quantity": self.total_quantity,
            "qty_per_order": self.qty_per_order or 1,
            "total_attempts": self.total_attempts,
            "cart_amount_limit": self.cart_amount_limit,
            "max_delivery_days": self.max_delivery_days,
            "brand": self.brand,
            "category": self.category,
            "model": self.model,
            "size": self.size,
        }

    @property
    def payload(self) -> dict:
        """What to record on the job — non-sensitive, and only what was given."""
        given = {
            "total_quantity": self.total_quantity,
            "qty_per_order": self.qty_per_order,
            "total_attempts": self.total_attempts,
            "cart_amount_limit": self.cart_amount_limit,
            "max_delivery_days": self.max_delivery_days,
            "brand": self.brand,
            "category": self.category,
            "model": self.model,
            "size": self.size,
        }
        return {name: value for name, value in given.items() if value is not None}


def _quantity_params(
    *,
    total_quantity: int | None,
    qty_per_order: int | None,
    total_attempts: int | None,
    cart_amount_limit: float | None,
    max_delivery_days: int | None,
    brand: str | None,
    category: str | None,
    model: str | None,
    size: str | None,
) -> _Quantity:
    """Validate the quantity knobs before anything starts.

    Each count must be at least 1 when given. Zero is the dangerous one: as a
    total it is a run that buys nothing, as a batch size it is an order of no
    units, and as an attempt cap it is a run that tries no accounts — all three
    would start a job, open browsers and then do nothing, so they are refused
    here instead. Checked before the card file is read (PLAN.md §5) and before
    the ``Job`` row exists, so a rejected request starts nothing.
    """
    for name, value in (
        ("total_quantity", total_quantity),
        ("qty_per_order", qty_per_order),
        ("total_attempts", total_attempts),
    ):
        if value is not None and value < 1:
            raise HTTPException(
                status_code=422,
                detail=f"{name} must be at least 1 — got {value}.",
            )
    return _Quantity(
        total_quantity=total_quantity,
        qty_per_order=qty_per_order,
        total_attempts=total_attempts,
        cart_amount_limit=cart_amount_limit,
        max_delivery_days=max_delivery_days,
        # A form always sends the field, so blank means "not given" rather than
        # "classified as the empty string" — see _parse_filter_json.
        brand=(brand or "").strip() or None,
        category=(category or "").strip() or None,
        model=(model or "").strip() or None,
        size=(size or "").strip() or None,
    )


#: The answer to a run that names no price cap (audit F-23). It speaks the
#: form's language and names the API's flag, for a caller without the form.
CAP_MISSING = "Enter a price cap, or tick 'No price cap' (no_cap=true) to order at any price."
#: The answer to a run that gives a cap and says it has none.
CAP_CONTRADICTED = (
    "A price cap and 'No price cap' contradict each other: keep the cap and untick "
    "'No price cap', or clear the cap to order at any price."
)


def _cap_params(target: float | None, no_cap: bool) -> dict:
    """The run's price cap, stated: a target, or ``no_cap`` on purpose.

    A blank target used to mean "no cap" — every order placed at whatever the
    final amount came to, with only an info box to say so (audit F-23). A run
    must now give a cap or say in so many words that it has none; giving both
    is refused too, because one of the two is a mistake and there is no telling
    which. Each refusal names its field the way FastAPI names the target's own
    bounds, so a client can put it beside the box. Checked before the card file
    is read and before a job row exists (PLAN.md §5), so a refused request
    starts nothing.

    Returns the fragment for ``Job.payload``: ``{"no_cap": True}`` when the run
    chose to have no cap, so its history says it was uncapped on purpose, and
    nothing for a capped run, whose ``target`` already says what the cap was.
    """
    if target is None and not no_cap:
        raise HTTPException(status_code=422, detail=[{"loc": ["body", "target"], "msg": CAP_MISSING}])
    if target is not None and no_cap:
        raise HTTPException(status_code=422, detail=[{"loc": ["body", "no_cap"], "msg": CAP_CONTRADICTED}])
    return {"no_cap": True} if no_cap else {}


def _seller_param(seller: str | None) -> str | None:
    """The chosen seller with its spaces tidied, or None for Flipkart's default.

    A form always sends the field, so blank means "not chosen" — and a run
    that chose none records none, so its history reads as before.
    """
    return " ".join((seller or "").split()) or None


#: Order run modes. "ai" (paced batches) is planned and refused until it lands.
MODE_REAL = "real"
MODE_MOCK = "mock"
MODE_AI = "ai"
ORDER_MODES = (MODE_REAL, MODE_MOCK, MODE_AI)


def _mode_params(mode: str | None, by_hand: bool) -> dict:
    """The fragment for ``Job.payload`` saying the run is a mock; nothing for a
    real run, so a real run's job reads exactly as it did before modes existed.
    A 422 for a mode that does not exist, and for a mock run asked to leave
    the bank's step to a person: a mock run has no bank page."""
    chosen = (mode or MODE_REAL).strip().lower()
    if chosen not in ORDER_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown mode {mode!r} — expected one of {', '.join(ORDER_MODES)}.",
        )
    if chosen == MODE_REAL:
        return {}
    if chosen == MODE_AI:
        return {"mode": MODE_AI}
    if by_hand:
        raise HTTPException(
            status_code=422,
            detail="A mock run opens no bank page, so there is no bank step to finish by hand.",
        )
    return {"mode": MODE_MOCK}


def _pacing_params(
    max_interval_minutes: float, min_attempts: int, scale_up_pct: int, run_duration: str
) -> Pacing:
    """The AI run's pacing; a 422 naming the field when one is out of range."""
    try:
        seconds = parse_duration(run_duration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"ai_run_duration: {exc}.") from None
    return Pacing(
        max_interval_s=float(max_interval_minutes) * 60,
        min_batch=int(min_attempts),
        scale_up_pct=int(scale_up_pct),
        run_seconds=seconds,
    )


#: At most this many delivery phone numbers in one run.
MAX_ADDRESS_PHONES = 500
_PHONE_SPLIT = re.compile(r"[\s,;]+")
_TEN_DIGITS = re.compile(r"^\d{10}$")


def _parse_phones(text: str | None) -> list[str]:
    """The run's delivery phone numbers, in the order given; [] for none.

    Ten digits each, without the country code, separated by spaces, commas or
    semicolons. A 422 names every number that is not ten digits, so a typo is
    fixed before the run rather than found on a courier's call."""
    if not text or not text.strip():
        return []
    numbers = [part for part in _PHONE_SPLIT.split(text.strip()) if part]
    bad = [number for number in numbers if not _TEN_DIGITS.match(number)]
    if bad:
        shown = ", ".join(bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
        raise HTTPException(
            status_code=422,
            detail=f"Each delivery phone number must be 10 digits without the country code: {shown}.",
        )
    if len(numbers) > MAX_ADDRESS_PHONES:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_ADDRESS_PHONES} delivery phone numbers per run; {len(numbers)} were given.",
        )
    return numbers


def _qty_mode_params(qty_mode: str | None) -> dict:
    """The fragment for ``Job.payload`` for a "minimum" run; nothing for
    "upto", the behaviour every run had before the option, so those jobs read
    as they did. A 422 for anything else."""
    chosen = (qty_mode or QTY_MODE_UPTO).strip().lower()
    if chosen not in QTY_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown qty_mode {qty_mode!r} — expected one of {', '.join(QTY_MODES)}.",
        )
    return {} if chosen == QTY_MODE_UPTO else {"qty_mode": chosen}


def _bank_step_params(by_hand: bool, payment_mode: str) -> dict:
    """The fragment for ``Job.payload`` when a person finishes the bank's step:
    the choice, and how long each bank page then waits for them; nothing
    otherwise. A 422 for a run that cannot have one: a Cash-on-Delivery run
    has no bank page, and a server with no screen has nowhere to show the
    window."""
    if not by_hand:
        return {}
    if payment_mode not in CARD_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                "A Cash-on-Delivery run has no bank step to finish by hand."
                if payment_mode == PAYMENT_COD
                else "Only a run that pays by card has a bank step to finish by hand."
            ),
        )
    if not _has_screen():
        raise HTTPException(
            status_code=422,
            detail=(
                "The tool runs here without a screen, so it cannot show a browser window for "
                "the bank's step. Finish it by hand only where the tool runs on a computer you are at."
            ),
        )
    return {"bank_step_by_hand": True, "bank_wait_seconds": _bank_wait_seconds()}


def _bank_wait_seconds() -> int:
    """How long an order waits for Flipkart's confirmation once the bank's page
    has opened, as ``run_order`` reckons it: its usual wait plus the bank's
    allowance (``bank_otp_fill_wait``)."""
    return ORDER_CONFIRMATION_TIMEOUT_MS // 1000 + max(0, int(get_settings().bank_otp_fill_wait))


def _has_screen() -> bool:
    """Whether a browser window this process opens is drawn on a screen a
    person sits at. On a Mac only a process in the logged-in desktop session
    can open one, the session ``launchctl managername`` names "Aqua"; one
    started over SSH or as a background service cannot. On Linux only a
    display server gives one, and a server or a container has none. A Windows
    desktop is taken at its word."""
    if sys.platform == "darwin":
        try:
            answer = subprocess.run(
                ["launchctl", "managername"], capture_output=True, text=True, timeout=2, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return answer.stdout.strip() == "Aqua"
    if sys.platform.startswith("win"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _destination_params(address_id: int | None, gst_id: int | None) -> dict:
    """The chosen address/GST for ``Job.payload`` — ids only, so not sensitive.

    A key is omitted when nothing was chosen: that run used the Address Book's
    default rows and picked neither, which a stored ``null`` would misreport as
    a deliberate choice.
    """
    chosen: dict = {}
    if address_id is not None:
        chosen["address_id"] = address_id
    if gst_id is not None:
        chosen["gst_id"] = gst_id
    return chosen


def _create_job(
    db: DbSession, job_type: JobType, selection: _Selection, params: dict, *, started_by: str | None = None
) -> Job:
    """Persist the queued Job row.

    ``params`` must be non-sensitive run parameters only (PLAN.md §5). The
    selection's size and provenance are merged in here rather than by each
    endpoint, so no run can be recorded without them; ``started_by`` is the
    signed-in user's email, kept on the run for good (spec FR-058).
    """
    job = Job(
        type=job_type,
        status=JobStatus.queued,
        total_items=len(selection.ids),
        payload={**params, **selection.provenance, "started_by": started_by},
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _mark_failed(job_id: int, reason: str) -> None:
    """Flip a job to failed from a background callback (own DB session).

    The request's session is long gone by the time a task finishes, so this
    opens its own. Called when the task died rather than finishing its run.
    """
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = JobStatus.failed
        job.finished_at = utcnow()
        job.log = f"{job.log or ''}\n{reason}".strip()
        db.commit()
    except Exception:  # noqa: BLE001 — a failing error path must stay quiet
        log.exception("could not mark job %s failed", job_id)
    finally:
        db.close()


def _on_task_done(task: asyncio.Task, job_id: int) -> None:
    """Make a crashed run visible instead of leaving the job stuck at queued."""
    if task.cancelled():
        _mark_failed(job_id, "Run was cancelled before it finished.")
        return
    exc = task.exception()
    if exc is None:
        return
    log.error("job %s crashed: %s", job_id, exc, exc_info=exc)
    _mark_failed(job_id, f"Run crashed: {type(exc).__name__}: {exc}")


def _refuse_if_active(db: DbSession, job_type: JobType) -> None:
    """409 when a job of this type is already queued or running.

    Server-side on purpose: the UI disables its own button while it watches a
    run, but a second tab or a page refresh knows nothing of that, and two
    syncs over the same accounts write the same rows at once.
    """
    active = db.scalar(
        select(Job.id)
        .where(Job.type == job_type, Job.status.in_((JobStatus.queued, JobStatus.running)))
        .order_by(Job.id.desc())
        .limit(1)
    )
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a {job_type.value} run (#{active}) is already in progress — wait for it "
                "to finish, or cancel it, before starting another"
            ),
        )


#: Set once the process has been asked to stop: no new run may start, and the
#: ones in flight get a grace period to finish (audit F-32, 25 Oct 2026).
_SHUTTING_DOWN = False
#: How long a stopping API process waits for its in-flight runs. Compose gives
#: the container 90 s (stop_grace_period) before killing it.
SHUTDOWN_GRACE_SECONDS = 80.0
#: How long the runs cancelled at the end of that grace get to unwind.
DRAIN_CANCEL_SECONDS = 5.0


def begin_shutdown() -> None:
    """From here on `_spawn` refuses: a run started now could not finish. The
    runs already going start no new account either (``app.jobs.stopping``)."""
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = True


def allow_runs() -> None:
    """Start-up's counterpart: the module may outlive one application
    (the test suite builds and stops many; a reload does too), so the flag a
    stop left behind is cleared when the next application starts."""
    global _SHUTTING_DOWN
    _SHUTTING_DOWN = False


# The job engine's batches ask the same question before each account (audit
# F-32). The flag is read afresh at every check, never captured, because it is
# reset in place — by the next application's start-up, and by tests.
stopping.set_probe(lambda: _SHUTTING_DOWN)


async def drain_background_tasks(timeout_s: float = SHUTDOWN_GRACE_SECONDS) -> int:
    """Wait for the in-process runs to finish before the process goes.

    An order run cut off between Pay and Flipkart's confirmation leaves an
    "unconfirmed" row that a person must settle by hand, so a stop waits — up
    to ``timeout_s`` — for the runs in flight. Returns how many were still
    running when the time ran out (logged by id). Those are cancelled here and
    given a moment to unwind, so none is left running once the caller resets
    the stop flag (``allow_runs``) on its way out.
    """
    pending = {task for task in _BACKGROUND_TASKS if not task.done()}
    if not pending:
        return 0
    log.warning("stopping with %d run(s) in flight; waiting up to %.0f s for them to finish", len(pending), timeout_s)
    _done, still_running = await asyncio.wait(pending, timeout=timeout_s)
    for task in still_running:
        log.error("a run was still in flight when the grace period ended; its orders may be left unconfirmed: %r", task.get_name())
        task.cancel()
    if still_running:
        await asyncio.wait(still_running, timeout=DRAIN_CANCEL_SECONDS)
    return len(still_running)


def _spawn(coro_factory: Callable[[], Awaitable[object]], job_id: int) -> asyncio.Task:
    """Run job work in-process, detached from the request.

    The coroutine is built here (not by the caller) so nothing is created that
    we then fail to schedule, and a done-callback surfaces any exception.
    """
    if _SHUTTING_DOWN:
        raise HTTPException(status_code=503, detail="The server is stopping; start the run again in a minute.")
    task = asyncio.create_task(coro_factory(), name=f"job-{job_id}")
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    task.add_done_callback(lambda t: _on_task_done(t, job_id))
    return task


async def _try_enqueue(job_type: JobType, args: tuple, kwargs: dict) -> bool:
    """Queue path — login/coupon only. True only if arq really took the job.

    Deliberately conservative: no Redis configured, arq missing, or any error
    enqueuing all return False, and the caller then runs the job in-process. A
    misconfigured broker must degrade throughput, never drop a run.

    ``arq_worker.enqueue_job`` refuses order jobs outright (card data must not
    reach a broker — PLAN.md §5); callers here never offer one, so that refusal
    is a second line of defence rather than the first.
    """
    settings = get_settings()
    if not settings.redis_url:
        return False
    try:
        from app.jobs.arq_worker import enqueue_job, queue_enabled
    except Exception:  # noqa: BLE001 — arq extra absent => in-process
        log.info("arq adapter unavailable; running %s job in-process", job_type.value)
        return False
    if not queue_enabled():
        return False
    try:
        handle = await enqueue_job(job_type, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "could not enqueue %s job (%s) — running in-process", job_type.value, exc
        )
        return False
    if handle is None:  # no broker after all
        return False
    log.info("enqueued %s job to the worker pool", job_type.value)
    return True


async def _dispatch(
    job_id: int,
    job_type: JobType,
    args: tuple,
    kwargs: dict,
    runner: Callable[[], Awaitable[object]],
    *,
    queueable: bool,
) -> None:
    """Start the run: queue it when allowed and possible, else in-process.

    A run the broker took is marked so on its row (``payload["dispatch"]``):
    the start-up sweep (``app.jobs.recovery``) must leave such a row alone — a
    worker may still be running it — and must sweep every other one.
    """
    if queueable and await _try_enqueue(job_type, args, kwargs):
        _note_dispatch(job_id, DISPATCH_ARQ)
        return
    _spawn(runner, job_id)


def _note_dispatch(job_id: int, how: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is not None:
            job.payload = {**(job.payload or {}), "dispatch": how}  # a new dict, so the change is seen
            db.commit()
    except Exception:  # noqa: BLE001 — the run is already on its way; the note is a courtesy
        log.exception("could not record how job %s was dispatched", job_id)
    finally:
        db.close()


# --- Start runs ---------------------------------------------------------------


@router.post("/login", response_model=JobOut, status_code=201)
async def start_login_job(
    payload: LoginJobRequest, db: DbSession = Depends(get_db), who: str = Depends(require_user)
) -> JobOut:
    """Generate Flipkart sessions for the selected accounts."""
    _refuse_if_active(db, JobType.login)
    selection = _resolve_selection(
        db, account_ids=payload.account_ids, account_filter=payload.account_filter
    )
    account_ids = selection.ids
    job = _create_job(db, JobType.login, selection, {"force": payload.force}, started_by=who)
    # Snapshot before dispatch: the background run owns the row from here on.
    out = JobOut.model_validate(job)
    await _dispatch(
        out.id,
        JobType.login,
        (out.id, account_ids),
        {"force": payload.force},
        lambda: run_login_job(out.id, account_ids, force=payload.force),
        queueable=True,
    )
    return out


@router.post("/balances", response_model=JobOut, status_code=201)
async def start_balances_job(
    payload: LoginJobRequest, db: DbSession = Depends(get_db), who: str = Depends(require_user)
) -> JobOut:
    """Read the SuperCoins and gift card balance of each selected account.
    Read-only on Flipkart: two account pages, nothing pressed."""
    _refuse_if_active(db, JobType.balances)
    selection = _resolve_selection(db, account_ids=payload.account_ids, account_filter=payload.account_filter)
    account_ids = selection.ids
    job = _create_job(db, JobType.balances, selection, {}, started_by=who)
    out = JobOut.model_validate(job)
    _spawn(lambda: run_balances_job(out.id, account_ids), out.id)
    return out


@router.post("/coupon", response_model=JobOut, status_code=201)
async def start_coupon_job(
    payload: CouponJobRequest, db: DbSession = Depends(get_db), who: str = Depends(require_user)
) -> JobOut:
    """Claim one coupon code on each selected account."""
    _refuse_if_active(db, JobType.coupon)
    selection = _resolve_selection(
        db, account_ids=payload.account_ids, account_filter=payload.account_filter
    )
    account_ids = selection.ids
    job = _create_job(
        db, JobType.coupon, selection, {"coupon_code": payload.coupon_code}, started_by=who
    )
    out = JobOut.model_validate(job)
    await _dispatch(
        out.id,
        JobType.coupon,
        (out.id, account_ids, payload.coupon_code),
        {},
        lambda: run_coupon_job(out.id, account_ids, payload.coupon_code),
        queueable=True,
    )
    return out


async def _read_card_sheet(cards_file: UploadFile | None) -> card_sheets.CardSheet:
    """The uploaded card sheet, parsed in memory and dropped as bytes at once:
    the one way an order run and its card preview read it, so the two can never
    disagree about which rows are cards. Refuses what the run refuses."""
    if cards_file is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "A corporate-card file is required when paying by card. "
                "Choose Cash on Delivery to order without one."
            ),
        )
    raw = await cards_file.read()
    if len(raw) > MAX_CARD_FILE_BYTES:
        del raw
        raise HTTPException(status_code=413, detail=CARD_FILE_TOO_LARGE)
    try:
        sheet = card_sheets.parse_card_sheet(io.BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 — a bad upload is a 422, not a 500
        # One fixed sentence, and only the exception's type in the log: the
        # parser's own message can quote a cell's raw text — openpyxl's
        # "invalid literal for int()" carries a card number written with
        # spaces in a cell typed as a number, whole.
        log.warning("card sheet unreadable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not read the card file: it is not a readable .xlsx or .csv sheet — "
                "save it again from Excel (as .xlsx) or as CSV and retry."
            ),
        ) from None
    finally:
        del raw  # drop the upload bytes as soon as they are parsed
    if sheet.rejected:
        # Named by row and field only — never a value — in the log and on the
        # job, so the operator sees which lines of the sheet were set aside.
        log.warning("card sheet: %d row(s) set aside: %s", len(sheet.rejected), sheet.skipped_note())
    if not sheet.records:
        raise HTTPException(
            status_code=422,
            detail="No usable card rows in the card file"
            + (f" — every card row was set aside: {sheet.skipped_note()}" if sheet.rejected else "")
            + ".",
        )
    return sheet


#: Product links beyond the first in one order.
MAX_EXTRA_LINKS = 9

#: Payment modes not yet mapped on a live payment page (review of 17 Nov
#: 2026): refused until they are, so no run can press Pay on a method the
#: order flow cannot see is selected.
UNMAPPED_PAYMENT_MODES = {
    "cod_card": "COD + Card (advance payment)",
    "rtgs": "RTGS",
    "imps": "IMPS",
    "gv": "Gift Voucher only",
}


async def _read_gift_cards(upload: UploadFile | None, db: DbSession, selection) -> dict[int, list]:
    """The gift card sheet, paired to the run's accounts by email."""
    from app.io.gift_cards import parse_gift_card_sheet

    if upload is None:
        raise HTTPException(status_code=422, detail="A Gift Voucher run needs its gift card sheet (Email, Voucher Number, Voucher PIN).")
    raw = await upload.read()
    if len(raw) > MAX_CARD_FILE_BYTES:
        raise HTTPException(status_code=413, detail=CARD_FILE_TOO_LARGE)
    try:
        rows, rejected = parse_gift_card_sheet(raw, upload.filename or "sheet.csv")
    except Exception as exc:  # noqa: BLE001
        log.warning("gift card sheet unreadable: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail="The gift card sheet could not be read.") from None
    finally:
        del raw
    by_email: dict[str, list] = {}
    for row in rows:
        by_email.setdefault(row.email, []).append(GiftCardInfo(number=row.number, pin=row.pin))
    paired = {
        account.id: by_email[account.email.lower()]
        for account in _selected_accounts(db, selection)
        if account.email.lower() in by_email
    }
    if not paired:
        detail = "No gift card in the sheet belongs to the selected accounts — the emails must match."
        if rejected:
            detail += " Rows set aside: " + "; ".join(f"row {r['row']}: {r['reason']}" for r in rejected[:10])
        raise HTTPException(status_code=422, detail=detail)
    return paired


def _failure_codes(text: str | None) -> tuple[str, ...]:
    """The failure codes a card run excludes, from a comma-separated list."""
    codes = tuple(dict.fromkeys(part.strip().upper() for part in (text or "").split(",") if part.strip()))
    unknown = [code for code in codes if code not in FAILURE_CODE_NAMES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown card failure code(s): {', '.join(unknown)} — expected {', '.join(FAILURE_CODE_NAMES)}.",
        )
    return codes


def _only_card_type(sheet: card_sheets.CardSheet, card_type: str) -> card_sheets.CardSheet:
    """The sheet's rows of the chosen card type. A sheet with no Card Type
    column is used whole: the type is then the run's label only."""
    if not card_type or not any(record.card_type for record in sheet.records):
        return sheet
    wanted = card_type.casefold()
    kept = [record for record in sheet.records if record.card_type.casefold() == wanted]
    if not kept:
        raise HTTPException(
            status_code=422,
            detail=f"No card in the sheet is of type {card_type!r}: check the sheet's Card Type column.",
        )
    return card_sheets.CardSheet(records=kept, rejected=sheet.rejected)


@router.get("/execution-versions")
def execution_versions() -> list[dict]:
    """The order form's "Order execution version" choices."""
    return [{"value": value, "label": label} for value, label in EXECUTION_VERSIONS.items()]


@router.get("/card-failure-codes")
def card_failure_codes() -> list[dict]:
    """The codes "Exclude cards with specific failure errors" offers."""
    return [{"code": code, "name": name} for code, name in FAILURE_CODE_NAMES.items()]


def _pair_by_corpassist(
    db: DbSession | None, sheet: card_sheets.CardSheet, accounts: list, min_remaining: float | None
) -> dict[int, card_sheets.CardRecord] | None:
    """A sheet with no email column whose cards are all CorpAssist child cards
    is paired by what each card has left (app.core.corpassist.pair_by_balance);
    None for any other sheet."""
    if db is None or any(record.email for record in sheet.records):
        return None
    from app.core.corpassist import pair_by_balance

    pairs = pair_by_balance(db, sheet.records, accounts, min_remaining=min_remaining)
    if pairs is not None and not pairs:
        raise HTTPException(
            status_code=422,
            detail=(
                "Every card in the file is an HDFC child card, but none has money left"
                + (f" over the ₹{min_remaining:,.0f} minimum" if min_remaining else "")
                + " or all are cancelled in CorpAssist."
            ),
        )
    return pairs


def _pair_cards(
    sheet: card_sheets.CardSheet,
    accounts: list,
    db: DbSession | None = None,
    min_remaining: float | None = None,
) -> dict[int, card_sheets.CardRecord]:
    """Which card pays for which account (``map_cards_to_accounts``), refusing
    what the run refuses: the one pairing a run and its preview both use.
    HDFC child cards that CorpAssist knows are paired by balance instead."""
    cards = sheet.records
    by_balance = _pair_by_corpassist(db, sheet, accounts, min_remaining)
    if by_balance is not None:
        return by_balance
    if len(accounts) > 1 and not any(record.email for record in cards):
        # Pairing by position is only safe when there is nothing to pair: with
        # several accounts and no email on any card, the wrong card would pay
        # for an account the moment the sheet's order and the accounts' differ.
        raise HTTPException(
            status_code=422,
            detail=(
                f"The card file has no email column, and {len(accounts)} accounts are selected: "
                "add a column with each account's email so every card pays for its own account, "
                "or run one account at a time."
            ),
        )
    pairs = map_cards_to_accounts(cards, accounts)
    if not pairs:
        raise HTTPException(
            status_code=422,
            detail=(
                "No usable card rows for the selected accounts — the card file's "
                "emails must match the accounts in this run."
                + (f" Rows set aside: {sheet.skipped_note()}." if sheet.rejected else "")
            ),
        )
    return pairs


@router.post("/order/card-preview", response_model=CardPreviewOut)
async def preview_order_cards(
    account_ids: str | None = Form(None),
    account_filter: str | None = Form(None),
    cards_file: UploadFile | None = File(None),
    card_type: str | None = Form(None, max_length=64),
    # As the run: HDFC child cards with less than this left are not paired.
    card_min_remaining: float | None = Form(None, ge=1, le=10_000_000),
    db: DbSession = Depends(get_db),
) -> CardPreviewOut:
    """Which card would pay for which account, before a card run starts.

    The sheet is read and paired exactly as ``start_order_job`` does it
    (``_read_card_sheet``, ``_pair_cards``), with the same selection fields and
    the same refusals, so what this shows is what the run will do. It answers
    per selected account only the card's last four digits and whether its row
    carries a bank password — never a full number, an expiry, a CVV or a
    password — and the rows set aside, by row number and reason. Nothing is
    stored, logged beyond counts, or kept: Start uploads the file again.
    """
    selection = _resolve_selection(
        db, account_ids=_parse_id_csv(account_ids), account_filter=_parse_filter_json(account_filter)
    )
    sheet = _only_card_type(await _read_card_sheet(cards_file), (card_type or "").strip())
    accounts = _selected_accounts(db, selection)
    pairs = _pair_cards(sheet, accounts, db, card_min_remaining)
    rows = []
    for account in accounts:
        record = pairs.get(account.id)
        rows.append(
            CardPairingOut(
                account_id=account.id,
                email=account.email,
                card_last4=record.number[-4:] if record is not None else None,
                has_password=bool(record.password) if record is not None else None,
            )
        )
    return CardPreviewOut(
        accounts=rows,
        paired=len(pairs),
        without_card=len(accounts) - len(pairs),
        paired_by=(
            "email"
            if any(record.email for record in sheet.records)
            else "balance"
            if _pair_by_corpassist(db, sheet, accounts, card_min_remaining) is not None
            else "position"
        ),
        rows_set_aside=[{"row": item["row"], "reason": item["reason"]} for item in sheet.rejected],
    )


@router.post("/order", response_model=JobOut, status_code=201)
async def start_order_job(
    product_link: str = Form(...),
    # Bounded at the door (audit F-23, 25 Oct 2026): a quantity of 0 or less
    # ordered one unit, an infinite cap passed every price, a NaN cap failed
    # every one — each a run that looked configured and was not.
    qty: int = Form(1, ge=1),
    target: float | None = Form(None, gt=0, allow_inf_nan=False),
    # "No cap" is said on purpose, never by leaving the target blank (audit
    # F-23, 26 Oct 2026): exactly one of the two is required — _cap_params.
    no_cap: bool = Form(False),
    account_ids: str | None = Form(None),
    account_filter: str | None = Form(None),
    payment_mode: str = Form(PAYMENT_CARD),
    address_id: int | None = Form(None),
    gst_id: int | None = Form(None),
    total_quantity: int | None = Form(None),
    qty_per_order: int | None = Form(None),
    total_attempts: int | None = Form(None),
    cart_amount_limit: float | None = Form(None, gt=0, allow_inf_nan=False),
    max_delivery_days: int | None = Form(None, ge=0),
    brand: str | None = Form(None),
    category: str | None = Form(None),
    model: str | None = Form(None),
    size: str | None = Form(None),
    # The seller to buy from, as the product's sellers page names it; blank
    # leaves it to Flipkart's default. Bounded like any name a person types.
    seller: str | None = Form(None, max_length=MAX_SELLER_CHARS),
    # A person finishes the bank's step after Pay, in a browser window the run
    # shows, rather than the tool typing the card's password there.
    bank_step_by_hand: bool = Form(False),
    # "real" places orders; "mock" runs everything up to the browser and then
    # makes up each order's outcome — no browser, nothing bought, no order rows.
    mode: str = Form(MODE_REAL),
    # "upto" (default): an order may carry fewer units when Flipkart caps the
    # quantity per order. "minimum": all of them, or that order is refused.
    qty_mode: str = Form(QTY_MODE_UPTO),
    # Refuse an order whose checkout does not show the GST it bills under.
    gst_mandatory: bool = Form(False),
    # Delivery phone numbers, 10 digits each, separated by spaces or commas:
    # the run's accounts use them in turn instead of their own.
    address_phones: str | None = Form(None),
    # Every order carries the address book's phone instead of the account's.
    use_address_phone: bool = Form(False),
    # Pass over accounts with an order bought in the last N days (blank: off).
    exclude_ordered_days: int | None = Form(None, ge=1, le=365),
    # Try never-ordered accounts first, then those that ordered longest ago.
    smart_selection: bool = Form(False),
    # Card pool rules (card runs only; app.core.card_pool).
    card_cooldown_days: int | None = Form(None, ge=1, le=365),
    card_least_used_days: int | None = Form(None, ge=1, le=365),
    card_max_uses: int | None = Form(None, ge=1, le=1000),
    card_exclude_failed_days: int | None = Form(None, ge=1, le=365),
    card_smart_max_uses: int | None = Form(None, ge=1, le=1000),
    card_exclude_failed_codes: str | None = Form(None, max_length=400),
    # HDFC child cards (CorpAssist) with less than this many rupees left are not used.
    card_min_remaining: float | None = Form(None, ge=1, le=10_000_000),
    card_type: str | None = Form(None, max_length=64),
    ref_code: str | None = Form(None, max_length=64),
    execution_version: str | None = Form(None, max_length=16),
    isolated_agents: bool = Form(False),
    use_supercoins: bool = Form(False),
    min_supercoins: int | None = Form(None, ge=0, le=10_000_000),
    use_wallet: bool = Form(False),
    apply_additional_offer: bool = Form(False),
    extra_product_links: str | None = Form(None, max_length=8000),
    gift_cards_file: UploadFile | None = File(None),
    coupon_codes: str | None = Form(None, max_length=2000),
    reward_ids: str | None = Form(None, max_length=2000),
    flipkart_minutes: bool = Form(False),
    # AI mode pacing (app.jobs.pacing); read only when mode is "ai".
    ai_max_interval_minutes: float = Form(1, ge=1, le=60, allow_inf_nan=False),
    ai_min_attempts: int = Form(10, ge=1, le=30),
    ai_scale_up_pct: int = Form(20, ge=1, le=90),
    ai_run_duration: str = Form("12h", max_length=16),
    # An AI run with made-up outcomes: the pacing is real, nothing is bought.
    ai_rehearsal: bool = Form(False),
    cards_file: UploadFile | None = File(None),
    db: DbSession = Depends(get_db),
    who: str = Depends(require_user),
) -> JobOut:
    """Place orders across the selected accounts, capped by ``target`` (§6a).

    A run gives ``target``, or sets ``no_cap`` to say on purpose that it has no
    cap; neither, or both, is a 422 and nothing starts (audit F-23). A run with
    no cap records ``no_cap: true`` on its job, so its history says so.

    ``payment_mode`` is "card" or "cod". On a card run the corporate-card
    workbook is uploaded per run: parsed in memory, mapped to accounts, handed
    to the task as objects, and never stored — which is why this job type is
    never queued (see the module docstring). A Cash-on-Delivery run takes no
    card file at all.

    ``address_id`` and ``gst_id`` choose where the run ships and which GST it
    bills under (ROADMAP §8); omitting them uses the Address Book's default
    address and, for the GST, the address's linked GST and then the default.
    An id that does not exist — or no address on file at all — is a 422: the
    run never starts, because an order sent to the wrong entity succeeds and so
    would never be noticed, and there is no fallback address to send it to.

    ``seller`` makes every order buy from that seller's row on the product's
    sellers page instead of from whichever seller the product page shows by
    default; a seller the page does not list stops that order before anything
    is pressed. Recorded on the job when given.

    ``bank_step_by_hand`` is for a person at the computer the tool runs on: the
    run shows its browser window, takes the accounts one at a time and types
    nothing on the bank's page after Pay, so that person enters the card's
    password or one-time code there (on 27 Oct 2026 HDFC's page answered the
    tool's own run with "Request Blocked"). A card run only, and only where
    there is a screen to show the window on. Recorded on the job when set,
    with how long each bank page waits (``bank_wait_seconds``), which the run
    page shows the person.

    ``total_quantity`` turns the run from "one order per account" into "N units
    across accounts", in batches of ``qty_per_order``, trying at most
    ``total_attempts`` accounts and respecting each account's platform unit
    limits (ROADMAP §9). Omit it and the run behaves exactly as before.
    ``brand``/``category`` classify the product so those limits can be matched
    and counted. ``cart_amount_limit`` and ``max_delivery_days`` are recorded
    for the run and enforced in ``run_order_job`` before any payment step.

    This endpoint is multipart, so ``account_filter`` is a JSON string of the
    same shape the other two take as a nested body.
    """
    payment_mode = (payment_mode or PAYMENT_CARD).strip().lower()
    if payment_mode not in PAYMENT_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown payment_mode {payment_mode!r} — expected one of "
                f"{', '.join(PAYMENT_MODES)}."
            ),
        )
    if payment_mode in UNMAPPED_PAYMENT_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{UNMAPPED_PAYMENT_MODES[payment_mode]} is not available yet: it has not been checked on a live "
                "Flipkart payment page. Choose Card, Cash on Delivery or GV + card."
            ),
        )
    # Before anything else: two order runs at once over the same accounts are
    # two real orders per account, and a second tab or a refresh knows nothing
    # of the button the first tab disabled.
    if not is_flipkart_link(product_link):
        # The run drives a real browser to this link; it opens Flipkart product
        # pages and nothing else (audit F-44, 25 Oct 2026).
        raise HTTPException(
            status_code=422,
            detail="The product link must be a Flipkart link (flipkart.com); the ordering browser opens nothing else.",
        )
    # With the request's other fields, before anything is looked up: a run
    # that has not said what its price cap is must not get as far as a 409,
    # let alone the card file.
    cap_params = _cap_params(target, no_cap)
    mode_params = _mode_params(mode, bank_step_by_hand)
    qty_mode_params = _qty_mode_params(qty_mode)
    chosen_qty_mode = qty_mode_params.get("qty_mode", QTY_MODE_UPTO)
    phones = _parse_phones(address_phones)
    chosen_version = (execution_version or DEFAULT_EXECUTION_VERSION).strip().lower()
    if chosen_version not in EXECUTION_VERSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown execution_version {execution_version!r} — expected one of {', '.join(EXECUTION_VERSIONS)}.",
        )
    card_rules = CardRules(
        cooldown_days=card_cooldown_days,
        least_used_days=card_least_used_days,
        max_uses=card_max_uses,
        exclude_failed_days=card_exclude_failed_days,
        smart_max_uses=card_smart_max_uses,
        exclude_failed_codes=_failure_codes(card_exclude_failed_codes),
        min_remaining=card_min_remaining,
    )
    chosen_card_type = (card_type or "").strip()
    if (card_rules.any() or chosen_card_type) and payment_mode not in CARD_MODES:
        raise HTTPException(
            status_code=422,
            detail="Card rules apply to card runs; a Cash-on-Delivery run uses no card.",
        )
    delivery_params = {
        **({"gst_mandatory": True} if gst_mandatory else {}),
        **({"address_phones": len(phones)} if phones else {}),
        **({"use_address_phone": True} if use_address_phone and not phones else {}),
        **({"exclude_ordered_days": exclude_ordered_days} if exclude_ordered_days else {}),
        **({"smart_selection": True} if smart_selection else {}),
    }
    pacing = None
    if mode_params.get("mode") == MODE_AI:
        pacing = _pacing_params(ai_max_interval_minutes, ai_min_attempts, ai_scale_up_pct, ai_run_duration)
        if ai_rehearsal and bank_step_by_hand:
            raise HTTPException(
                status_code=422,
                detail="A rehearsal opens no bank page, so there is no bank step to finish by hand.",
            )
        mode_params = {**mode_params, "pacing": pacing.as_payload(), **({"rehearsal": True} if ai_rehearsal else {})}
    mock = mode_params.get("mode") == MODE_MOCK or bool(pacing is not None and ai_rehearsal)
    # Likewise a bank step to finish by hand that this run cannot have.
    by_hand_params = _bank_step_params(bank_step_by_hand, payment_mode)
    _refuse_if_active(db, JobType.order)
    _check_destination(db, address_id, gst_id)
    destination_params = _destination_params(address_id, gst_id)
    quantity = _quantity_params(
        total_quantity=total_quantity,
        qty_per_order=qty_per_order,
        total_attempts=total_attempts,
        cart_amount_limit=cart_amount_limit,
        max_delivery_days=max_delivery_days,
        brand=brand,
        category=category,
        model=model,
        size=size,
    )
    seller = _seller_param(seller)
    seller_params = {"seller": seller} if seller else {}
    chosen_ref_code = (ref_code or "").strip() or None
    if execution_version:
        seller_params = {**seller_params, "execution_version": chosen_version}
    codes = list(dict.fromkeys(part.strip() for part in re.split(r"[\s,;]+", coupon_codes or "") if part.strip()))
    if len(codes) > 10:
        raise HTTPException(status_code=422, detail="At most 10 coupon codes in one run.")
    if codes:
        seller_params = {**seller_params, "coupon_codes": codes}
    rewards = [part.strip() for part in re.split(r"[\s,;]+", reward_ids or "") if part.strip()]
    if rewards:
        seller_params = {**seller_params, "reward_ids": rewards}
    if flipkart_minutes:
        seller_params = {**seller_params, "flipkart_minutes": True}
    extra_links = [link.strip() for link in (extra_product_links or "").splitlines() if link.strip()]
    if len(extra_links) > MAX_EXTRA_LINKS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_EXTRA_LINKS + 1} product links in one order.")
    for link in extra_links:
        if not is_flipkart_link(link):
            raise HTTPException(status_code=422, detail="Every product link must be a Flipkart link (flipkart.com).")
    if extra_links:
        per_order = quantity.qty_per_order or qty or 1
        if per_order % (len(extra_links) + 1):
            raise HTTPException(
                status_code=422,
                detail="Quantity per order must be divisible by the number of product URLs.",
            )
        if seller:
            raise HTTPException(status_code=422, detail="A seller cannot be chosen for an order of several products.")
        seller_params = {**seller_params, "extra_product_links": extra_links}
    placement = place_order_run(db, isolated=isolated_agents)
    if placement.reason:
        raise HTTPException(status_code=409, detail=placement.reason)
    if isolated_agents:
        seller_params = {**seller_params, "isolated_agents": True}
    if use_supercoins:
        seller_params = {**seller_params, "use_supercoins": True, **({"min_supercoins": min_supercoins} if min_supercoins else {})}
    if use_wallet:
        seller_params = {**seller_params, "use_wallet": True}
    if apply_additional_offer:
        seller_params = {**seller_params, "apply_additional_offer": True}
    if placement.agent is not None:
        seller_params = {**seller_params, "agent": placement.agent.name}
    if chosen_ref_code:
        seller_params = {**seller_params, "ref_code": chosen_ref_code}

    wanted_filter = _parse_filter_json(account_filter)
    wanted_ids = _parse_id_csv(account_ids)
    # Accounts first: an empty selection is a 400 about accounts, and we have no
    # reason to touch the card file before we know there is work to do.
    selection = _resolve_selection(
        db, account_ids=wanted_ids, account_filter=wanted_filter
    )
    run_ids = selection.ids

    # The gift card sheet of a Gift Voucher run: read in memory, paired by
    # email, never stored — the run gets objects, the job only a count.
    gift_cards: dict[int, list] | None = None
    if payment_mode in GIFT_CARD_MODES and not mock:
        gift_cards = await _read_gift_cards(gift_cards_file, db, selection)
        seller_params = {**seller_params, "gift_card_accounts": len(gift_cards)}
    # Mock hides the Payment section: a mock card run with no sheet rehearses
    # without cards, on the same no-card path as Cash on Delivery. Every mode
    # that pays nothing by card goes this way too.
    if payment_mode not in CARD_MODES or (mock and cards_file is None):
        # No card is involved, so nothing is read, parsed or held in memory —
        # and no account row needs loading either, only its id.
        job = _create_job(
            db,
            JobType.order,
            selection,
            {
                "product_link": product_link,
                "qty": qty,
                "target": target,
                **cap_params,
                **mode_params,
                **qty_mode_params,
                **delivery_params,
                "payment_mode": payment_mode,
                **destination_params,
                **quantity.payload,
                **seller_params,
            },
            started_by=who,
        )
        out = JobOut.model_validate(job)
        _spawn(
            lambda: run_order_job(
                out.id,
                run_ids,
                product_link=product_link,
                qty=qty,
                target=target,
                cards=None,
                payment_mode=payment_mode,
                address_id=address_id,
                gst_id=gst_id,
                seller=seller,
                mock=mock,
                pacing=pacing,
                qty_mode=chosen_qty_mode,
                gst_mandatory=gst_mandatory,
                address_phones=phones,
                use_address_phone=use_address_phone,
                exclude_ordered_days=exclude_ordered_days,
                smart_selection=smart_selection,
                ref_code=chosen_ref_code,
                execution_version=chosen_version,
                use_supercoins=use_supercoins,
                min_supercoins=min_supercoins if use_supercoins else None,
                use_wallet=use_wallet,
                extra_links=extra_links or None,
                gift_cards=gift_cards,
                coupon_codes=codes or None,
                **quantity.task_kwargs,
            ),
            out.id,
        )
        return out

    sheet = _only_card_type(await _read_card_sheet(cards_file), chosen_card_type)
    # Convert to the type run_order actually declares. CardRecord happens to be
    # duck-compatible, but relying on that would break the moment anything in
    # the engine does an isinstance check — and CardInfo carries no email, so
    # the identifying field is dropped here too.
    # Mapping cards to accounts is the one step that needs the rows themselves
    # (it matches on email), so they are loaded here and nowhere else.
    card_map = {
        account_id: CardInfo(
            number=record.number,
            month=record.month,
            year=record.year,
            cvv=record.cvv,
            password=record.password,
        )
        for account_id, record in _pair_cards(
            sheet, _selected_accounts(db, selection), db, card_rules.min_remaining
        ).items()
    }

    job = _create_job(
        db,
        JobType.order,
        selection,
        # Non-sensitive only. Card details are NOT written here (PLAN.md §5);
        # the rows set aside are row numbers and field names.
        {
            "product_link": product_link,
            "qty": qty,
            "target": target,
            **cap_params,
            **mode_params,
            **qty_mode_params,
            **delivery_params,
            "payment_mode": payment_mode,
            "card_rows_skipped": sheet.rejected,
            **card_rules.as_payload(),
            **({"card_type": chosen_card_type} if chosen_card_type else {}),
            **destination_params,
            **quantity.payload,
            **seller_params,
            **by_hand_params,
        },
        started_by=who,
    )
    if sheet.rejected:
        job.log = f"{job.log or ''}card sheet: {len(sheet.rejected)} row(s) set aside — {sheet.skipped_note()}\n"
        db.commit()
    out = JobOut.model_validate(job)
    _spawn(
        lambda: run_order_job(
            out.id,
            run_ids,
            product_link=product_link,
            qty=qty,
            target=target,
            cards=card_map,
            payment_mode=payment_mode,
            address_id=address_id,
            gst_id=gst_id,
            seller=seller,
            bank_step_by_hand=bank_step_by_hand,
            card_rules=card_rules,
            mock=mock,
            pacing=pacing,
            qty_mode=chosen_qty_mode,
            gst_mandatory=gst_mandatory,
            address_phones=phones,
            use_address_phone=use_address_phone,
            exclude_ordered_days=exclude_ordered_days,
            smart_selection=smart_selection,
            ref_code=chosen_ref_code,
            execution_version=chosen_version,
                use_supercoins=use_supercoins,
                min_supercoins=min_supercoins if use_supercoins else None,
                use_wallet=use_wallet,
                extra_links=extra_links or None,
                gift_cards=gift_cards,
                coupon_codes=codes or None,
            **quantity.task_kwargs,
        ),
        out.id,
    )
    return out


async def card_upload_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's 422 on the two card routes, without the values that failed.

    FastAPI's own answer quotes each failing field's ``input`` back. A card
    sheet posted as a text field rather than as a file fails as "Expected
    UploadFile", and its ``input`` is the whole sheet — every number, expiry,
    CVV and password in it — sent straight back in the answer (review of
    27 Oct 2026). On ``POST /jobs/order`` and its card preview each error
    keeps its field and its reason and loses the value; every other route
    answers exactly as FastAPI does. Registered in ``app.main``.
    """
    if request.scope.get("endpoint") not in (start_order_job, preview_order_cards):
        return await request_validation_exception_handler(request, exc)
    errors = [{key: value for key, value in error.items() if key != "input"} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


# --- Read / control -----------------------------------------------------------


@router.get("/bulk-orders", response_model=list[BulkOrderRunOut])
def list_bulk_orders(
    limit: int = Query(50, ge=1, le=500),
    since: datetime | None = Query(None, description="runs started at or after (ISO)"),
    until: datetime | None = Query(None, description="runs started before (ISO)"),
    status: list[JobStatus] | None = Query(None),
    mode: str | None = Query(None, description="real, mock or ai"),
    created_by: str | None = Query(None, max_length=255),
    platform: str | None = Query(None, max_length=32),
    db: DbSession = Depends(get_db),
) -> list[BulkOrderRunOut]:
    """The Orders page: one row per order run, newest first.

    Everything a row shows that the run's own record does not hold — orders
    and units placed, the product's name, the address and GST labels — is
    read here in a fixed number of queries over the page, never per row.
    """
    stmt = select(Job).where(Job.type == JobType.order)
    if since is not None:
        stmt = stmt.where(Job.started_at >= since.replace(tzinfo=None))
    if until is not None:
        stmt = stmt.where(Job.started_at < until.replace(tzinfo=None))
    if status:
        stmt = stmt.where(Job.status.in_(status))
    if platform and platform.strip().lower() != "flipkart":
        return []  # Flipkart is the one platform runs are placed on today
    jobs = db.scalars(stmt.order_by(Job.id.desc()).limit(limit if not (mode or created_by) else 2000)).all()
    # Mode and creator live in the run's JSON payload: filtered here, then cut to the limit.
    if mode:
        wanted = mode.strip().lower()
        jobs = [job for job in jobs if str((job.payload or {}).get("mode") or "real") == wanted]
    if created_by:
        jobs = [job for job in jobs if (job.payload or {}).get("started_by") == created_by]
    jobs = jobs[:limit]
    if not jobs:
        return []
    ids = [job.id for job in jobs]

    tallies: dict[int, dict] = {}
    rows = db.execute(
        select(
            Order.job_id,
            Order.status,
            func.count(Order.id),
            func.coalesce(func.sum(func.coalesce(Order.qty, 1)), 0),
            func.coalesce(func.sum(Order.amount), 0),
        )
        .where(Order.job_id.in_(ids))
        .group_by(Order.job_id, Order.status)
    ).all()
    for job_id, order_status, count, units, amount in rows:
        tally = tallies.setdefault(job_id, {"orders": 0, "units": 0, "amount": 0.0, "unconfirmed": 0, "failed": 0})
        if order_status == OrderStatus.placed:
            tally["orders"] += count
            tally["units"] += int(units)
            tally["amount"] += float(amount or 0)
        elif order_status == OrderStatus.unconfirmed:
            tally["unconfirmed"] += count
        else:
            tally["failed"] += count

    # The card the run's orders were paid with, masked ("54XX…824803"): the
    # Orders page's "Parent Cards". The ledger holds the full number.
    cards: dict[int, str] = {}
    for job_id, number in db.execute(
        select(Order.job_id, func.min(Order.card_number))
        .where(Order.job_id.in_(ids), Order.card_number.is_not(None))
        .group_by(Order.job_id)
    ).all():
        digits = "".join(ch for ch in (number or "") if ch.isdigit())
        if len(digits) >= 8:
            cards[job_id] = f"{digits[:2]}XX…{digits[-6:]}"

    # The product's name as an order of the run recorded it; the run itself
    # keeps only the link.
    names: dict[int, str] = {}
    for job_id, model in db.execute(
        select(Order.job_id, func.min(Order.model))
        .where(Order.job_id.in_(ids), Order.model.is_not(None))
        .group_by(Order.job_id)
    ).all():
        if model:
            names[job_id] = model

    def _ids(key: str) -> set[int]:
        return {
            value for job in jobs if isinstance(value := (job.payload or {}).get(key), int)
        }

    address_labels = {
        row.id: row.label
        for row in db.scalars(select(Address).where(Address.id.in_(_ids("address_id")))).all()
    }
    gst_labels = {
        row.id: row.label for row in db.scalars(select(Gst).where(Gst.id.in_(_ids("gst_id")))).all()
    }

    out: list[BulkOrderRunOut] = []
    for job in jobs:
        payload = job.payload or {}
        tally = tallies.get(job.id, {})
        out.append(
            BulkOrderRunOut(
                **JobOut.model_validate(job).model_dump(),
                platform="flipkart",
                mode=str(payload.get("mode") or "real"),
                product_link=payload.get("product_link"),
                product_name=names.get(job.id),
                total_quantity=payload.get("total_quantity"),
                qty_per_order=payload.get("qty_per_order") or payload.get("qty"),
                orders_placed=tally.get("orders", 0),
                units_placed=tally.get("units", 0),
                orders_unconfirmed=tally.get("unconfirmed", 0),
                orders_failed=tally.get("failed", 0),
                parent_card=cards.get(job.id),
                execution_version=payload.get("execution_version"),
                flipkart_minutes=bool(payload.get("flipkart_minutes")),
                amount_placed=round(tally.get("amount", 0.0), 2),
                payment_mode=payload.get("payment_mode"),
                card_type=payload.get("card_type"),
                ref_code=payload.get("ref_code"),
                started_by=payload.get("started_by"),
                address_label=address_labels.get(payload.get("address_id")),
                gst_label=gst_labels.get(payload.get("gst_id")),
                target=payload.get("target"),
            )
        )
    return out


@router.get("/bulk-orders/creators", response_model=list[str])
def bulk_order_creators(db: DbSession = Depends(get_db)) -> list[str]:
    """Who has started order runs: the Orders page's "Created by" filter."""
    names = {
        (payload or {}).get("started_by")
        for payload in db.scalars(select(Job.payload).where(Job.type == JobType.order))
    }
    return sorted(name for name in names if name)


class JobTaskOut(BaseModel):
    id: int
    job_id: int
    job_type: str
    source: str
    email: str | None
    order_id: str | None
    status: str
    message: str | None
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    triggered_by: str | None


class JobTaskPage(BaseModel):
    items: list[JobTaskOut]
    total: int
    page: int
    size: int


@router.get("/tasks", response_model=JobTaskPage)
def list_job_tasks(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    email: str = Query("", max_length=255),
    job_type: str = Query("", max_length=32),
    status: str = Query("", max_length=32),
    error_q: str = Query("", max_length=120),
    db: DbSession = Depends(get_db),
) -> JobTaskPage:
    """Paginated per-account job items for the Order Units -> All tasks view."""
    stmt = select(JobItem, Job, Account.email).join(Job, Job.id == JobItem.job_id).outerjoin(Account, Account.id == JobItem.account_id)
    if email.strip():
        stmt = stmt.where(Account.email.ilike(f"%{email.strip()}%"))
    if job_type.strip():
        try:
            stmt = stmt.where(Job.type == JobType(job_type.strip()))
        except ValueError:
            return JobTaskPage(items=[], total=0, page=page, size=size)
    if status.strip():
        stmt = stmt.where(JobItem.status == status.strip())
    if error_q.strip():
        stmt = stmt.where(JobItem.message.ilike(f"%{error_q.strip()}%"))

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.execute(stmt.order_by(JobItem.id.desc()).offset((page - 1) * size).limit(size)).all()
    items: list[JobTaskOut] = []
    for item, job, acct_email in rows:
        payload = job.payload if isinstance(job.payload, dict) else {}
        items.append(
            JobTaskOut(
                id=item.id,
                job_id=job.id,
                job_type=job.type.value if hasattr(job.type, "value") else str(job.type),
                source=str(payload.get("mode") or payload.get("source") or "manual"),
                email=acct_email,
                order_id=None,
                status=item.status.value if hasattr(item.status, "value") else str(item.status),
                message=item.message,
                created_at=job.started_at,
                started_at=job.started_at,
                finished_at=item.updated_at if str(getattr(item.status, "value", item.status)) in ("done", "failed") else None,
                triggered_by=payload.get("started_by"),
            )
        )
    return JobTaskPage(items=items, total=total, page=page, size=size)


@router.get("", response_model=list[JobOut])
def list_jobs(
    limit: int = Query(50, ge=1, le=500),
    db: DbSession = Depends(get_db),
) -> list[JobOut]:
    """Most recent jobs first."""
    rows = db.scalars(select(Job).order_by(Job.id.desc()).limit(limit)).all()
    return [JobOut.model_validate(row) for row in rows]


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: int, db: DbSession = Depends(get_db)) -> JobDetail:
    """One job plus its per-account rows."""
    job = db.scalar(
        select(Job)
        .where(Job.id == job_id)
        .options(selectinload(Job.items).selectinload(JobItem.account))
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobDetail.from_job(job)


#: A running job whose cancel was already asked and that has shown no progress
#: for this long is taken to have no engine left (a worker that died with it,
#: a broker that dropped it): a second cancel then closes the row by hand. The
#: same span as the worker's own job timeout.
STALE_RUN_SECONDS = 60 * 60


def _last_activity(db: DbSession, job: Job):
    latest = db.scalar(select(func.max(JobItem.updated_at)).where(JobItem.job_id == job.id))
    return latest or job.started_at


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: int, db: DbSession = Depends(get_db)) -> JobOut:
    """Ask a running job to stop.

    Cooperative by design: we only raise the flag. The engine checks it between
    accounts so an in-flight browser step is never killed half-way through a
    payment — see PLAN.md §6a, where a partially-confirmed order is the one
    outcome worth avoiding.

    Two cases are closed outright, because there is nothing to stop and the
    open row would otherwise refuse every later run of its type
    (``_refuse_if_active``): a job still ``queued`` — nothing has started, so a
    cancel is a withdrawal — and a ``running`` job whose cancel was already
    asked and that has made no progress for :data:`STALE_RUN_SECONDS` — its
    engine is gone, and a second cancel is the operator saying so.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status == JobStatus.queued:
        job.status = JobStatus.failed
        job.cancel_requested = True
        job.finished_at = utcnow()
        job.log = f"{job.log or ''}\nCancelled before it started.".strip()
    elif job.status == JobStatus.running and job.cancel_requested:
        idle = (utcnow().replace(tzinfo=None) - _last_activity(db, job).replace(tzinfo=None)).total_seconds()
        if idle >= STALE_RUN_SECONDS:
            job.status = JobStatus.failed
            job.finished_at = utcnow()
            job.log = (
                f"{job.log or ''}\nCancelled by hand after {int(idle // 60)} minutes without progress; "
                "the run's process was no longer answering."
            ).strip()
    else:
        job.cancel_requested = True
    db.commit()
    db.refresh(job)
    return JobOut.model_validate(job)
