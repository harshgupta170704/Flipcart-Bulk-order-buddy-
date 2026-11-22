"""CorpAssist (HDFC) API: user ids, Fetch, and the child cards read from email.

``/hdfc/user-ids``     list, add, change, delete the HDFC user ids.
``/hdfc/fetch``        read the child-card emails of the chosen (or every
                       active) user id from the shared inbox. Read-only on
                       the inbox; nothing is sent to HDFC.
``/hdfc/child-cards``  the cards with opening, used and remaining amounts,
                       filtered; cancel or delete chosen ones; ``.csv`` to
                       download them.

A child card's number is shown in full, as the ledger shows the number an
order was paid with (the owner's decisions of 25 Oct and 19 Nov 2026). No
expiry or CVV exists to show.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.core import corpassist
from app.core.card_pool import digits
from app.core.security import require_user
from app.db.base import get_db
from app.db.models import HdfcChildCard, HdfcUserId
from app.io.orders_csv import IST, to_ist

router = APIRouter(prefix="/hdfc", tags=["corpassist"], dependencies=[Depends(require_user)])

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
    active: bool = True

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
    active: bool | None = None

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
