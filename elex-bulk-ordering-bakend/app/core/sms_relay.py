"""SMS relay (Employee Phones): OTP texts forwarded by phones, and waiting for one.

A phone runs an SMS forwarder app (any app that can POST each incoming text
to a URL: "SMS Forwarder", "SmsForwarder", Tasker, MacroDroid...). Each phone
is registered here with its number and gets a token; the app posts to
``/sms/inbound?token=...`` (app.api.phones) and may post a heartbeat to
``/sms/ping?token=...``. A flow that has just made a bank or portal send an
OTP to a mobile number calls :class:`SmsOtpReader` (or :func:`wait_for_otp`)
with that number and the moment it asked, and gets the code.

What is kept: the token's SHA-256 only; each text for :data:`KEEP_HOURS`
(the page's "Messages", and the OTP wait), then it is deleted; the phone's
last OTP and when, which the Employee Phones page shows. Nothing here logs a
text or a code: a log line names the phone's label and the last two digits of
its number.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.automation.otp import OtpReader
from app.db.models import OtpPhone, SmsMessage, naive_utcnow

log = logging.getLogger("ordering.sms_relay")

#: How long a relayed text is kept.
KEEP_HOURS = 24
#: A phone that pinged or relayed a text this recently counts as online.
ONLINE_MINUTES = 10
#: The longest text kept; anything past this is cut.
MAX_BODY = 1000
#: How often an OTP wait looks at the table.
POLL_SECONDS = 2.0

#: A 4-8 digit code beside OTP wording, as banks and portals word it:
#: "OTP is 123456", "123456 is your One Time Password", "use 1234 to verify".
_CODE_IN_CONTEXT = re.compile(
    r"(?:otp|one[\s\-]*time\s*(?:password|pin|code)?|verification\s+code|security\s+code"
    r"|login\s+code|passcode|code\s+is|pin\s+is)\b[^0-9]{0,40}?(?<!\d)(\d{4,8})(?!\d)"
    r"|(?<!\d)(\d{4,8})(?!\d)[^0-9]{0,40}?\b(?:is\s+(?:your|the)|as\s+(?:your|the))\b[^0-9]{0,20}?"
    r"(?:otp|one[\s\-]*time|code|password|pin)",
    re.IGNORECASE,
)
#: Wording that marks a text as not carrying an OTP even when it has digits
#: beside "code" (an amount debited, an account number...).
_AMOUNT_NEAR = re.compile(r"(?:rs\.?|inr|₹)\s*$", re.IGNORECASE)


def code_from_sms(text: str | None) -> str | None:
    """The one-time code in a text, or None when it carries none."""
    if not text:
        return None
    for match in _CODE_IN_CONTEXT.finditer(text):
        code = match.group(1) or match.group(2)
        start = match.start(1) if match.group(1) else match.start(2)
        if _AMOUNT_NEAR.search(text[max(0, start - 6) : start]):
            continue  # "Rs 1234" is money, not a code
        return code
    return None


def normalise_number(value: str | None) -> str | None:
    """10 digits, without +91 or a leading 0; None when it is not a mobile number."""
    found = re.sub(r"\D", "", value or "")
    if len(found) == 12 and found.startswith("91"):
        found = found[2:]
    elif len(found) == 11 and found.startswith("0"):
        found = found[1:]
    return found if len(found) == 10 else None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str, str]:
    """A fresh token: (the token, its hash, its hint)."""
    token = secrets.token_urlsafe(24)
    return token, hash_token(token), token[-4:]


def phone_for_token(db: DbSession, token: str | None) -> OtpPhone | None:
    """The active phone this token belongs to, or None."""
    if not token or len(token) > 200:
        return None
    phone = db.scalar(select(OtpPhone).where(OtpPhone.token_hash == hash_token(token.strip())))
    if phone is None or not phone.active:
        return None
    return phone


def is_online(phone: OtpPhone, now: datetime | None = None) -> bool:
    now = now or naive_utcnow()
    seen = max((t for t in (phone.last_ping_at, phone.last_sms_at) if t is not None), default=None)
    return seen is not None and now - seen <= timedelta(minutes=ONLINE_MINUTES)


def _tag(phone: OtpPhone) -> str:
    return f"{phone.label} (…{phone.phone_number[-2:]})"


def ping(db: DbSession, phone: OtpPhone) -> None:
    phone.last_ping_at = naive_utcnow()
    db.commit()


@dataclass(frozen=True)
class Relayed:
    message_id: int
    otp_found: bool


def receive(db: DbSession, phone: OtpPhone, *, sender: str | None, body: str) -> Relayed:
    """Keep one relayed text, note its OTP on the phone, and drop old texts."""
    now = naive_utcnow()
    text = (body or "").strip()[:MAX_BODY]
    code = code_from_sms(text)
    message = SmsMessage(
        phone_id=phone.id,
        sender=(sender or "").strip()[:64] or None,
        body=text,
        otp=code,
        received_at=now,
    )
    db.add(message)
    phone.last_sms_at = now
    phone.last_sender = message.sender
    if code:
        phone.last_otp = code
        phone.last_otp_at = now
    db.execute(delete(SmsMessage).where(SmsMessage.received_at < now - timedelta(hours=KEEP_HOURS)))
    db.commit()
    log.info("SMS relay: a text from %s%s", _tag(phone), " with an OTP" if code else "")
    return Relayed(message_id=message.id, otp_found=code is not None)


class NoPhoneForNumber(RuntimeError):
    """No active relay phone is registered for the number an OTP was sent to."""


def latest_code(
    db: DbSession,
    number: str,
    *,
    not_before: datetime,
    sender_like: tuple[str, ...] = (),
) -> str | None:
    """The newest OTP relayed for ``number`` since ``not_before`` (naive UTC)."""
    wanted = normalise_number(number)
    if wanted is None:
        raise NoPhoneForNumber(f"{number!r} is not a 10-digit mobile number")
    phone = db.scalar(select(OtpPhone).where(OtpPhone.phone_number == wanted))
    if phone is None or not phone.active:
        raise NoPhoneForNumber(f"no active Employee Phone is registered for …{wanted[-2:]}")
    rows = db.scalars(
        select(SmsMessage)
        .where(SmsMessage.phone_id == phone.id)
        .where(SmsMessage.received_at >= not_before)
        .where(SmsMessage.otp.is_not(None))
        .order_by(SmsMessage.received_at.desc(), SmsMessage.id.desc())
    ).all()
    for row in rows:
        if sender_like and not any(s.lower() in (row.sender or "").lower() or s.lower() in row.body.lower() for s in sender_like):
            continue
        return row.otp
    return None


async def wait_for_otp(
    session_factory,
    number: str,
    *,
    not_before: datetime,
    timeout: float = 120,
    sender_like: tuple[str, ...] = (),
) -> str | None:
    """Wait up to ``timeout`` seconds for an OTP texted to ``number`` after
    ``not_before`` (naive UTC). ``session_factory`` opens a DB session: each
    look is a fresh one, so the phone's post is seen as soon as it commits."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        with session_factory() as db:
            code = latest_code(db, number, not_before=not_before, sender_like=sender_like)
        if code:
            return code
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(POLL_SECONDS)


class SmsOtpReader(OtpReader):
    """An :class:`OtpReader` whose "address" is a mobile number: the code is
    read from the Employee Phone registered for it."""

    def __init__(self, session_factory=None, *, sender_like: tuple[str, ...] = ()) -> None:
        if session_factory is None:
            from app.db.base import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory
        self._sender_like = sender_like

    async def get_otp(
        self,
        address: str,
        *,
        timeout: int = 60,
        not_before: datetime | None = None,
    ) -> str | None:
        since = not_before or naive_utcnow() - timedelta(seconds=5)
        if since.tzinfo is not None:
            since = since.astimezone(timezone.utc).replace(tzinfo=None)
        return await wait_for_otp(
            self._session_factory, address, not_before=since, timeout=timeout, sender_like=self._sender_like
        )
