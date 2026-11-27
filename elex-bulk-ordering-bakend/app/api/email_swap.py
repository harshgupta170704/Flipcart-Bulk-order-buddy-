"""Swap Email Address API — bulk-change Flipkart login emails from a CSV.

Upload a CSV whose first column is the account's current (old) login email and
second column is the new email to move it to — or pick one account on the page
(``/check`` then ``/one``). Each pair becomes an
``email_swaps`` row (status ``pending``). A single background worker then, one
row at a time, signs the account in with its stored session, drives Flipkart's
profile email change, and fills the verification code(s) the modal asks for —
both codes go to mailboxes the OTP reader already reads for these accounts
(@catbar.net / @apmail.org), so no phone is involved.

On success the account's own ``email`` is updated to the new address so the rest
of the system keeps working against it; the session cookies stay valid (Flipkart
does not sign the account out on an email change).
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.api.products import is_flipkart_link  # noqa: F401  (kept for parity; unused directly)
from app.automation.email_swap import run_email_swap
from app.automation.otp import RoutedOtpReader, get_otp_reader
from app.config import get_settings
from app.core import app_settings, session_store
from app.core.browser import browser_page
from app.core.security import require_user
from app.db.base import SessionLocal, get_db
from app.db.models import Account, EmailSwap, naive_utcnow

log = logging.getLogger("ordering.api.email_swap")

router = APIRouter(prefix="/email-swap", tags=["email-swap"], dependencies=[Depends(require_user)])

_EMAIL_HEADERS = {"old_email", "old", "current", "current_email", "from", "email"}

# One worker at a time; a strong reference so the loop is not garbage-collected.
_worker: asyncio.Task | None = None


# --- schemas -----------------------------------------------------------------

class UploadIn(BaseModel):
    csv: str = Field(..., description="CSV text: col1 = old email, col2 = new email")
    batch: str | None = None


class UploadOut(BaseModel):
    batch: str
    created: int
    skipped: int
    errors: list[str] = Field(default_factory=list)


class SwapRowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch: str | None
    old_email: str
    new_email: str
    status: str
    stage: str | None
    message: str | None
    attempts: int
    created_at: datetime
    updated_at: datetime


class SummaryOut(BaseModel):
    total: int
    pending: int
    running: int
    done: int
    failed: int
    worker_running: bool


# --- CSV parsing -------------------------------------------------------------

def _parse_pairs(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (pairs, errors). First column old email, second new email."""
    pairs: list[tuple[str, str]] = []
    errors: list[str] = []
    reader = csv.reader(io.StringIO(text))
    for line_no, row in enumerate(reader, start=1):
        cells = [c.strip() for c in row if c is not None]
        cells = [c for c in cells if c != ""]
        if len(cells) < 2:
            if cells:
                errors.append(f"line {line_no}: need two columns (old,new) — got {cells!r}")
            continue
        old, new = cells[0], cells[1]
        # Skip a header row like "old_email,new_email".
        if line_no == 1 and (old.lower() in _EMAIL_HEADERS or "@" not in old):
            continue
        if "@" not in old or "@" not in new:
            errors.append(f"line {line_no}: not an email pair — {old!r}, {new!r}")
            continue
        pairs.append((old.lower(), new.lower()))
    return pairs, errors


# --- endpoints ---------------------------------------------------------------

@router.post("/upload", response_model=UploadOut)
def upload_swaps(payload: UploadIn, db: DbSession = Depends(get_db)) -> UploadOut:
    pairs, errors = _parse_pairs(payload.csv or "")
    if not pairs and not errors:
        raise HTTPException(status_code=422, detail="No old,new email pairs found in the CSV.")
    batch = (payload.batch or "").strip() or f"swap-{uuid.uuid4().hex[:8]}"
    created = 0
    skipped = 0
    for old, new in pairs:
        # Skip a pair already queued or in flight for the same old email.
        active = db.scalar(
            select(func.count(EmailSwap.id)).where(
                EmailSwap.old_email == old,
                EmailSwap.status.in_(("pending", "running")),
            )
        )
        if active:
            skipped += 1
            continue
        db.add(EmailSwap(batch=batch, old_email=old, new_email=new, status="pending"))
        created += 1
    db.commit()
    if created:
        _ensure_worker()
    return UploadOut(batch=batch, created=created, skipped=skipped, errors=errors[:50])


class AddressCheck(BaseModel):
    email: str
    #: Which inbox reads this address's codes ("InstAddr vikramten (m.kuku.lu)",
    #: "Microsoft 365 prem@askcaayush.com (every @askcaayush.com account)", …).
    inbox: str | None = None
    readable: bool
    problem: str | None = None


class SwapCheckOut(BaseModel):
    account_id: int | None
    account_status: str | None
    has_session: bool
    old: AddressCheck
    new: AddressCheck | None = None
    #: Another account already logs in with the new email.
    new_in_use: bool = False
    #: A swap for this account is already pending or running.
    already_queued: bool = False
    ok: bool
    reasons: list[str] = Field(default_factory=list)


class SwapOneIn(BaseModel):
    old_email: str = Field(..., min_length=3, max_length=255)
    new_email: str = Field(..., min_length=3, max_length=255)


async def _check_address(reader, address: str) -> AddressCheck:
    """Which inbox serves ``address`` and whether it can really be read now —
    the same check the worker makes before asking Flipkart for codes."""
    label = None
    if isinstance(reader, RoutedOtpReader):
        _, label = reader.reader_for(address)
    problem = await reader.probe(address)
    return AddressCheck(email=address, inbox=label, readable=problem is None, problem=problem)


async def _swap_check(db: DbSession, old_email: str, new_email: str | None) -> SwapCheckOut:
    old_email = old_email.strip().lower()
    new_email = (new_email or "").strip().lower() or None
    reader = get_otp_reader(get_settings())
    account = db.scalar(select(Account).where(func.lower(Account.email) == old_email))
    reasons: list[str] = []
    has_session = bool(account and session_store.stored_cookies(db, account))
    if account is None:
        reasons.append(f"no account has the email {old_email}")
    elif not has_session:
        reasons.append("the account has no saved Flipkart session — sign it in first")

    old = await _check_address(reader, old_email)
    if not old.readable:
        reasons.append(f"the code for the old address cannot be read: {old.problem}")

    new = None
    new_in_use = False
    if new_email:
        if "@" not in new_email:
            reasons.append("the new email is not an email address")
        elif new_email == old_email:
            reasons.append("the new email is the same as the old one")
        new_in_use = bool(
            db.scalar(select(func.count(Account.id)).where(func.lower(Account.email) == new_email))
        )
        if new_in_use:
            reasons.append(f"another account already uses {new_email}")
        new = await _check_address(reader, new_email)
        if not new.readable:
            reasons.append(f"the code for the new address cannot be read: {new.problem}")
    else:
        reasons.append("enter the new email")

    already_queued = bool(
        db.scalar(
            select(func.count(EmailSwap.id)).where(
                EmailSwap.old_email == old_email, EmailSwap.status.in_(("pending", "running"))
            )
        )
    )
    if already_queued:
        reasons.append("a swap for this account is already queued or running")
    return SwapCheckOut(
        account_id=account.id if account else None,
        account_status=(account.status.value if hasattr(account.status, "value") else str(account.status))
        if account
        else None,
        has_session=has_session,
        old=old,
        new=new,
        new_in_use=new_in_use,
        already_queued=already_queued,
        ok=not reasons,
        reasons=reasons,
    )


@router.get("/check", response_model=SwapCheckOut)
async def check_swap(old_email: str, new_email: str | None = None, db: DbSession = Depends(get_db)) -> SwapCheckOut:
    """Before swapping one account: does it exist with a session, is the new
    email free, and can BOTH codes be read (the inbox is really opened)?"""
    return await _swap_check(db, old_email, new_email)


@router.post("/one", response_model=SwapRowOut)
async def swap_one(payload: SwapOneIn, db: DbSession = Depends(get_db)) -> SwapRowOut:
    """Swap a single account picked on the page. Refused, with every reason,
    unless the same checks as ``/check`` pass — so a click never asks
    Flipkart for a code nobody can read."""
    check = await _swap_check(db, payload.old_email, payload.new_email)
    if not check.ok:
        raise HTTPException(status_code=422, detail="; ".join(check.reasons))
    row = EmailSwap(
        batch=f"single-{uuid.uuid4().hex[:8]}",
        old_email=payload.old_email.strip().lower(),
        new_email=payload.new_email.strip().lower(),
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    _ensure_worker()
    return SwapRowOut.model_validate(row)


@router.get("/items", response_model=list[SwapRowOut])
def list_swaps(batch: str | None = None, limit: int = 500, db: DbSession = Depends(get_db)) -> list[SwapRowOut]:
    q = select(EmailSwap).order_by(EmailSwap.id.desc())
    if batch:
        q = q.where(EmailSwap.batch == batch)
    rows = db.scalars(q.limit(max(1, min(limit, 2000)))).all()
    return [SwapRowOut.model_validate(r) for r in rows]


@router.get("/summary", response_model=SummaryOut)
def swap_summary(db: DbSession = Depends(get_db)) -> SummaryOut:
    def n(status: str | None = None) -> int:
        q = select(func.count(EmailSwap.id))
        if status:
            q = q.where(EmailSwap.status == status)
        return int(db.scalar(q) or 0)

    return SummaryOut(
        total=n(), pending=n("pending"), running=n("running"),
        done=n("done"), failed=n("failed"), worker_running=_worker_alive(),
    )


@router.post("/run", response_model=SummaryOut)
def run_pending(db: DbSession = Depends(get_db)) -> SummaryOut:
    """Kick the worker (e.g. to retry after sessions were refreshed)."""
    _ensure_worker()
    return swap_summary(db)


@router.post("/retry-failed", response_model=SummaryOut)
def retry_failed(db: DbSession = Depends(get_db)) -> SummaryOut:
    rows = db.scalars(select(EmailSwap).where(EmailSwap.status == "failed")).all()
    for r in rows:
        r.status = "pending"
    db.commit()
    if rows:
        _ensure_worker()
    return swap_summary(db)


# --- background worker -------------------------------------------------------

def _worker_alive() -> bool:
    return _worker is not None and not _worker.done()


def _ensure_worker() -> None:
    global _worker
    if _worker_alive():
        return
    try:
        _worker = asyncio.get_running_loop().create_task(_drain_pending())
    except RuntimeError:
        # No running loop (e.g. called from a sync test) — nothing to start.
        _worker = None


async def _process_one(swap_id: int) -> None:
    settings = get_settings()
    reader = get_otp_reader(settings)
    with SessionLocal() as db:
        swap = db.get(EmailSwap, swap_id)
        if swap is None:
            return
        swap.status = "running"
        swap.attempts += 1
        db.commit()
        old_email, new_email = swap.old_email, swap.new_email

        account = db.scalar(select(Account).where(Account.email == old_email))
        if account is None:
            swap.status, swap.stage = "failed", "no_account"
            swap.message = f"no account found with email {old_email}"
            db.commit()
            return
        cookies = session_store.stored_cookies(db, account)
        if not cookies:
            swap.status, swap.stage = "failed", "no_session"
            swap.message = "account has no stored Flipkart session — sign it in first"
            db.commit()
            return
        proxy = str(app_settings.get_effective(db).get("proxy_server") or "") or None
        account_id = account.id

    # Flipkart mails a code to BOTH addresses. Check each can really be read
    # before asking for them: a code nobody can read only wastes the attempt.
    problems = [
        f"{which} {address}: {problem}"
        for which, address in (("old", old_email), ("new", new_email))
        if (problem := await reader.probe(address))
    ]
    if problems:
        with SessionLocal() as db:
            swap = db.get(EmailSwap, swap_id)
            if swap:
                swap.status, swap.stage = "failed", "otp_unreadable"
                swap.message = ("cannot read the code for " + "; ".join(problems))[:500]
                db.commit()
        return

    # Drive the browser outside the DB session.
    try:
        async with browser_page(headless=settings.browser_headless, proxy=proxy, cookies=cookies) as (_ctx, page):
            result = await run_email_swap(
                page,
                old_email=old_email,
                new_email=new_email,
                otp_reader=reader,
                otp_wait=settings.otp_fetch_timeout,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("email-swap %s -> %s crashed: %s", old_email, new_email, exc)
        with SessionLocal() as db:
            swap = db.get(EmailSwap, swap_id)
            if swap:
                swap.status, swap.stage, swap.message = "failed", "error", str(exc)[:500]
                db.commit()
        return

    with SessionLocal() as db:
        swap = db.get(EmailSwap, swap_id)
        if swap is None:
            return
        swap.stage = result.stage
        swap.message = result.message[:500] if result.message else None
        if result.ok:
            swap.status = "done"
            # Point the account at its new login email so the rest of the
            # system keeps working.
            account = db.get(Account, account_id)
            if account is not None and account.email.lower() != new_email.lower():
                account.email = new_email
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    swap.message = (swap.message or "") + " | account email not updated (new email already on another account)"
                    db.commit()
            # Changing the email signs the old Flipkart session out, so the
            # stored cookies are now dead: forget them and mark the account
            # session_invalid, which is the app's cue to sign it back in under
            # the new email (a login run), rather than loading a dead set into
            # order/coupon runs (app.core.session_store.forget_session).
            if result.session_dead and account is not None:
                try:
                    session_store.forget_session(db, account)
                except Exception:  # noqa: BLE001 — never fail the swap over this
                    log.warning("email-swap: could not forget dead session for %s", new_email)
        else:
            swap.status = "failed"
        db.commit()


async def _drain_pending() -> None:
    """Process pending rows one at a time until none remain."""
    while True:
        with SessionLocal() as db:
            nxt = db.scalar(select(EmailSwap).where(EmailSwap.status == "pending").order_by(EmailSwap.id.asc()))
            swap_id = nxt.id if nxt else None
        if swap_id is None:
            return
        try:
            await _process_one(swap_id)
        except Exception:  # noqa: BLE001 — one bad row must not stop the batch
            log.exception("email-swap worker: row %s failed unexpectedly", swap_id)


async def run_email_swap_scheduler(poll_seconds: float = 5.0) -> None:
    """Background loop that drains queued email swaps on the API event loop.

    The ``/upload``, ``/run`` and ``/retry-failed`` endpoints are sync FastAPI
    handlers, so they run in a threadpool with no running event loop and
    ``_ensure_worker`` cannot start a task there. This loop is created on the
    main loop in the app lifespan, so the async swap worker always has a loop
    to run on — uploaded rows get picked up within ``poll_seconds``.
    """
    while True:
        try:
            await asyncio.sleep(poll_seconds)
            with SessionLocal() as db:
                pending = db.scalar(select(EmailSwap).where(EmailSwap.status == "pending").limit(1))
            if pending is not None:
                await _drain_pending()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never kill the API loop
            log.warning("email-swap scheduler tick failed: %s", exc)
