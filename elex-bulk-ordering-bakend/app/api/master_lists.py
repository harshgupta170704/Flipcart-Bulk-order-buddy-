"""Two setup lists of Platform Accounts & Configs: Gift Vouchers and Order
Phone Numbers.

**Gift vouchers** are kept as a register — a label, the last four characters
of the code, the face value and the balance left, and the account it is
assigned to. The full code and its PIN are never stored, for the same reason
card numbers are not (PLAN.md §5): a stolen database must not be spendable.

**Order phone numbers** are the 10-digit numbers a run may put on the delivery
address in turn (the order form's "Address Phone Numbers" can take them from
here instead of being typed each time).
"""

from __future__ import annotations

import re
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core.security import require_user
from app.db.base import get_db
from app.db.models import Account, GiftVoucher, OrderPhone

gift_router = APIRouter(prefix="/gift-vouchers", tags=["gift vouchers"], dependencies=[Depends(require_user)])
phone_router = APIRouter(prefix="/order-phones", tags=["order phones"], dependencies=[Depends(require_user)])

_PHONE = re.compile(r"^\d{10}$")


# --- gift vouchers ------------------------------------------------------------


class GiftVoucherIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=64)
    #: Only the last four characters are kept, whatever is sent.
    code: str | None = Field(default=None, max_length=64)
    value: float = Field(ge=0, le=10_000_000)
    balance: float | None = Field(default=None, ge=0, le=10_000_000)
    account_email: str | None = Field(default=None, max_length=255)
    expires_on: date | None = None


class GiftVoucherPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=64)
    balance: float | None = Field(default=None, ge=0, le=10_000_000)
    account_email: str | None = Field(default=None, max_length=255)
    expires_on: date | None = None


class GiftVoucherOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    code_last4: str | None
    value: float
    balance: float
    account_id: int | None
    account_email: str | None = None
    expires_on: date | None
    created_at: datetime


def _account_id(db: DbSession, email: str | None) -> int | None:
    if not email or not email.strip():
        return None
    account = db.scalar(select(Account).where(func.lower(Account.email) == email.strip().lower()))
    if account is None:
        raise HTTPException(status_code=422, detail=f"No account has the email {email!r}.")
    return account.id


def _voucher_out(db: DbSession, row: GiftVoucher) -> GiftVoucherOut:
    out = GiftVoucherOut.model_validate(row)
    if row.account_id is not None:
        account = db.get(Account, row.account_id)
        out.account_email = account.email if account else None
    return out


@gift_router.get("", response_model=list[GiftVoucherOut])
def list_gift_vouchers(db: DbSession = Depends(get_db)) -> list[GiftVoucherOut]:
    return [_voucher_out(db, row) for row in db.scalars(select(GiftVoucher).order_by(GiftVoucher.id.desc()))]


@gift_router.post("", response_model=GiftVoucherOut, status_code=201)
def add_gift_voucher(payload: GiftVoucherIn, db: DbSession = Depends(get_db)) -> GiftVoucherOut:
    code = re.sub(r"\s", "", payload.code or "")
    row = GiftVoucher(
        label=payload.label.strip(),
        code_last4=code[-4:] or None,
        value=payload.value,
        balance=payload.value if payload.balance is None else payload.balance,
        account_id=_account_id(db, payload.account_email),
        expires_on=payload.expires_on,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _voucher_out(db, row)


@gift_router.patch("/{voucher_id}", response_model=GiftVoucherOut)
def update_gift_voucher(voucher_id: int, payload: GiftVoucherPatch, db: DbSession = Depends(get_db)) -> GiftVoucherOut:
    row = db.get(GiftVoucher, voucher_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Gift voucher not found")
    sent = payload.model_fields_set
    if "label" in sent and payload.label:
        row.label = payload.label.strip()
    if "balance" in sent and payload.balance is not None:
        row.balance = payload.balance
    if "account_email" in sent:
        row.account_id = _account_id(db, payload.account_email)
    if "expires_on" in sent:
        row.expires_on = payload.expires_on
    db.commit()
    db.refresh(row)
    return _voucher_out(db, row)


@gift_router.delete("/{voucher_id}", status_code=204)
def delete_gift_voucher(voucher_id: int, db: DbSession = Depends(get_db)) -> None:
    row = db.get(GiftVoucher, voucher_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Gift voucher not found")
    db.delete(row)
    db.commit()


# --- order phone numbers ------------------------------------------------------


class OrderPhoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: str = Field(max_length=16)
    label: str | None = Field(default=None, max_length=64)
    active: bool = True

    @field_validator("number")
    @classmethod
    def _ten_digits(cls, value: str) -> str:
        digits = re.sub(r"\D", "", value)
        if digits.startswith("91") and len(digits) == 12:
            digits = digits[2:]
        if not _PHONE.match(digits):
            raise ValueError("a phone number is 10 digits, without the country code")
        return digits


class OrderPhonePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, max_length=64)
    active: bool | None = None


class OrderPhoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number: str
    label: str | None
    active: bool
    created_at: datetime


@phone_router.get("", response_model=list[OrderPhoneOut])
def list_order_phones(db: DbSession = Depends(get_db)) -> list[OrderPhone]:
    return list(db.scalars(select(OrderPhone).order_by(OrderPhone.id)))


@phone_router.post("", response_model=OrderPhoneOut, status_code=201)
def add_order_phone(payload: OrderPhoneIn, db: DbSession = Depends(get_db)) -> OrderPhone:
    if db.scalar(select(OrderPhone).where(OrderPhone.number == payload.number)):
        raise HTTPException(status_code=409, detail=f"{payload.number} is already on the list.")
    row = OrderPhone(number=payload.number, label=(payload.label or "").strip() or None, active=payload.active)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@phone_router.patch("/{phone_id}", response_model=OrderPhoneOut)
def update_order_phone(phone_id: int, payload: OrderPhonePatch, db: DbSession = Depends(get_db)) -> OrderPhone:
    row = db.get(OrderPhone, phone_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Phone number not found")
    if "label" in payload.model_fields_set:
        row.label = (payload.label or "").strip() or None
    if payload.active is not None:
        row.active = payload.active
    db.commit()
    db.refresh(row)
    return row


@phone_router.delete("/{phone_id}", status_code=204)
def delete_order_phone(phone_id: int, db: DbSession = Depends(get_db)) -> None:
    row = db.get(OrderPhone, phone_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Phone number not found")
    db.delete(row)
    db.commit()
