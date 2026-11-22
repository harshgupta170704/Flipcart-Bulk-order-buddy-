"""CorpAssist (HDFC) API: user ids, Fetch, and the child cards read from email.

``/hdfc/user-ids``     list, add, change, delete the HDFC user ids.
``/hdfc/fetch``        read the child-card emails of the chosen (or every
                       active) user id from the shared inbox. Read-only on
                       the inbox; nothing is sent to HDFC.
``/hdfc/child-cards``  the cards with opening, used and remaining amounts,
                       filtered; cancel or delete chosen ones; ``.csv`` to
                       download them.
``/hdfc/batches``      ask HDFC for new virtual cards (app.core.hdfc_batches):
                       sent to the CorpAssist portal, or HDFC's bulk sheet to
                       upload by hand; then the inbox is watched for them.
``/hdfc/portal``       whether the portal is set up; test its sign-in; read
                       its Virtual Card Users in as user ids.

A child card's number is shown in full, as the ledger shows the number an
order was paid with (the owner's decisions of 25 Oct and 19 Nov 2026). No
expiry or CVV exists to show.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.automation import hdfc_portal
from app.config import get_settings
from app.core import corpassist, hdfc_batches
from app.core.card_pool import digits
from app.core.security import require_user
from app.db.base import SessionLocal, get_db
from app.db.models import HdfcCardBatch, HdfcChildCard, HdfcUserId
from app.io import hdfc_bulk_csv
from app.io.orders_csv import IST, to_ist

log = logging.getLogger("ordering.corpassist")

router = APIRouter(prefix="/hdfc", tags=["corpassist"], dependencies=[Depends(require_user)])

#: Portal work running detached from its request (a batch being sent).
_TASKS: set[asyncio.Task] = set()


def _portal() -> hdfc_portal.Portal:
    """The CorpAssist portal; tests put a fake in its place."""
    return hdfc_portal.portal(session_factory=SessionLocal)


def _session_factory():
    return SessionLocal

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MOBILE = re.compile(r"^\d{10}$")


# --- schemas -------------------------------------------------------------------


def _last4(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    tail = digits(value)[-4:]
    if len(tail) != 4:
        raise ValueError("the parent card needs at least its last four digits")
    return tail


def _aan(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    number = digits(value)
    if not 8 <= len(number) <= 32:
        raise ValueError("the Aan is the parent card's Alternate A/c No.: 8 to 32 digits")
    return number


def _portal_id(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if len(value) > 16:
        raise ValueError("HDFC's Virtual Card User ID is at most 16 characters")
    return value


def _mobile(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    number = digits(value)
    if len(number) == 12 and number.startswith("91"):
        number = number[2:]
    if not _MOBILE.match(number):
        raise ValueError("a mobile number is 10 digits")
    return number


class UserIdIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=255)
    mobile: str | None = Field(default=None, max_length=20)
    #: Only the last four digits are kept, whatever is sent.
    parent_card: str | None = Field(default=None, max_length=32)
    #: The parent card's Alternate A/c No. (HDFC's bulk sheet's Aan).
    aan: str | None = Field(default=None, max_length=40)
    portal_user_id: str | None = Field(default=None, max_length=16)
    active: bool = True

    @field_validator("aan")
    @classmethod
    def _aan_ok(cls, v: str | None) -> str | None:
        return _aan(v)

    @field_validator("portal_user_id")
    @classmethod
    def _portal_ok(cls, v: str | None) -> str | None:
        return _portal_id(v)

    @field_validator("username")
    @classmethod
    def _username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("a username is required")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("not an email address")
        return v

    @field_validator("mobile")
    @classmethod
    def _mobile_ok(cls, v: str | None) -> str | None:
        return _mobile(v)

    @field_validator("parent_card")
    @classmethod
    def _parent_ok(cls, v: str | None) -> str | None:
        return _last4(v)


class UserIdPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, min_length=3, max_length=255)
    mobile: str | None = Field(default=None, max_length=20)
    parent_card: str | None = Field(default=None, max_length=32)
    aan: str | None = Field(default=None, max_length=40)
    portal_user_id: str | None = Field(default=None, max_length=16)
    active: bool | None = None

    @field_validator("aan")
    @classmethod
    def _aan_ok(cls, v: str | None) -> str | None:
        return _aan(v)

    @field_validator("portal_user_id")
    @classmethod
    def _portal_ok(cls, v: str | None) -> str | None:
        return _portal_id(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("not an email address")
        return v

    @field_validator("mobile")
    @classmethod
    def _mobile_ok(cls, v: str | None) -> str | None:
        return _mobile(v)

    @field_validator("parent_card")
    @classmethod
    def _parent_ok(cls, v: str | None) -> str | None:
        return _last4(v)


class UserIdOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    mobile: str | None
    parent_card_last4: str | None
    aan: str | None = None
    portal_user_id: str | None = None
    active: bool
    last_fetched_at: datetime | None
    last_error: str | None
    card_count: int = 0
    created_at: datetime


class FetchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The user ids to fetch; left out, every active one.
    user_ids: list[int] | None = Field(default=None, max_length=500)


class FetchResultOut(BaseModel):
    user_id: int
    username: str
    emails: int
    added: int
    error: str | None


class ChildCardOut(BaseModel):
    id: int
    user_id: int
    username: str
    email: str
    card_number: str
    parent_card_last4: str | None
    opening_amount: float | None
    used: float
    remaining: float | None
    status: str
    error: str | None
    issued_at: datetime | None
    created_at: datetime


class CardsActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[int] = Field(min_length=1, max_length=5000)
    #: "cancel", "activate" or "delete".
    action: str

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        if v not in ("cancel", "activate", "delete"):
            raise ValueError("action is cancel, activate or delete")
        return v


class CardsActionOut(BaseModel):
    changed: int


# --- user ids ------------------------------------------------------------------


def _user_out(user: HdfcUserId, cards: int) -> UserIdOut:
    out = UserIdOut.model_validate(user)
    out.card_count = cards
    return out


def _card_counts(db: DbSession) -> dict[int, int]:
    rows = db.execute(select(HdfcChildCard.user_id, func.count()).group_by(HdfcChildCard.user_id)).all()
    return {int(uid): int(n) for uid, n in rows}


def _clash(db: DbSession, *, username: str | None, email: str | None, exclude: int | None = None) -> None:
    for column, value, what in ((HdfcUserId.username, username, "username"), (HdfcUserId.email, email, "email")):
        if value is None:
            continue
        stmt = select(HdfcUserId.id).where(func.lower(column) == value.lower())
        if exclude is not None:
            stmt = stmt.where(HdfcUserId.id != exclude)
        if db.scalar(stmt) is not None:
            raise HTTPException(status_code=409, detail=f"A user id with this {what} exists.")


def _user(db: DbSession, user_id: int) -> HdfcUserId:
    user = db.get(HdfcUserId, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="No such user id.")
    return user


@router.get("/user-ids", response_model=list[UserIdOut])
def list_user_ids(db: DbSession = Depends(get_db)) -> list[UserIdOut]:
    counts = _card_counts(db)
    users = db.scalars(select(HdfcUserId).order_by(HdfcUserId.username.asc())).all()
    return [_user_out(u, counts.get(u.id, 0)) for u in users]


@router.post("/user-ids", response_model=UserIdOut, status_code=201)
def add_user_id(payload: UserIdIn, db: DbSession = Depends(get_db)) -> UserIdOut:
    _clash(db, username=payload.username, email=payload.email)
    user = HdfcUserId(
        username=payload.username,
        email=payload.email,
        mobile=payload.mobile,
        parent_card_last4=payload.parent_card,
        aan=payload.aan,
        portal_user_id=payload.portal_user_id,
        active=payload.active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_out(user, 0)


@router.patch("/user-ids/{user_id}", response_model=UserIdOut)
def update_user_id(user_id: int, payload: UserIdPatch, db: DbSession = Depends(get_db)) -> UserIdOut:
    user = _user(db, user_id)
    sent = payload.model_fields_set
    if "username" in sent and payload.username is not None:
        payload.username = payload.username.strip()
    _clash(
        db,
        username=payload.username if "username" in sent else None,
        email=payload.email if "email" in sent else None,
        exclude=user.id,
    )
    if "username" in sent and payload.username:
        user.username = payload.username
    if "email" in sent and payload.email:
        user.email = payload.email
    if "mobile" in sent:
        user.mobile = payload.mobile
    if "parent_card" in sent:
        user.parent_card_last4 = payload.parent_card
    if "aan" in sent:
        user.aan = payload.aan
    if "portal_user_id" in sent:
        user.portal_user_id = payload.portal_user_id
    if "active" in sent and payload.active is not None:
        user.active = payload.active
    db.commit()
    db.refresh(user)
    return _user_out(user, _card_counts(db).get(user.id, 0))


@router.delete("/user-ids/{user_id}", status_code=204)
def delete_user_id(user_id: int, db: DbSession = Depends(get_db)) -> Response:
    """Deletes the user id and the child cards read for it."""
    user = _user(db, user_id)
    db.execute(delete(HdfcChildCard).where(HdfcChildCard.user_id == user.id))
    db.delete(user)
    db.commit()
    return Response(status_code=204)


@router.post("/fetch", response_model=list[FetchResultOut])
def fetch_cards(payload: FetchIn, db: DbSession = Depends(get_db)) -> list[FetchResultOut]:
    """Read the child-card emails of the chosen user ids (or every active one)."""
    stmt = select(HdfcUserId).order_by(HdfcUserId.username.asc())
    if payload.user_ids is not None:
        stmt = stmt.where(HdfcUserId.id.in_(payload.user_ids))
    else:
        stmt = stmt.where(HdfcUserId.active.is_(True))
    users = list(db.scalars(stmt).all())
    if not users:
        raise HTTPException(status_code=422, detail="No user id to fetch.")
    try:
        source = corpassist.mail_source(db)
    except corpassist.InboxNotConfigured as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    results = corpassist.fetch(db, users, source)
    return [FetchResultOut(**r.__dict__) for r in results]


# --- child cards ---------------------------------------------------------------


def _utc_start_of(day: date) -> datetime:
    """The naive UTC moment India's ``day`` begins."""
    return datetime.combine(day, time.min, tzinfo=IST).astimezone(timezone.utc).replace(tzinfo=None)


def _cards(
    db: DbSession,
    *,
    user_id: int | None,
    status: str | None,
    min_remaining: float | None,
    created_from: date | None,
    created_to: date | None,
    search: str | None,
) -> list[ChildCardOut]:
    stmt = (
        select(HdfcChildCard, HdfcUserId)
        .join(HdfcUserId, HdfcUserId.id == HdfcChildCard.user_id)
        .order_by(HdfcChildCard.created_at.desc(), HdfcChildCard.id.desc())
    )
    if user_id is not None:
        stmt = stmt.where(HdfcChildCard.user_id == user_id)
    if status:
        stmt = stmt.where(HdfcChildCard.status == status)
    # The dates are India's days, as the page shows times; created_at is naive UTC.
    if created_from:
        stmt = stmt.where(HdfcChildCard.created_at >= _utc_start_of(created_from))
    if created_to:
        stmt = stmt.where(HdfcChildCard.created_at < _utc_start_of(created_to + timedelta(days=1)))
    if search and search.strip():
        term = search.strip().lower()
        number = digits(term)
        conditions = [func.lower(HdfcUserId.username).contains(term), func.lower(HdfcUserId.email).contains(term)]
        if number:
            conditions.append(HdfcChildCard.card_number.contains(number))
        stmt = stmt.where(or_(*conditions))
    rows = db.execute(stmt).all()
    used = corpassist.used_by_number(db, [card.card_number for card, _ in rows])
    out: list[ChildCardOut] = []
    for card, user in rows:
        spent = round(used.get(card.card_number, 0.0), 2)
        remaining = None if card.opening_amount is None else round(card.opening_amount - spent, 2)
        if min_remaining is not None and (remaining is None or remaining < min_remaining):
            continue
        out.append(
            ChildCardOut(
                id=card.id,
                user_id=user.id,
                username=user.username,
                email=user.email,
                card_number=card.card_number,
                parent_card_last4=card.parent_card_last4,
                opening_amount=card.opening_amount,
                used=spent,
                remaining=remaining,
                status=card.status,
                error=card.error,
                issued_at=card.issued_at,
                created_at=card.created_at,
            )
        )
    return out


def _filters(
    user_id: int | None = Query(None),
    status: str | None = Query(None, pattern="^(active|cancelled)$"),
    min_remaining: float | None = Query(None, ge=0, le=10_000_000),
    created_from: date | None = Query(None),
    created_to: date | None = Query(None),
    search: str | None = Query(None, max_length=64),
) -> dict:
    return dict(
        user_id=user_id,
        status=status,
        min_remaining=min_remaining,
        created_from=created_from,
        created_to=created_to,
        search=search,
    )


@router.get("/child-cards", response_model=list[ChildCardOut])
def list_child_cards(filters: dict = Depends(_filters), db: DbSession = Depends(get_db)) -> list[ChildCardOut]:
    return _cards(db, **filters)


#: The download's columns. Expiry and CVV are not here because they are never kept.
CSV_COLUMNS = ("Username", "Email", "Card Number", "Parent Card", "Opening Amount", "Used", "Remaining", "Status", "Created")


def _guard(cell: str) -> str:
    """No cell may start a spreadsheet formula."""
    return "'" + cell if cell[:1] in ("=", "+", "-", "@", "\t", "\r") else cell


@router.get("/child-cards.csv")
def download_child_cards(filters: dict = Depends(_filters), db: DbSession = Depends(get_db)) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    money = lambda v: "" if v is None else f"{v:.2f}"  # noqa: E731
    for row in _cards(db, **filters):
        writer.writerow(
            [
                _guard(row.username),
                _guard(row.email),
                row.card_number,
                f"XXXX{row.parent_card_last4}" if row.parent_card_last4 else "",
                money(row.opening_amount),
                money(row.used),
                money(row.remaining),
                row.status,
                to_ist(row.created_at).strftime("%Y-%m-%d %H:%M"),
            ]
        )
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="hdfc_child_cards_{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/child-cards/action", response_model=CardsActionOut)
def act_on_child_cards(payload: CardsActionIn, db: DbSession = Depends(get_db)) -> CardsActionOut:
    cards = list(db.scalars(select(HdfcChildCard).where(HdfcChildCard.id.in_(payload.ids))).all())
    if payload.action == "delete":
        for card in cards:
            db.delete(card)
    else:
        status = corpassist.CANCELLED if payload.action == "cancel" else corpassist.ACTIVE
        for card in cards:
            card.status = status
    db.commit()
    return CardsActionOut(changed=len(cards))


# --- card batches --------------------------------------------------------------


class BatchRowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    #: The card's limit, in rupees.
    amount: float = Field(ge=1, le=10_000_000)
    #: Cards for this user id.
    count: int = Field(default=1, ge=1, le=500)
    valid_days: int = Field(default=7, ge=1, le=365)


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[BatchRowIn] = Field(min_length=1, max_length=500)
    #: "portal": sign in to CorpAssist and upload; "download": the sheet only.
    send: str = "portal"

    @field_validator("send")
    @classmethod
    def _send(cls, v: str) -> str:
        if v not in ("portal", "download"):
            raise ValueError("send is portal or download")
        return v


class BatchRowOut(BaseModel):
    user_id: int
    username: str
    amount: float
    count: int
    valid_days: int


class BatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    send: str
    rows: list[BatchRowOut]
    card_count: int
    cards_received: int
    error: str | None
    portal_report: str | None
    started_by: str | None
    created_at: datetime
    submitted_at: datetime | None
    finished_at: datetime | None


def _batch(db: DbSession, batch_id: int) -> HdfcCardBatch:
    batch = db.get(HdfcCardBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="No such batch.")
    return batch


def _send_in_background(batch_id: int) -> None:
    task = asyncio.create_task(
        hdfc_batches.send_to_portal(batch_id, session_factory=_session_factory(), portal_factory=_portal),
        name=f"hdfc-batch-{batch_id}",
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def _effective_settings(db: DbSession):
    from types import SimpleNamespace
    from app.core import app_settings

    base = get_settings()
    if hasattr(type(base), "model_fields"):
        merged = {name: getattr(base, name) for name in type(base).model_fields}
    else:
        merged = dict(vars(base))
    merged.update(app_settings.get_overrides(db))
    return SimpleNamespace(**merged)


def _portal_ready(db: DbSession) -> None:
    config = hdfc_portal.config_from(_effective_settings(db))
    if not config.configured:
        raise HTTPException(
            status_code=409,
            detail="The HDFC portal sign-in is not set up: configure it in Settings or download the sheet instead.",
        )


@router.get("/batches", response_model=list[BatchOut])
def list_batches(db: DbSession = Depends(get_db)) -> list[BatchOut]:
    rows = db.scalars(select(HdfcCardBatch).order_by(HdfcCardBatch.id.desc()).limit(200)).all()
    return [BatchOut.model_validate(b) for b in rows]


@router.post("/batches", response_model=BatchOut, status_code=201)
async def create_batch(
    payload: BatchIn, db: DbSession = Depends(get_db), who: str = Depends(require_user)
) -> BatchOut:
    """New virtual cards: sent to the portal now, or a sheet to download."""
    if payload.send == "portal":
        _portal_ready(db)
    rows = [hdfc_batches.RowIn(r.user_id, r.amount, r.count, r.valid_days) for r in payload.rows]
    try:
        batch = hdfc_batches.create(db, rows, send=payload.send, started_by=who)
    except hdfc_batches.BatchRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if payload.send == "portal":
        _send_in_background(batch.id)
    return BatchOut.model_validate(batch)


@router.get("/batches/{batch_id}.csv")
def download_batch_sheet(batch_id: int, db: DbSession = Depends(get_db)) -> Response:
    """HDFC's bulk sheet for the batch, to upload on "Create Virtual Card (Bulk)"."""
    batch = _batch(db, batch_id)
    try:
        data = hdfc_batches.sheet(db, batch)
    except (hdfc_batches.BatchRefused, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{hdfc_bulk_csv.FILE_NAME}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/batches/{batch_id}", response_model=BatchOut)
def get_batch(batch_id: int, db: DbSession = Depends(get_db)) -> BatchOut:
    return BatchOut.model_validate(_batch(db, batch_id))


@router.post("/batches/{batch_id}/retry", response_model=BatchOut)
async def retry_batch(batch_id: int, db: DbSession = Depends(get_db)) -> BatchOut:
    """Send a failed portal batch again."""
    batch = _batch(db, batch_id)
    if batch.send != "portal" or batch.status != hdfc_batches.FAILED:
        raise HTTPException(status_code=409, detail="Only a failed batch sent to the portal can be retried.")
    if batch.cards_received:
        raise HTTPException(
            status_code=409,
            detail="Some of this batch's cards arrived, so the portal made it: make a new batch for the rest.",
        )
    _portal_ready(db)
    batch.status = hdfc_batches.SENDING
    batch.error = None
    batch.portal_report = None
    batch.finished_at = None
    batch.submitted_at = None
    db.commit()
    db.refresh(batch)
    _send_in_background(batch.id)
    return BatchOut.model_validate(batch)


@router.post("/batches/{batch_id}/cancel", response_model=BatchOut)
def cancel_batch(batch_id: int, db: DbSession = Depends(get_db)) -> BatchOut:
    """Stop watching for the batch's cards. Cards HDFC already made stay made."""
    batch = _batch(db, batch_id)
    if batch.status not in hdfc_batches.OPEN:
        raise HTTPException(status_code=409, detail="The batch is already closed.")
    if batch.status == hdfc_batches.SENDING:
        raise HTTPException(status_code=409, detail="The batch is being sent to the portal right now; wait a minute.")
    batch.status = hdfc_batches.CANCELLED
    batch.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.refresh(batch)
    return BatchOut.model_validate(batch)


# --- the portal ----------------------------------------------------------------


class PortalOut(BaseModel):
    configured: bool
    url: str
    #: The portal user, first and last characters only.
    user_hint: str | None
    otp_via: str | None
    otp_ready: bool
    otp_detail: str | None
    auto_fetch_minutes: int
    busy: bool
    last_error: str | None = None


class PortalTestOut(BaseModel):
    ok: bool
    detail: str
    #: The captcha sign-in reached HDFC's OTP box: a person types the code.
    needs_otp: bool | None = None


class SyncOut(BaseModel):
    seen: int
    added: int
    updated: int


def _hint(user: str) -> str | None:
    if not user:
        return None
    return user if len(user) <= 3 else f"{user[:2]}…{user[-1]}"


@router.get("/portal", response_model=PortalOut)
def portal_status(db: DbSession = Depends(get_db)) -> PortalOut:
    from app.core import sms_relay
    from app.db.models import OtpPhone

    settings = _effective_settings(db)
    config = hdfc_portal.config_from(settings)
    assume_online = bool(getattr(settings, "employee_phones_assume_online", False))
    ready, detail = False, None
    if config.otp_mobile:
        number = sms_relay.normalise_number(config.otp_mobile)
        phone = db.scalar(select(OtpPhone).where(OtpPhone.phone_number == number)) if number else None
        if phone is None or not phone.active:
            detail = f"no active Employee Phone is registered for the OTP mobile …{(number or config.otp_mobile)[-2:]}"
        elif not assume_online and not sms_relay.is_online(phone):
            ready = True
            detail = f"the Employee Phone '{phone.label}' has not been heard from in {sms_relay.ONLINE_MINUTES} minutes"
        else:
            ready = True
    elif config.otp_email:
        ready = bool(settings.otp_imap_host and settings.otp_imap_user)
        detail = None if ready else "the OTP goes by email, but no OTP inbox is set up"
    elif config.configured:
        detail = "no OTP source is set: configure HDFC Portal OTP mobile or email in Settings"
    return PortalOut(
        configured=config.configured,
        url=config.url,
        user_hint=_hint(config.user),
        otp_via=config.otp_via,
        otp_ready=ready,
        otp_detail=detail,
        auto_fetch_minutes=int(getattr(settings, "hdfc_auto_fetch_minutes", 0) or 0),
        busy=hdfc_portal.busy(),
    )


@router.post("/portal/test-login", response_model=PortalTestOut, response_model_exclude_none=True)
async def portal_test_login(db: DbSession = Depends(get_db)) -> PortalTestOut:
    _portal_ready(db)
    try:
        detail = await _portal().test_login()
    except hdfc_portal.PortalError as exc:
        return PortalTestOut(ok=False, detail=str(exc))
    return PortalTestOut(ok=True, detail=detail)


class CaptchaOut(BaseModel):
    #: ``data:image/png;base64,...`` of HDFC's captcha.
    image: str
    expires_in: int


class CaptchaIn(BaseModel):
    text: str


@router.post("/portal/captcha", response_model=CaptchaOut)
async def portal_captcha(db: DbSession = Depends(get_db)) -> CaptchaOut:
    """Open HDFC's sign-in page and return its captcha for a person to read.
    The captcha is HDFC's bot check: it is shown, never solved by code."""
    config = hdfc_portal.config_from(_effective_settings(db))
    if not config.configured:
        raise HTTPException(status_code=409, detail="Set the HDFC portal user and password in Settings first.")
    try:
        image = await hdfc_portal.PlaywrightPortal(config, _session_factory()).start_captcha_login()
    except hdfc_portal.PortalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return CaptchaOut(image=image, expires_in=hdfc_portal.CAPTCHA_WAIT_SECONDS)


@router.post("/portal/captcha/submit", response_model=PortalTestOut, response_model_exclude_none=True)
async def portal_captcha_submit(payload: CaptchaIn, db: DbSession = Depends(get_db)) -> PortalTestOut:
    """Sign in with the captcha a person typed; the signed-in session is then
    reused by Test sign-in, Sync from HDFC and Create on HDFC. When HDFC asks
    for an OTP and none can be read, ``needs_otp`` is set and the OTP goes to
    ``/portal/otp/submit``."""
    config = hdfc_portal.config_from(_effective_settings(db))
    try:
        detail = await hdfc_portal.PlaywrightPortal(config, _session_factory()).finish_captcha_login(payload.text)
    except hdfc_portal.PortalNeedsOtp as exc:
        return PortalTestOut(ok=False, detail=str(exc), needs_otp=True)
    except hdfc_portal.PortalError as exc:
        return PortalTestOut(ok=False, detail=str(exc))
    return PortalTestOut(ok=True, detail=detail)


class OtpIn(BaseModel):
    code: str


@router.post("/portal/otp/submit", response_model=PortalTestOut, response_model_exclude_none=True)
async def portal_otp_submit(payload: OtpIn, db: DbSession = Depends(get_db)) -> PortalTestOut:
    """Finish the captcha sign-in with the OTP a person read from HDFC's message."""
    config = hdfc_portal.config_from(_effective_settings(db))
    try:
        detail = await hdfc_portal.PlaywrightPortal(config, _session_factory()).finish_otp_login(payload.code)
    except hdfc_portal.PortalError as exc:
        return PortalTestOut(ok=False, detail=str(exc))
    return PortalTestOut(ok=True, detail=detail)


@router.post("/portal/sync-users", response_model=SyncOut)
async def portal_sync_users(db: DbSession = Depends(get_db)) -> SyncOut:
    """Read the portal's Virtual Card Users in as user ids: a known one (by
    HDFC's user ID, else by username) has its email, mobile and ID brought up
    to date; a new one is added. Nothing on the portal changes."""
    _portal_ready(db)
    try:
        found = await _portal().list_users()
    except hdfc_portal.PortalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    added = updated = 0
    for item in found:
        if not item.email or not _EMAIL.match(item.email):
            continue
        user = None
        if item.portal_user_id:
            user = db.scalar(select(HdfcUserId).where(HdfcUserId.portal_user_id == item.portal_user_id))
        if user is None:
            user = db.scalar(select(HdfcUserId).where(func.lower(HdfcUserId.username) == item.username.lower()))
        clash = db.scalar(select(HdfcUserId).where(func.lower(HdfcUserId.email) == item.email.lower()))
        if user is None:
            if clash is not None:
                continue  # that email belongs to another user id here; leave it to a person
            db.add(
                HdfcUserId(
                    username=item.username.lower(),
                    email=item.email,
                    mobile=item.mobile,
                    portal_user_id=item.portal_user_id or None,
                    active=item.active,
                )
            )
            db.flush()
            added += 1
            continue
        changed = False
        if item.email != user.email and (clash is None or clash.id == user.id):
            user.email, changed = item.email, True
        if item.mobile and item.mobile != user.mobile:
            user.mobile, changed = item.mobile, True
        if item.portal_user_id and item.portal_user_id != user.portal_user_id:
            user.portal_user_id, changed = item.portal_user_id, True
        updated += int(changed)
    db.commit()
    return SyncOut(seen=len(found), added=added, updated=updated)


@router.post("/user-ids/{user_id}/point-email", response_model=UserIdOut)
async def point_user_email(user_id: int, db: DbSession = Depends(get_db)) -> UserIdOut:
    """Set this user's email on the portal to the one here (the shared inbox's
    address), so HDFC mails its cards where Fetch reads them."""
    _portal_ready(db)
    user = _user(db, user_id)
    try:
        await _portal().set_user_email(user.username, user.email)
    except hdfc_portal.PortalError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return _user_out(user, _card_counts(db).get(user.id, 0))
