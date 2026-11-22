"""Card pool rules for card runs (step C).

All read from what this tool already records; no card data is added:

- **Cooldown** (``cooldown_days``): a card that paid for an order (placed, or
  sent and never confirmed) in the last N days is not used. Read from
  ``Order.card_number``, which card runs already keep for statements.
- **Least used first** (``least_used_days``): accounts whose card paid for the
  fewest orders in the last N days are tried first.
- **Hard cap** (``max_uses``): one card pays for at most N orders in this run
  (a sheet may give the same card to several accounts).
- **Recent failures** (``exclude_failed_days``): a card that failed an order
  in the last N days is not used. Failed orders keep no card number, so these
  are remembered in ``card_failures`` under a salted one-way fingerprint.

- **Smart cap** (``smart_max_uses``): a card pays for at most N orders in any
  30 days, counted from our own ``Order.card_number`` history. With the hard
  cap also set, the lower of the two applies to each card.
- **Failure codes** (``exclude_failed_codes``): the failure exclusion can be
  narrowed to chosen reasons (declined, insufficient funds, OTP, ...), read
  from the failure's recorded reason by :func:`failure_code`.
- **Minimum remaining** (``min_remaining``): an HDFC child card kept by
  CorpAssist (app.core.corpassist) with less than N rupees left, no opening
  amount on record, or cancelled there, is not used. A card CorpAssist does
  not know is not affected.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.config import get_settings
from app.db.models import CardFailure, Order, OrderStatus, naive_utcnow

BOUGHT = (OrderStatus.placed, OrderStatus.unconfirmed)
_NOT_DIGIT = re.compile(r"\D")


#: The window the smart cap counts a card's orders over.
SMART_WINDOW_DAYS = 30
#: How far back a failure-code exclusion looks when no number of days is given.
DEFAULT_FAILED_DAYS = 30

#: Failure codes, each with the words in a failure's reason that name it.
#: Checked in this order; a payment failure matching none is PAYMENT_OTHER.
FAILURE_CODES: dict[str, tuple[str, ...]] = {
    "INSUFFICIENT_FUNDS": ("insufficient", "limit exceeded", "exceeds limit", "credit limit"),
    "OTP_FAILED": ("otp",),
    "AUTH_FAILED": ("password", "3-d", "3d secure", "authenticat"),
    "BANK_TIMEOUT": ("timed out", "timeout", "request blocked", "bank page"),
    "CARD_INVALID": ("cvv", "expir", "invalid card", "card number"),
    "DECLINED": ("declin", "not approved", "rejected", "transaction failed"),
}
FAILURE_CODE_NAMES: dict[str, str] = {
    "DECLINED": "Declined by the bank",
    "INSUFFICIENT_FUNDS": "Insufficient funds / limit",
    "OTP_FAILED": "OTP failed",
    "AUTH_FAILED": "Password / 3-D Secure failed",
    "BANK_TIMEOUT": "Bank page timed out or blocked",
    "CARD_INVALID": "Card details invalid or expired",
    "PAYMENT_OTHER": "Other payment failure",
}


def failure_code(reason: str | None) -> str | None:
    """The code of a failure's reason; None for one that was not at payment."""
    text = (reason or "").lower()
    for code, words in FAILURE_CODES.items():
        if any(word in text for word in words):
            return code
    # A failure already recorded against a card that names no known cause.
    return "PAYMENT_OTHER" if is_payment_failure(reason) or "payment" in text or "card" in text else None


@dataclass(frozen=True)
class CardRules:
    cooldown_days: int | None = None
    least_used_days: int | None = None
    max_uses: int | None = None
    exclude_failed_days: int | None = None
    #: Orders a card may pay for in any 30 days (from our own history).
    smart_max_uses: int | None = None
    #: Only failures with these codes count against a card; empty means any.
    exclude_failed_codes: tuple[str, ...] = ()
    #: Rupees a CorpAssist child card must have left to be used.
    min_remaining: float | None = None

    def any(self) -> bool:
        return bool(
            self.cooldown_days
            or self.least_used_days
            or self.max_uses
            or self.exclude_failed_days
            or self.smart_max_uses
            or self.exclude_failed_codes
            or self.min_remaining
        )

    @property
    def failed_window(self) -> int | None:
        """Days the failure exclusion looks back: as given, or the default when only codes were chosen."""
        if self.exclude_failed_days:
            return self.exclude_failed_days
        return DEFAULT_FAILED_DAYS if self.exclude_failed_codes else None

    def as_payload(self) -> dict:
        names = ("cooldown_days", "least_used_days", "max_uses", "exclude_failed_days", "smart_max_uses", "min_remaining")
        payload = {f"card_{name}": getattr(self, name) for name in names if getattr(self, name)}
        if self.exclude_failed_codes:
            payload["card_exclude_failed_codes"] = list(self.exclude_failed_codes)
        return payload


def digits(number: str | None) -> str:
    return _NOT_DIGIT.sub("", number or "")


def card_key(number: str | None) -> str:
    """A salted one-way fingerprint of a card number: the same card always
    gives the same key, and the key gives nothing back."""
    salt = get_settings().jwt_secret
    return hashlib.sha256(f"card:{salt}:{digits(number)}".encode()).hexdigest()


def numbers_used_since(db: DbSession, numbers: set[str], days: int) -> Counter:
    """Orders paid per card number (digits only) in the last ``days`` days."""
    if not numbers or days <= 0:
        return Counter()
    since = naive_utcnow() - timedelta(days=days)
    rows = db.execute(
        select(Order.card_number, func.count())
        .where(Order.card_number.is_not(None))
        .where(Order.status.in_(BOUGHT))
        .where(Order.created_at >= since)
        .group_by(Order.card_number)
    ).all()
    tally: Counter = Counter()
    for number, count in rows:
        key = digits(number)
        if key in numbers:
            tally[key] += int(count)
    return tally


def failed_recently(
    db: DbSession, numbers: Iterable[str], days: int, codes: Iterable[str] = ()
) -> set[str]:
    """The card numbers (digits) with a recorded failure in the last ``days``
    days — only failures with one of ``codes``, when any are given."""
    by_key = {card_key(number): digits(number) for number in numbers if digits(number)}
    if not by_key or days <= 0:
        return set()
    wanted = set(codes)
    since = naive_utcnow() - timedelta(days=days)
    rows = db.execute(
        select(CardFailure.card_key, CardFailure.reason)
        .where(CardFailure.card_key.in_(list(by_key)))
        .where(CardFailure.failed_at >= since)
    ).all()
    return {by_key[key] for key, reason in rows if not wanted or failure_code(reason) in wanted}


#: Words in a failed order's reason that put the failure ON THE CARD: the bank
#: or the card itself said no. Until 19 Nov 2026 any reason mentioning "card"
#: or "payment" counted, so a payment page that did not load or a refused gift
#: card marked a good bank card as failed and skipped its account next time
#: (audit of 19 Nov 2026).
CARD_SIGNALS = (
    "declin", "not approved", "transaction failed", "insufficient", "limit exceeded",
    "exceeds limit", "credit limit", "otp", "password", "3-d", "3d secure", "authenticat",
    "cvv", "expir", "invalid card", "bank",
)
#: Reasons that name the payment step but say nothing about the card: the page,
#: the flow or another payment instrument failed. Checked first.
NOT_THE_CARD = (
    "could not reach the payment page", "payment page did not load", "did not load",
    "could not select the payment method", "could not be entered", "gift card", "gift voucher",
    "wallet", "supercoins", "cash on delivery", "offers no", "shown but unavailable",
    "could not read", "locator.", "timeout 30000ms", "no card supplied", "over target",
)
#: Kept for callers that imported the old name.
PAYMENT_WORDS = CARD_SIGNALS


def is_payment_failure(reason: str | None) -> bool:
    """True when a failed order's reason holds the card or its bank to blame."""
    text = (reason or "").lower()
    if not text or any(phrase in text for phrase in NOT_THE_CARD):
        return False
    return any(word in text for word in CARD_SIGNALS)


def record_failure(db: DbSession, number: str | None, reason: str | None, job_id: int | None) -> None:
    if not digits(number):
        return
    db.add(CardFailure(card_key=card_key(number), reason=(reason or "")[:255] or None, job_id=job_id))
    db.commit()


@dataclass
class CardPlan:
    """What the rules decided for one run."""

    #: account id -> why its card is not used (the account is skipped).
    blocked: dict[int, str]
    #: The run's accounts in the order to try them.
    order: list[int]
    #: card number (digits) -> orders it may still pay for (the smart cap).
    caps: dict[str, int] = field(default_factory=dict)


def plan(db: DbSession, account_ids: list[int], cards: dict[int, object], rules: CardRules) -> CardPlan:
    """Apply cooldown, failure exclusion and least-used ordering up front.
    (The hard cap is counted while the run goes: see ``CapCounter``.)"""
    number_of = {aid: digits(getattr(card, "number", "")) for aid, card in cards.items()}
    numbers = {n for n in number_of.values() if n}
    blocked: dict[int, str] = {}

    if rules.cooldown_days:
        used = numbers_used_since(db, numbers, rules.cooldown_days)
        for aid, number in number_of.items():
            if used.get(number):
                blocked[aid] = f"its card was used in the last {rules.cooldown_days} day(s) (card cooldown)"

    window = rules.failed_window
    if window:
        failed = failed_recently(db, numbers, window, rules.exclude_failed_codes)
        for aid, number in number_of.items():
            if number in failed and aid not in blocked:
                which = (
                    f" ({', '.join(rules.exclude_failed_codes)})" if rules.exclude_failed_codes else ""
                )
                blocked[aid] = f"its card failed an order{which} in the last {window} day(s)"

    if rules.min_remaining:
        from app.core.corpassist import CANCELLED, balances

        known = balances(db, numbers)
        for aid, number in number_of.items():
            balance = known.get(number)
            if balance is None or aid in blocked:
                continue
            if balance.status == CANCELLED:
                blocked[aid] = "its HDFC child card is cancelled in CorpAssist"
            elif balance.remaining is None:
                blocked[aid] = "its HDFC child card has no opening amount on record (CorpAssist)"
            elif balance.remaining < rules.min_remaining:
                blocked[aid] = (
                    f"its HDFC child card has ₹{balance.remaining:,.2f} left, under the "
                    f"₹{rules.min_remaining:,.0f} minimum (CorpAssist)"
                )

    caps: dict[str, int] = {}
    if rules.smart_max_uses:
        used = numbers_used_since(db, numbers, SMART_WINDOW_DAYS)
        for number in numbers:
            caps[number] = max(0, rules.smart_max_uses - used.get(number, 0))
        for aid, number in number_of.items():
            if number and caps.get(number, 1) <= 0 and aid not in blocked:
                blocked[aid] = (
                    f"its card already paid for {rules.smart_max_uses} order(s) in the last "
                    f"{SMART_WINDOW_DAYS} days (smart cap)"
                )

    order = list(account_ids)
    if rules.least_used_days:
        uses = numbers_used_since(db, numbers, rules.least_used_days)
        position = {aid: i for i, aid in enumerate(order)}
        order.sort(key=lambda aid: (uses.get(number_of.get(aid, ""), 0), position[aid]))
    return CardPlan(blocked=blocked, order=order, caps=caps)


class CapCounter:
    """The hard cap per card for this run, counted as orders are claimed.

    ``claim`` reserves a use before the order starts and ``release`` hands it
    back when nothing was bought, so accounts sharing a card never go past it.
    """

    def __init__(self, max_uses: int | None, per_card: dict[str, int] | None = None) -> None:
        self.max_uses = max_uses
        #: Each card's own ceiling for this run (the smart cap's remainder).
        self.per_card = dict(per_card or {})
        self.used: Counter = Counter()

    def limit(self, number: str | None) -> int | None:
        """The lower of the hard cap and this card's own ceiling; None for no limit."""
        key = digits(number)
        limits = [value for value in (self.max_uses, self.per_card.get(key)) if value is not None]
        return min(limits) if limits else None

    def claim(self, number: str | None) -> bool:
        limit = self.limit(number)
        if limit is None:
            return True
        key = digits(number)
        if self.used[key] >= limit:
            return False
        self.used[key] += 1
        return True

    def release(self, number: str | None) -> None:
        key = digits(number)
        if self.used[key] > 0:
            self.used[key] -= 1
