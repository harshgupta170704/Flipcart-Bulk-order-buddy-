"""Employee Phones: the SMS relay phones (app.core.sms_relay).

Signed in (``/otp-phones``): list, add, change, delete a phone; issue it a new
token; read the texts it relayed in the last day.

Public, authorised by the phone's own token (``/sms``): what the phone's SMS
forwarder app posts.

* ``POST /sms/inbound`` — one text. The token comes as ``?token=``, an
  ``X-Phone-Token`` header or ``Authorization: Bearer``. The body may be JSON
  or a form, with the sender as ``from``/``sender``/``phone``/``number``/
  ``address`` and the text as ``text``/``message``/``body``/``msg``/
  ``content``/``sms``: forwarder apps name them differently and let the user
  pick, so all of these are read.
* ``POST /sms/ping`` (or GET) — a heartbeat, for the page's Last Ping.

A wrong or inactive token gets 401 and nothing is kept.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core import sms_relay
from app.core.security import require_user
from app.db.base import get_db
from app.db.models import OtpPhone, SmsMessage, naive_utcnow

router = APIRouter(prefix="/otp-phones", tags=["employee phones"], dependencies=[Depends(require_user)])
public_router = APIRouter(prefix="/sms", tags=["sms relay"])

#: The largest body a forwarder may post; a text is a few hundred bytes.
MAX_INBOUND_BYTES = 16_384

_SENDER_KEYS = ("from", "sender", "phone", "number", "address", "originatingaddress", "fromnumber")
_TEXT_KEYS = ("text", "message", "body", "msg", "content", "sms", "messagebody")


# --- schemas -------------------------------------------------------------------


def _number(value: str) -> str:
    number = sms_relay.normalise_number(value)
    if number is None:
        raise ValueError("a mobile number is 10 digits")
    return number


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


class PhoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=64)
    phone_number: str = Field(min_length=10, max_length=20)
    employee_id: str | None = Field(default=None, max_length=64)
    corporate_id: str | None = Field(default=None, max_length=64)
    active: bool = True

    @field_validator("phone_number")
    @classmethod
    def _num(cls, v: str) -> str:
        return _number(v)

    @field_validator("employee_id", "corporate_id")
    @classmethod
    def _ids(cls, v: str | None) -> str | None:
        return _optional(v)

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("a label is required")
        return v


class PhonePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=64)
    employee_id: str | None = Field(default=None, max_length=64)
    corporate_id: str | None = Field(default=None, max_length=64)
    active: bool | None = None

    @field_validator("employee_id", "corporate_id")
    @classmethod
    def _ids(cls, v: str | None) -> str | None:
        return _optional(v)


class PhoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    phone_number: str
    employee_id: str | None
    corporate_id: str | None
    token_hint: str
    active: bool
    online: bool = False
    last_ping_at: datetime | None
    last_sms_at: datetime | None
    last_sender: str | None
    last_otp: str | None
    last_otp_at: datetime | None
    created_at: datetime


class PhoneCreatedOut(PhoneOut):
    #: Shown once; only its hash is kept.
    token: str
    webhook_path: str
    ping_path: str


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sender: str | None
    body: str
    otp: str | None
    received_at: datetime


# --- signed in -----------------------------------------------------------------


def _assume_online(db: DbSession) -> bool:
    from app.core import app_settings

    return bool(app_settings.get_effective(db).get("employee_phones_assume_online", False))


def _out(phone: OtpPhone, now: datetime | None = None, *, assume_online: bool = False) -> PhoneOut:
    out = PhoneOut.model_validate(phone)
    out.online = phone.active and (assume_online or sms_relay.is_online(phone, now))
    return out


def _with_token(phone: OtpPhone, token: str, *, assume_online: bool = False) -> PhoneCreatedOut:
    return PhoneCreatedOut(
        **_out(phone, assume_online=assume_online).model_dump(),
        token=token,
        webhook_path=f"/api/sms/inbound?token={token}",
        ping_path=f"/api/sms/ping?token={token}",
    )


def _phone(db: DbSession, phone_id: int) -> OtpPhone:
    phone = db.get(OtpPhone, phone_id)
    if phone is None:
        raise HTTPException(status_code=404, detail="No such phone.")
    return phone


class PhoneSettingsOut(BaseModel):
    assume_online: bool


class PhoneSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assume_online: bool


class PhoneBulkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corporate_id: str | None = Field(default=None, max_length=64)
    csv_text: str = Field(min_length=1, max_length=200_000)


class PhoneBulkOut(BaseModel):
    added: list[PhoneCreatedOut]
    updated: int
    skipped: list[str]


@router.get("/settings", response_model=PhoneSettingsOut)
def get_phone_settings(db: DbSession = Depends(get_db)) -> PhoneSettingsOut:
    return PhoneSettingsOut(assume_online=_assume_online(db))


@router.put("/settings", response_model=PhoneSettingsOut)
def set_phone_settings(payload: PhoneSettingsIn, db: DbSession = Depends(get_db)) -> PhoneSettingsOut:
    from app.core import app_settings

    app_settings.set_overrides(db, {"employee_phones_assume_online": payload.assume_online})
    return PhoneSettingsOut(assume_online=payload.assume_online)


@router.post("/bulk", response_model=PhoneBulkOut)
def bulk_import_phones(payload: PhoneBulkIn, db: DbSession = Depends(get_db)) -> PhoneBulkOut:
    """Import employee_id,phone_number rows (matches BOB Configure New Employee IDs CSV)."""
    import csv
    import io

    assume = _assume_online(db)
    corp = _optional(payload.corporate_id)
    reader = csv.reader(io.StringIO(payload.csv_text))
    added: list[PhoneCreatedOut] = []
    updated = 0
    skipped: list[str] = []
    for idx, row in enumerate(reader, 1):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        if idx == 1 and any(h.lower().replace(" ", "_") in ("employee_id", "phone_number", "phone", "mobile") for h in cells):
            continue
        if len(cells) < 2:
            skipped.append(f"Row {idx}: expected employee_id,phone_number")
            continue
        emp_id = _optional(cells[0])
        num = sms_relay.normalise_number(cells[1])
        label = _optional(cells[2]) if len(cells) >= 3 else None
        if not num:
            skipped.append(f"Row {idx}: invalid phone number '{cells[1]}'")
            continue
        existing = db.scalar(select(OtpPhone).where(OtpPhone.phone_number == num))
        if existing is not None:
            if emp_id:
                existing.employee_id = emp_id
            if corp:
                existing.corporate_id = corp
            if label:
                existing.label = label[:64]
            updated += 1
            continue
        token, digest, hint = sms_relay.new_token()
        phone = OtpPhone(
            label=(label or emp_id or f"Phone {num[-4:]}")[:64],
            phone_number=num,
            employee_id=emp_id,
            corporate_id=corp,
            active=True,
            token_hash=digest,
            token_hint=hint,
        )
        db.add(phone)
        db.flush()
        added.append(_with_token(phone, token, assume_online=assume))
    db.commit()
    return PhoneBulkOut(added=added, updated=updated, skipped=skipped)


@router.get("", response_model=list[PhoneOut])
def list_phones(db: DbSession = Depends(get_db)) -> list[PhoneOut]:
    now = naive_utcnow()
    assume = _assume_online(db)
    return [_out(p, now, assume_online=assume) for p in db.scalars(select(OtpPhone).order_by(OtpPhone.label.asc())).all()]


@router.post("", response_model=PhoneCreatedOut, status_code=201)
def add_phone(payload: PhoneIn, db: DbSession = Depends(get_db)) -> PhoneCreatedOut:
    if db.scalar(select(OtpPhone.id).where(OtpPhone.phone_number == payload.phone_number)) is not None:
        raise HTTPException(status_code=409, detail="A phone with this number exists.")
    token, digest, hint = sms_relay.new_token()
    phone = OtpPhone(
        label=payload.label,
        phone_number=payload.phone_number,
        employee_id=payload.employee_id,
        corporate_id=payload.corporate_id,
        active=payload.active,
        token_hash=digest,
        token_hint=hint,
    )
    db.add(phone)
    db.commit()
    db.refresh(phone)
    return _with_token(phone, token, assume_online=_assume_online(db))


@router.patch("/{phone_id}", response_model=PhoneOut)
def update_phone(phone_id: int, payload: PhonePatch, db: DbSession = Depends(get_db)) -> PhoneOut:
    phone = _phone(db, phone_id)
    sent = payload.model_fields_set
    if "label" in sent and payload.label and payload.label.strip():
        phone.label = payload.label.strip()
    if "employee_id" in sent:
        phone.employee_id = payload.employee_id
    if "corporate_id" in sent:
        phone.corporate_id = payload.corporate_id
    if "active" in sent and payload.active is not None:
        phone.active = payload.active
    db.commit()
    db.refresh(phone)
    return _out(phone, assume_online=_assume_online(db))


@router.post("/{phone_id}/token", response_model=PhoneCreatedOut)
def new_phone_token(phone_id: int, db: DbSession = Depends(get_db)) -> PhoneCreatedOut:
    """A new token for the phone; the old one stops working at once."""
    phone = _phone(db, phone_id)
    token, digest, hint = sms_relay.new_token()
    phone.token_hash = digest
    phone.token_hint = hint
    db.commit()
    db.refresh(phone)
    return _with_token(phone, token)


@router.delete("/{phone_id}", status_code=204)
def delete_phone(phone_id: int, db: DbSession = Depends(get_db)) -> Response:
    phone = _phone(db, phone_id)
    db.query(SmsMessage).filter(SmsMessage.phone_id == phone.id).delete()
    db.delete(phone)
    db.commit()
    return Response(status_code=204)


@router.get("/{phone_id}/messages", response_model=list[MessageOut])
def phone_messages(phone_id: int, db: DbSession = Depends(get_db)) -> list[MessageOut]:
    phone = _phone(db, phone_id)
    since = naive_utcnow() - timedelta(hours=sms_relay.KEEP_HOURS)
    rows = db.scalars(
        select(SmsMessage)
        .where(SmsMessage.phone_id == phone.id)
        .where(SmsMessage.received_at >= since)
        .order_by(SmsMessage.received_at.desc(), SmsMessage.id.desc())
        .limit(100)
    ).all()
    return [MessageOut.model_validate(r) for r in rows]


# --- public: what the phone posts ----------------------------------------------


def _token_from(query_token: str | None, header_token: str | None, authorization: str | None) -> str | None:
    if query_token:
        return query_token
    if header_token:
        return header_token
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _authorised(db: DbSession, token: str | None) -> OtpPhone:
    phone = sms_relay.phone_for_token(db, token)
    if phone is None:
        raise HTTPException(status_code=401, detail="Unknown or inactive phone token.")
    return phone


def _fields(raw: bytes, content_type: str) -> dict[str, str]:
    """The posted fields, lower-cased keys, from JSON or a form; a bare text
    body is the message itself."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    data: object = None
    if "json" in content_type or text[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        flat: dict[str, str] = {}
        for key, value in data.items():
            if isinstance(value, dict):  # {"message": {"from": .., "text": ..}}
                for k, v in value.items():
                    flat.setdefault(str(k).lower().replace("_", ""), "" if v is None else str(v))
            else:
                flat[str(key).lower().replace("_", "")] = "" if value is None else str(value)
        return flat
    if "form" in content_type or ("=" in text and "\n" not in text):
        parsed = parse_qs(text, keep_blank_values=True)
        if parsed:
            return {k.lower().replace("_", ""): v[0] for k, v in parsed.items()}
    return {"text": text}


#: GawkAr "SMS forwarder"'s default URL template wraps each text as
#: "%t - %s (%c) <br/>Message: %m (%d)" (time, sender, contact, text, date).
_WRAPPED = re.compile(
    r"^.*? - (?P<sender>[^()]+?) \([^()]*\)\s*<br\s*/?>\s*Message:\s*(?P<text>.*?)\s*\([^()]*\)\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _unwrap(sender: str | None, body: str) -> tuple[str | None, str]:
    """The sender and the text itself, out of a forwarder's wrapper when it has one."""
    match = _WRAPPED.match(body.strip())
    if match is None:
        return sender, body
    return sender or match.group("sender").strip(), match.group("text")


def _first(fields: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = fields.get(key)
        if value is not None and value.strip():
            return value
    return None


@public_router.post("/inbound")
async def sms_inbound(
    request: Request,
    token: str | None = Query(None, max_length=200),
    x_phone_token: str | None = Header(None, max_length=200),
    authorization: str | None = Header(None, max_length=300),
    db: DbSession = Depends(get_db),
) -> dict:
    phone = _authorised(db, _token_from(token, x_phone_token, authorization))
    raw = await request.body()
    if len(raw) > MAX_INBOUND_BYTES:
        raise HTTPException(status_code=413, detail="A text is a few hundred bytes.")
    fields = _fields(raw, request.headers.get("content-type", "").lower())
    body = _first(fields, _TEXT_KEYS)
    if body is None:
        raise HTTPException(status_code=422, detail="No message text in the post (send it as 'text' or 'message').")
    sender, body = _unwrap(_first(fields, _SENDER_KEYS), body)
    relayed = sms_relay.receive(db, phone, sender=sender, body=body)
    return {"ok": True, "otp_found": relayed.otp_found}


@public_router.api_route("/ping", methods=["GET", "POST"])
def sms_ping(
    token: str | None = Query(None, max_length=200),
    x_phone_token: str | None = Header(None, max_length=200),
    authorization: str | None = Header(None, max_length=300),
    db: DbSession = Depends(get_db),
) -> dict:
    phone = _authorised(db, _token_from(token, x_phone_token, authorization))
    sms_relay.ping(db, phone)
    return {"ok": True}
