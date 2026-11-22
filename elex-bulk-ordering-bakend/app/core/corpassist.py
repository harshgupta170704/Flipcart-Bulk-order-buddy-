"""HDFC CorpAssist: fetch child cards from the shared inbox, and their balances.

HDFC mails each virtual child card to the user id's email. Those emails reach
one shared mailbox: the first OTP inbox (``ORDERING_OTP_IMAP_*``, with the
Settings page's changes applied). **Fetch** reads the mail sent to each user
id's address by the ``hdfc_sender_filter`` senders in the last
``hdfc_since_days`` days, reads a card from each (app.io.hdfc_mail), and keeps
the cards it has not seen before. It only reads: it asks HDFC for nothing.

**Used** is what the card paid for, from our own ledger (``Order.amount`` of
the placed and unconfirmed orders whose ``card_number`` is this card), so it
is never out of step with Order Units History. **Remaining** is the opening
amount less that. A card's balance spent outside this tool is not seen.

Nothing here logs a card number; counts and last fours only.
"""

from __future__ import annotations

import email
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.core import app_settings
from app.core.card_pool import BOUGHT, digits
from app.db.models import HdfcChildCard, HdfcUserId, Order, naive_utcnow
from app.io.hdfc_mail import read_child_card

log = logging.getLogger("ordering.corpassist")

ACTIVE = "active"
CANCELLED = "cancelled"
STATUSES = (ACTIVE, CANCELLED)


class InboxNotConfigured(RuntimeError):
    """No shared inbox is set up, so there is nothing to fetch from."""


@dataclass(frozen=True)
class Mail:
    subject: str
    body: str
    sent_at: datetime | None = None


class MailSource(Protocol):
    def mails_to(self, address: str) -> list[Mail]: ...

    def close(self) -> None: ...


class ImapMailSource:
    """The shared inbox, read-only, one connection for a whole fetch."""

    def __init__(self, settings) -> None:
        from app.automation.otp import ImapOtpReader

        senders = tuple(x.strip() for x in (settings.hdfc_sender_filter or "").split(",") if x.strip())
        self._reader = ImapOtpReader(
            host=settings.otp_imap_host,
            port=settings.otp_imap_port,
            user=settings.otp_imap_user,
            password=settings.otp_imap_password,
            mailbox=settings.otp_imap_mailbox,
            use_ssl=settings.otp_imap_use_ssl,
            senders=senders,
            since_days=settings.hdfc_since_days,
        )
        self._conn = None

    def mails_to(self, address: str) -> list[Mail]:
        from app.automation.otp import _message_text, _parsed_date

        if self._conn is None:
            self._conn = self._reader._connect()
        conn = self._conn
        found: list[Mail] = []
        for mailbox in self._reader._mailboxes():
            try:
                typ, _ = conn.select(mailbox, readonly=True)
            except Exception:  # noqa: BLE001 — the folder does not exist here
                continue
            if typ != "OK":
                continue
            for msg_id in self._reader._search_ids(conn, address):
                typ, data = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not data or not data[0]:
                    continue
                message = email.message_from_bytes(data[0][1])
                sent = _parsed_date(message)
                found.append(
                    Mail(subject="", body=_message_text(message), sent_at=sent.replace(tzinfo=None) if sent else None)
                )
        return found

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None


def effective_settings(db: DbSession) -> SimpleNamespace:
    """Deployment config with the Settings page's inbox changes applied, as a run sees it."""
    base = get_settings()
    merged = {name: getattr(base, name) for name in type(base).model_fields}
    merged.update(app_settings.get_effective(db))
    return SimpleNamespace(**merged)


def mail_source(db: DbSession) -> MailSource:
    settings = effective_settings(db)
    if not (settings.otp_imap_host and settings.otp_imap_user):
        raise InboxNotConfigured(
            "No inbox is set up to read HDFC emails from. Set the OTP inbox on the Settings page first."
        )
    return ImapMailSource(settings)


@dataclass
class FetchResult:
    user_id: int
    username: str
    emails: int = 0
    added: int = 0
    error: str | None = None


def fetch(db: DbSession, users: Sequence[HdfcUserId], source: MailSource) -> list[FetchResult]:
    """Read each user id's child-card emails and keep the new cards."""
    results: list[FetchResult] = []
    since_days = getattr(effective_settings(db), "hdfc_since_days", 30)
    try:
        for user in users:
            result = FetchResult(user_id=user.id, username=user.username)
            results.append(result)
            try:
                mails = source.mails_to(user.email)
            except Exception as exc:  # noqa: BLE001 — one inbox failure is reported, not raised
                log.warning("CorpAssist: the inbox could not be read for user id %s (%s)", user.id, type(exc).__name__)
                result.error = "the inbox could not be read"
                user.last_error = result.error
                continue
            result.emails = len(mails)
            reasons: list[str] = []
            read = 0
            for mail in mails:
                parsed = read_child_card(mail.subject, mail.body)
                if parsed.number is None:
                    reasons.append(parsed.reason or "the email could not be read")
                    continue
                read += 1
                if db.scalar(select(HdfcChildCard.id).where(HdfcChildCard.card_number == parsed.number)):
                    continue  # already kept, by this or another user id
                db.add(
                    HdfcChildCard(
                        user_id=user.id,
                        card_number=parsed.number,
                        parent_card_last4=parsed.parent_last4 or user.parent_card_last4,
                        opening_amount=parsed.opening_amount,
                        status=ACTIVE,
                        issued_at=mail.sent_at,
                    )
                )
                db.flush()
                result.added += 1
            if not mails:
                result.error = f"no HDFC email to this address in the last {since_days} days"
            elif not read:
                result.error = reasons[0]
            user.last_fetched_at = naive_utcnow()
            user.last_error = result.error
        db.commit()
    finally:
        source.close()
    log.info(
        "CorpAssist fetch: %d user id(s), %d email(s), %d new card(s)",
        len(results),
        sum(r.emails for r in results),
        sum(r.added for r in results),
    )
    return results


def used_by_number(db: DbSession, numbers: Iterable[str]) -> dict[str, float]:
    """Rupees each card (digits) paid for, from our own placed and unconfirmed orders."""
    wanted = {digits(n) for n in numbers if digits(n)}
    if not wanted:
        return {}
    rows = db.execute(
        select(Order.card_number, func.sum(Order.amount))
        .where(Order.card_number.is_not(None))
        .where(Order.status.in_(BOUGHT))
        .group_by(Order.card_number)
    ).all()
    used: dict[str, float] = {}
    for number, total in rows:
        key = digits(number)
        if key in wanted:
            used[key] = used.get(key, 0.0) + float(total or 0)
    return used


@dataclass
class Balance:
    opening: float | None
    used: float
    status: str

    @property
    def remaining(self) -> float | None:
        return None if self.opening is None else round(self.opening - self.used, 2)


def balances(db: DbSession, numbers: Iterable[str]) -> dict[str, Balance]:
    """The CorpAssist balance of each of these cards (digits) that is a known child card."""
    wanted = {digits(n) for n in numbers if digits(n)}
    if not wanted:
        return {}
    cards = db.scalars(select(HdfcChildCard).where(HdfcChildCard.card_number.in_(wanted))).all()
    used = used_by_number(db, [c.card_number for c in cards])
    return {
        c.card_number: Balance(opening=c.opening_amount, used=used.get(c.card_number, 0.0), status=c.status)
        for c in cards
    }


def pair_by_balance(
    db: DbSession, cards: Sequence, accounts: Sequence, *, min_remaining: float | None = None
) -> dict[int, object] | None:
    """Pair a sheet of CorpAssist child cards with accounts by balance.

    Used when the sheet has no email column (HDFC's virtual cards are not tied
    to a Flipkart account): when every card in it is a child card CorpAssist
    knows, each account gets a card of its own, the ones with the most left
    first. Cancelled cards, cards with nothing left and cards under
    ``min_remaining`` are passed over; a card with no opening amount on record
    comes last. None when any card in the sheet is not a known child card, so
    the caller keeps its own rule for such a sheet.
    """
    numbers = [digits(getattr(c, "number", "")) for c in cards]
    if not numbers or not all(numbers):
        return None
    known = balances(db, numbers)
    if any(n not in known for n in numbers):
        return None
    usable: list[tuple[float, int, object]] = []
    seen: set[str] = set()
    for order, (card, number) in enumerate(zip(cards, numbers)):
        if number in seen:
            continue
        seen.add(number)
        balance = known[number]
        if balance.status == CANCELLED:
            continue
        left = balance.remaining
        if left is not None and (left <= 0 or (min_remaining and left < min_remaining)):
            continue
        usable.append((left if left is not None else float("-inf"), order, card))
    usable.sort(key=lambda item: (-item[0], item[1]))
    pairs = {account.id: card for account, (_, _, card) in zip(accounts, usable)}
    log.info("CorpAssist pairing: %d of %d account(s) got a child card by balance", len(pairs), len(accounts))
    return pairs
