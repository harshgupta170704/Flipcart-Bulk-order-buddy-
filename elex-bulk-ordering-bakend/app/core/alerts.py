"""The alerts engine — rules that turn records into a queue for a human
(ROADMAP §2 Phase 11).

Phases 1-10 record what happened; this module answers "what needs me?". It is
modelled on the queue the operator already works from in their existing portal
("Needs attention — 443 open"), which means two properties matter more than any
individual rule:

* **Identity, not history.** Every alert carries a stable ``key``
  (``"delivery_overdue:order:412"``). Raising the same key again updates that
  one row and reopens it if it had been closed, so the queue stays a count of
  *real problems* rather than a count of how many times a job ran.
* **Auto-resolution.** When the condition behind an alert goes away, the next
  :func:`evaluate` closes it. A rule therefore computes its **full** current key
  set and resolves every other open alert of its kind. A queue nobody can clear
  gets ignored, and an ignored queue is worse than no queue at all.

The second property is why a rule owns its ``kind``: ``evaluate`` resolves open
alerts of that kind that the rule no longer reports, so nothing else should
raise alerts under a rule's kind by hand. Ad-hoc alerts (a sync job noticing an
address mismatch) use a kind of their own and are closed by whoever raised them.

Rules are plain functions registered in :data:`RULES`, so adding one is additive
— a new rule is a function plus a line in that tuple, and nothing else changes.
Each takes its thresholds as keyword arguments with defaults, so ``evaluate``
can call it with no arguments while a caller (or a test) can tighten it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, exists, func, or_, select

from app.core import cancellations, order_status, run_notes
from app.core.money import rupees
from app.core.order_units import normalised_line
from app.io.invoice_pdf import KIND_FEE, KIND_PRODUCT, KIND_UNKNOWN
from sqlalchemy.orm import Session as DbSession

from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Alert,
    AlertSeverity,
    Invoice,
    Order,
    OrderStatus,
    OrderUnit,
    utcnow,
)

log = logging.getLogger("ordering.alerts")

#: Days after which a placed order the platform still has not delivered is
#: someone's problem. Deliberately generous: Flipkart's own estimates run to a
#: week, and an alert that fires on every normal delivery trains the operator to
#: ignore the queue.
OVERDUE_DAYS = 10
#: The order rules look back this far at most: an outcome older than a quarter
#: is history, not something to act on, and loading every such order ever on
#: every pass grew with the ledger (audit F-43, 25 Oct 2026).
ORDER_RULE_WINDOW_DAYS = 90
#: A run's own outcome — stopped by the price cap, or failed before anything
#: reached Flipkart — is news for a few days and then only history: the run
#: said it on the day, the ledger keeps the row, and nothing about it can
#: still be acted on. Until 11 Nov 2026 each stayed open for the whole quarter,
#: and a fortnight of rehearsals left 28 of the queue's 31 alerts about them.
RUN_OUTCOME_WINDOW_DAYS = 3
#: An order Flipkart took and then cancelled (or never placed) is worth a
#: fortnight in the queue: long enough to reorder or chase a refund.
CANCELLED_WINDOW_DAYS = 14

#: Days after which a placed order that has never been synced is suspicious —
#: not because the order is wrong, but because we know nothing about it.
STALE_SYNC_DAYS = 2

#: Days after a unit was delivered by which its order's product invoice is
#: expected on file. Flipkart issues the tax invoice when the unit ships and
#: offers it on the order page once it is delivered; the read-back fetches it
#: from there, and the timed read-backs come round every few hours — so three
#: days leave room for a read-back or two, or a PDF put on file by hand,
#: before a person is asked to chase it.
MISSING_INVOICE_DAYS = 3

#: Rupees the invoices may be off the amount an order was recorded at before
#: it counts as a difference: the checkout shows whole rupees and an invoice
#: prints paise, so anything within a rupee is rounding, not a wrong bill.
INVOICE_AMOUNT_TOLERANCE = 1.0

#: Unit readings that mean the unit reached the buyer: delivered only. A unit
#: read as returned is left out: "Refund Completed" also describes a prepaid
#: order cancelled before it shipped, which never had a product invoice, and
#: the date beside a return or refund is that step's, not the delivery's.
DELIVERED_STATES: tuple[str, ...] = (order_status.DELIVERED,)

#: Platform statuses that end an order's life, so "not delivered yet" is false.
#: Compared lower-cased, and both spellings of cancelled are listed because the
#: raw platform wording is stored verbatim (``Order.platform_status``) and a
#: spelling we failed to anticipate would read as "still in transit" forever.
#: What counts as settled is decided once, in app.core.order_status; the SQL
#: form the overdue rule needs lives next to that rule (_SETTLED_LIKE).

#: Order statuses whose rows stand on the platform. A ``failed_over_target``
#: row never became an order there, and a ``failed`` one either never did or
#: was cancelled there (app.core.cancellations), so asking about its delivery
#: or its sync state would raise an alert no human can act on.
LIVE_ORDER_STATUSES: tuple[OrderStatus, ...] = (OrderStatus.placed,)

#: How many keys go into one IN clause. Well under SQLite's and PostgreSQL's
#: bind-parameter ceilings, which a rule reporting on 37k accounts would hit.
_KEY_CHUNK = 900

#: Values of ``Alert.resolved_by``: who closed the row.
RESOLVED_BY_USER = "user"
RESOLVED_BY_RULE = "rule"

#: Detail keys that change by themselves as time passes — an age in days —
#: and so must not count as the alert changing. The message is derived from
#: the details (it carries the same age), so it is refreshed but never compared.
VOLATILE_DETAIL_KEYS = frozenset({"days"})

#: Rules passes are serialised. One can run on a request's thread ("Evaluate
#: now") while another runs on the event loop as a read-back finishes, and two
#: read-then-insert upserts of the same new key would collide on the UNIQUE
#: index. A pass is idempotent, so a waiter simply runs after.
_PASS_LOCK = threading.Lock()


# --- What a rule reports ------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    """One thing a rule found, identified by ``key``.

    Frozen because a finding is a reading of the data at one moment: a rule
    builds its whole list before anything is written, so nothing can mutate a
    finding between being found and being raised.
    """

    key: str
    message: str
    severity: AlertSeverity = AlertSeverity.warning
    account_id: int | None = None
    order_id: int | None = None
    details: dict | None = None


@dataclass(frozen=True)
class Rule:
    """A rule and the ``kind`` of alert it owns.

    ``find`` is called with the session alone; thresholds live as defaults in
    the rule's own signature so there is one place each is written down.
    """

    kind: str
    find: Callable[[DbSession], Sequence[Finding]]


# --- Small shared formatting --------------------------------------------------
def _money(value: float | None) -> str:
    """An amount as the operator reads it everywhere else — "₹1,29,900.00",
    Indian grouping and two decimals (app.core.money, NFR-010) — or a hedge
    when we never saw one. Each rules pass rewrites an open alert's message,
    so the alerts already open read this way after the next pass."""
    if value is None:
        return "an unknown amount"
    return rupees(value)


def _who(email: str | None, account_id: int | None) -> str:
    """Name the account in a message.

    The email is what the operator recognises; the id is the fallback for an
    order whose account row has gone (the alert still needs a human).
    """
    if email:
        return email
    return f"account {account_id}" if account_id else "an unknown account"


def _days_ago(moment: datetime | None, *, now: datetime) -> int:
    """Whole days between ``moment`` and ``now``, never negative.

    The ``DateTime`` columns are naive UTC, but a row written earlier in the
    same session still holds the timezone-aware value it was given, so a naive
    reading is assumed to be UTC rather than compared as-is — subtracting an
    aware datetime from a naive one raises.
    """
    if moment is None:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0, int((now - moment).total_seconds() // 86400))


def _not_settled() -> ColumnElement[bool]:
    """SQL for "the platform has not finished with this order".

    Reads the derived ``delivery_state`` column, which the model fills from the
    platform's wording through the one shared classifier — so this rule, the
    ledger's filter and the table's badge cannot disagree. NULL (a row not yet
    derived, or never synced) counts as unsettled: a never-synced order must
    not be the one order that can never go overdue.
    """
    return or_(
        Order.delivery_state.is_(None),
        Order.delivery_state.in_(tuple(order_status.UNDELIVERED)),
    )


def _orders_with_email(
    db: DbSession, *where, window_days: int | None = ORDER_RULE_WINDOW_DAYS
) -> list[tuple[Order, str | None]]:
    """Order rows plus their account email, in one query, within a look-back
    window of ``window_days`` by the row's recording (:data:`ORDER_RULE_WINDOW_DAYS`
    unless a rule names its own; ``None`` for no window at all).

    An OUTER join on purpose: an order whose account row is missing is exactly
    the kind of thing a human should see, and an inner join would hide it.
    """
    stmt = select(Order, Account.email).outerjoin(Account, Account.id == Order.account_id).where(*where)
    if window_days is not None:
        cutoff = (utcnow() - timedelta(days=window_days)).replace(tzinfo=None)
        stmt = stmt.where(Order.created_at >= cutoff)
    return [(order, email) for order, email in db.execute(stmt.order_by(Order.id.asc())).all()]


def _named(order: Order) -> str:
    """An order as a person knows it: Flipkart's order id when it has one,
    else "the order" — never the ledger's own row number, which no screen
    shows (until 11 Nov 2026 the alerts said "Order 26")."""
    return f"Order {order.order_id}" if order.order_id else "The order"


# --- The rules ----------------------------------------------------------------
def find_over_target_orders(db: DbSession) -> list[Finding]:
    """Orders the price cap refused to place (PLAN.md §6a).

    Severity is **info**, never critical: the cap doing its job is the tool
    working correctly. The operator still wants the list — it is where they
    decide whether to raise a target — but a healthy outcome that shouts like a
    failure is how a queue loses its meaning.
    """
    findings: list[Finding] = []
    rows = _orders_with_email(db, Order.status == OrderStatus.over_target, window_days=RUN_OUTCOME_WINDOW_DAYS)
    for order, email in rows:
        label = f" ({order.model})" if order.model else ""
        findings.append(
            Finding(
                key=f"order_over_target:order:{order.id}",
                message=(
                    f"The order{label} for {_who(email, order.account_id)} was not placed: "
                    f"{_money(order.amount)} is over the {_money(order.target)} price cap. "
                    "Nothing was charged."
                ),
                severity=AlertSeverity.info,
                account_id=order.account_id,
                order_id=order.id,
                details={
                    "amount": order.amount,
                    "target": order.target,
                    "model": order.model,
                },
            )
        )
    return findings


def find_failed_orders(db: DbSession) -> list[Finding]:
    """Orders the run did not get — and orders Flipkart cancelled, each said
    for what it is.

    A failed order with no order id never reached Flipkart: the run tried and
    did not get an order. One with Flipkart's order id was taken by Flipkart
    and cancelled there later (app.core.cancellations), and its message says
    so with the reason the order page gave — "was cancelled by Flipkart: Sorry,
    your order was cancelled as per seller policy restrictions applicable to
    this brand." — rather than "failed", which blamed the run (26 Oct 2026).
    Both stay under this rule's kind and key: an alert already open for a
    cancellation is reworded where it stands, never raised a second time, and
    one a person accepted stays accepted. A cancellation the account itself
    asked for ("Cancelled by user", "You requested a cancellation…") says so,
    and is info: someone meant it — as does one the tool holds a cancel
    request for (app.core.cancellations). When only some of an order's units
    were the account's, the order still reads as Flipkart's cancellation, says
    how many units were the account's, and quotes only the other units'
    reasons: the account's own reason is not why Flipkart cancelled.
    """
    findings: list[Finding] = []
    # Two kinds of "failed", each with its own time in the queue: an attempt
    # that never reached Flipkart, and an order Flipkart took and cancelled.
    rows = _orders_with_email(db, cancellations.never_sent_sql(), window_days=RUN_OUTCOME_WINDOW_DAYS)
    rows += _orders_with_email(db, cancellations.placed_then_cancelled_sql(), window_days=CANCELLED_WINDOW_DAYS)
    rows.sort(key=lambda pair: pair[0].id)
    readings = cancellations.cancel_readings(
        db, [order.id for order, _email in rows if cancellations.placed_then_cancelled(order)]
    )
    # Why each attempt failed, in its run's own words, in one query.
    said = run_notes.messages_for(
        db, [order for order, _email in rows if not cancellations.placed_then_cancelled(order)]
    )
    for order, email in rows:
        label = f" ({order.model})" if order.model else ""
        who = _who(email, order.account_id)
        if not cancellations.placed_then_cancelled(order):
            reason = run_notes.reason_text(said.get(order.id))
            message = f"The order{label} for {who} was not placed"
            findings.append(
                Finding(
                    key=f"order_failed:order:{order.id}",
                    message=(
                        f"{message}: {reason}"
                        if reason
                        else f"{message}: the run stopped before anything was sent to Flipkart"
                    ),
                    severity=AlertSeverity.warning,
                    account_id=order.account_id,
                    order_id=order.id,
                    details={"model": order.model, "product_link": order.product_link, "reason": reason},
                )
            )
            continue
        if _never_placed(order):
            # Flipkart gave the attempt an order id and then said "Order Not
            # Placed": the payment did not go through. Not a cancellation, and
            # nothing to refund — said for what it is (11 Nov 2026). Its own
            # name: until 24 Nov 2026 this reused ``said``, the run lines
            # looked up above, so the next attempt that never reached
            # Flipkart met a string and the whole pass failed.
            flipkart_says = (order.platform_status or "").strip()
            findings.append(
                Finding(
                    key=f"order_failed:order:{order.id}",
                    message=(
                        f"Order {order.order_id}{label} for {who} was not placed: Flipkart says "
                        f"\"{flipkart_says}\" — the payment did not go through, so nothing is on its way. "
                        "If the card shows a charge, the bank reverses it."
                    ),
                    severity=AlertSeverity.warning,
                    account_id=order.account_id,
                    order_id=order.id,
                    details={
                        "order_id": order.order_id,
                        "cancelled_by": cancellations.BY_PLATFORM,
                        "never_placed": True,
                        "model": order.model,
                        "product_link": order.product_link,
                        "cancel_reason": flipkart_says or None,
                    },
                )
            )
            continue
        reading = readings.get(order.id) or cancellations.CancelReading()
        by_user = reading.user_cancelled
        if by_user:
            reason = " / ".join(reading.user_reasons) or None
            message = f"Order {order.order_id}{label} for {who} was cancelled at the account's request"
        else:
            reason = " / ".join(reading.other_reasons) or None
            message = f"Order {order.order_id}{label} for {who} was cancelled by Flipkart"
            if reading.by_user:
                units = "one unit" if reading.by_user == 1 else f"{reading.by_user} units"
                message += f" ({units} at the account's request)"
        findings.append(
            Finding(
                key=f"order_failed:order:{order.id}",
                message=f"{message}: {reason}" if reason else f"{message}; no reason recorded",
                severity=AlertSeverity.info if by_user else AlertSeverity.warning,
                account_id=order.account_id,
                order_id=order.id,
                details={
                    "order_id": order.order_id,
                    "cancelled_by": cancellations.BY_USER if by_user else cancellations.BY_PLATFORM,
                    "model": order.model,
                    "product_link": order.product_link,
                    "cancel_reason": reason,
                },
            )
        )
    return findings


def _never_placed(order: Order) -> bool:
    """Whether Flipkart's wording says this order was never placed ("Order
    Not Placed", "Payment Failed"). Read off the order row, which mirrors the
    unit that speaks for the order (app.jobs.sync_tasks._mirror_from_units) —
    so a pass over thousands of cancelled orders loads no unit rows for it."""
    return order_status.never_placed(order.platform_status)


def find_unconfirmed_orders(db: DbSession) -> list[Finding]:
    """Orders submitted to Flipkart that it never confirmed — they may exist.

    **Critical**, unlike a failure: this is the one outcome where money may
    have been committed and nobody knows, and where ordering again on the
    account can buy the same thing twice. A person has to look.
    """
    findings: list[Finding] = []
    # No look-back window: an unconfirmed order stays unconfirmed — its units
    # spent, a second order a risk — until a read-back finds it or a person
    # settles it, and its alert must stay as long. Until 11 Nov 2026 the alert
    # closed itself after 90 days while the order went on reading unconfirmed.
    for order, email in _orders_with_email(db, Order.status == OrderStatus.unconfirmed, window_days=None):
        label = f" ({order.model})" if order.model else ""
        findings.append(
            Finding(
                key=f"order_unconfirmed:order:{order.id}",
                message=(
                    f"The order{label} for {_who(email, order.account_id)} was "
                    f"submitted for {_money(order.amount)} but Flipkart never confirmed "
                    "it. It may exist — open the account's orders on Flipkart, then open "
                    "this row in Order Units and say what you found before ordering again."
                ),
                severity=AlertSeverity.critical,
                account_id=order.account_id,
                order_id=order.id,
                details={
                    "amount": order.amount,
                    "model": order.model,
                    "product_link": order.product_link,
                },
            )
        )
    return findings


def find_overdue_deliveries(
    db: DbSession, *, overdue_days: int = OVERDUE_DAYS
) -> list[Finding]:
    """Placed orders the platform has neither delivered nor cancelled in time.

    Only ``placed`` rows are considered: a failed or over-target row never
    reached the platform, so there is no delivery to be late. A never-synced
    order counts as not delivered — after ten days, silence is the problem
    whether it is the platform's or ours.
    """
    now = utcnow()
    cutoff = now - timedelta(days=max(1, int(overdue_days)))
    findings: list[Finding] = []
    rows = _orders_with_email(
        db,
        Order.status.in_(LIVE_ORDER_STATUSES),
        # Not NULL and older than the cutoff. A NULL placed_at never passes the
        # comparison in SQL, which is right — it was never placed.
        Order.placed_at.is_not(None),
        Order.placed_at < cutoff,
        _not_settled(),
    )
    for order, email in rows:
        age = _days_ago(order.placed_at, now=now)
        state = order.platform_status or "nothing yet"
        findings.append(
            Finding(
                key=f"delivery_overdue:order:{order.id}",
                message=(
                    f"{_named(order)} for {_who(email, order.account_id)} was ordered {age} days "
                    f"ago and Flipkart still says \"{state}\""
                ),
                severity=AlertSeverity.warning,
                account_id=order.account_id,
                order_id=order.id,
                details={
                    "days": age,
                    "overdue_days": int(overdue_days),
                    "platform_status": order.platform_status,
                    "tracking_id": order.tracking_id,
                },
            )
        )
    return findings


def find_invalid_sessions(db: DbSession) -> list[Finding]:
    """Accounts whose stored session no longer works.

    Nothing can run for such an account — no coupon, no order, no sync — until
    someone logs it in again, so it is a blocker rather than a note.
    """
    rows = db.execute(
        select(Account.id, Account.email)
        .where(Account.status == AccountStatus.session_invalid)
        .order_by(Account.id.asc())
    ).all()
    return [
        Finding(
            key=f"session_invalid:account:{account_id}",
            message=(
                f"{_who(email, account_id)} has an invalid session and needs "
                "logging in again"
            ),
            severity=AlertSeverity.warning,
            account_id=account_id,
            details={"email": email},
        )
        for account_id, email in rows
    ]


def find_unsynced_orders(
    db: DbSession, *, stale_days: int = STALE_SYNC_DAYS
) -> list[Finding]:
    """Placed orders that have never been synced back from the platform.

    Info, not warning: the order is probably fine. What is wrong is that the
    record is blind — no tracking id, no delivery date, no OTP — and a blind
    record is the thing the operator cannot answer a question from.
    """
    now = utcnow()
    cutoff = now - timedelta(days=max(1, int(stale_days)))
    findings: list[Finding] = []
    rows = _orders_with_email(
        db,
        Order.status.in_(LIVE_ORDER_STATUSES),
        Order.placed_at.is_not(None),
        Order.placed_at < cutoff,
        Order.synced_at.is_(None),
    )
    for order, email in rows:
        age = _days_ago(order.placed_at, now=now)
        findings.append(
            Finding(
                key=f"stale_sync:order:{order.id}",
                message=(
                    f"{_named(order)} for {_who(email, order.account_id)} was placed {age} days "
                    "ago and has never been read back from Flipkart"
                ),
                severity=AlertSeverity.info,
                account_id=order.account_id,
                order_id=order.id,
                details={"days": age, "stale_days": int(stale_days)},
            )
        )
    return findings


#: The fewest characters a normalised Address Book first line may have and
#: still count as a match. "1" or "A" would be found in nearly any address in
#: the same pincode, and a check that cannot fail is no check at all.
MIN_LINE1_CHARS = 3


#: The Address Book read once for a whole pass: pincode -> its entries' first
#: lines, normalised, the too-short ones already left out.
BookLines = dict[str, list[str]]


def book_lines(book: Sequence[Address]) -> BookLines:
    """Normalise every entry's first line once, grouped by pincode.

    The mismatch rule compares every unit in the ledger against the book, so
    normalising the book inside that loop cost one pass over the book's text
    per unit — about half a second of a 40,000-unit pass, measured — for the
    same answers this gives in one go.
    """
    lines: BookLines = {}
    for entry in book:
        pincode = (entry.pincode or "").strip()
        line = normalised_line(entry.line1)
        if pincode and len(line) >= MIN_LINE1_CHARS:
            lines.setdefault(pincode, []).append(line)
    return lines


def _shown_in(shown: str, pincode: str | None, lines: BookLines) -> bool:
    """Whether a normalised page address is one of the book's at that pincode."""
    wanted = (pincode or "").strip()
    if not wanted or not shown:
        return False
    # Both are single-spaced words, so padding with spaces makes the
    # containment a whole-word one.
    padded = f" {shown} "
    return any(f" {line} " in padded for line in lines.get(wanted, ()))


def address_in_book(address: str | None, pincode: str | None, book: Sequence[Address]) -> bool:
    """Whether a delivery address the platform showed is one of ours.

    Spec C-5 as decided: the same pincode and the same first line. The page
    renders the whole address on one line, so "the same first line" is the
    Address Book entry's first line appearing, normalised, inside the page's
    text — spelling of the locality and state can differ without a false
    alarm. It must appear as whole words: "A-1" is not the first line of
    "A-1234, Nucleus Software", and a line too short to tell addresses apart
    (:data:`MIN_LINE1_CHARS`) matches none — either would hide a real
    mismatch behind a match that could not fail.
    """
    return _shown_in(normalised_line(address), pincode, book_lines(book))


def _units_with_address(db: DbSession, *where):
    return db.execute(
        select(OrderUnit, Order, Account.email)
        .join(Order, Order.id == OrderUnit.order_pk)
        .outerjoin(Account, Account.id == Order.account_id)
        .where(OrderUnit.delivery_address.is_not(None), *where)
        .order_by(OrderUnit.id.asc())
    ).all()


def find_address_mismatches(db: DbSession) -> list[Finding]:
    """Units whose delivery address, as the platform showed it, matches no
    address in the Address Book (spec FR-044 — "Order unit address mismatch").

    Warning, not critical: the order may be fine, but it is going somewhere
    the business did not set up, which is the quiet failure the Address Book
    exists to prevent. A closed one stays closed while the same address stands.
    """
    lines = book_lines(db.scalars(select(Address)).all())
    # Plain columns, not entities: this rule walks every unit with an address
    # on every pass, and hydrating two ORM objects per unit was a third of the
    # pass at a year's volume (remaining-work §8). A unit whose pincode the
    # book does not have at all is a mismatch without normalising anything.
    rows = db.execute(
        select(
            OrderUnit.id,
            OrderUnit.position,
            OrderUnit.unit_id,
            OrderUnit.delivery_address,
            OrderUnit.delivery_pincode,
            Order.id,
            Order.order_id,
            Order.account_id,
            Account.email,
        )
        .join(Order, Order.id == OrderUnit.order_pk)
        .outerjoin(Account, Account.id == Order.account_id)
        .where(OrderUnit.delivery_address.is_not(None))
        .order_by(OrderUnit.id.asc())
    ).all()
    findings: list[Finding] = []
    for unit_pk, position, unit_id, address, pincode, order_pk, order_id, account_id, email in rows:
        wanted = (pincode or "").strip()
        if wanted in lines and _shown_in(normalised_line(address), wanted, lines):
            continue
        findings.append(
            Finding(
                key=f"address_mismatch:unit:{unit_pk}",
                message=(
                    f"Order {order_id or order_pk} (unit {position}) is addressed to "
                    f"'{address}', which is not in the Address Book "
                    f"({_who(email, account_id)})"
                ),
                severity=AlertSeverity.warning,
                account_id=account_id,
                order_id=order_pk,
                details={
                    "unit_id": unit_id,
                    "delivery_address": address,
                    "delivery_pincode": pincode,
                },
            )
        )
    return findings


def find_address_changes(db: DbSession) -> list[Finding]:
    """Units whose delivery address changed between two read-backs (spec
    FR-045 — "Order unit address change").

    Keyed on the moment of the change, so a second change after the first was
    accepted is a new alert rather than a silent update of a closed one.
    """
    findings: list[Finding] = []
    for unit, order, email in _units_with_address(db, OrderUnit.address_changed_at.is_not(None)):
        changed = unit.address_changed_at
        stamp = int(changed.replace(tzinfo=timezone.utc).timestamp()) if changed.tzinfo is None else int(changed.timestamp())
        findings.append(
            Finding(
                key=f"address_change:unit:{unit.id}:{stamp}",
                message=(
                    f"The delivery address of order {order.order_id or order.id} (unit {unit.position}) "
                    f"changed on the platform; it now reads '{unit.delivery_address}' "
                    f"({_who(email, order.account_id)})"
                ),
                severity=AlertSeverity.warning,
                account_id=order.account_id,
                order_id=order.id,
                details={
                    "unit_id": unit.unit_id,
                    "delivery_address": unit.delivery_address,
                    "delivery_pincode": unit.delivery_pincode,
                    "changed_at": changed.isoformat(),
                },
            )
        )
    return findings


def _gstin(value: str | None) -> str:
    return "".join((value or "").split()).upper()


def find_gst_mismatches(db: DbSession) -> list[Finding]:
    """Units the platform shows billed under a GST other than the one the run
    chose ("Order unit GST mismatch", the owner's addition of 26 Oct 2026).

    The run records the GST it entered at checkout on the order row
    (``Order.gstin``); the read-back keeps the GST the order page shows
    (``OrderUnit.billed_gstin``). When both are known and differ, the invoice
    will name the wrong entity — a quiet failure, since the order itself went
    through. A unit whose page shows no GST block, or an order placed with no
    GST, raises nothing: only two known values are compared.
    """
    rows = db.execute(
        select(
            OrderUnit.id,
            OrderUnit.position,
            OrderUnit.unit_id,
            OrderUnit.billed_gstin,
            OrderUnit.billed_business_name,
            Order.id,
            Order.order_id,
            Order.account_id,
            Order.gstin,
            Order.gst_name,
            Account.email,
        )
        .join(Order, Order.id == OrderUnit.order_pk)
        .outerjoin(Account, Account.id == Order.account_id)
        .where(OrderUnit.billed_gstin.is_not(None), Order.gstin.is_not(None))
        .order_by(OrderUnit.id.asc())
    ).all()
    findings: list[Finding] = []
    for unit_pk, position, unit_id, billed, business, order_pk, order_id, account_id, chosen, chosen_name, email in rows:
        if _gstin(billed) == _gstin(chosen):
            continue
        findings.append(
            Finding(
                key=f"gst_mismatch:unit:{unit_pk}",
                message=(
                    f"Order {order_id or order_pk} (unit {position}) is billed under GST {billed}"
                    f"{f' ({business})' if business else ''}, not the {chosen}"
                    f"{f' ({chosen_name})' if chosen_name else ''} the run chose "
                    f"({_who(email, account_id)})"
                ),
                severity=AlertSeverity.warning,
                account_id=account_id,
                order_id=order_pk,
                details={
                    "unit_id": unit_id,
                    "billed_gstin": billed,
                    "billed_business_name": business,
                    "chosen_gstin": chosen,
                    "chosen_gst_name": chosen_name,
                },
            )
        )
    return findings


# --- the invoice checks across orders (26 Oct 2026) -------------------------------
def _naive(moment: datetime | None) -> datetime | None:
    """A stamp as naive UTC, whichever way it was written (see :func:`_days_ago`),
    so two of them can be compared."""
    if moment is None or moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _has_product_invoice() -> ColumnElement[bool]:
    """SQL for "the order has a product invoice on file".

    The product invoice is the one the parser read as the product's tax
    invoice (``kind`` "product") — the reading the row dialog compares with
    the amount and the one-file download prefers (audit F-38). A fee or
    transport invoice, a credit note or a PDF the reader could not place is
    not one.
    """
    return exists().where(Invoice.order_pk == Order.id, Invoice.kind == KIND_PRODUCT).correlate(Order)


def _counted_once(invoices: Sequence[tuple[str | None, float | None]]) -> tuple[float | None, int]:
    """``(the grand totals added up, how many invoices)``, each invoice once.

    The same invoice can be on file twice — downloaded again, its bytes
    differ, so the store keeps both — and is one invoice by its number; a row
    with no number read counts on its own. The total is None when any of them
    has no total read off it: a sum with a part missing would read as a
    difference that is not there.
    """
    seen: set[str] = set()
    total = 0.0
    count = 0
    unknown = False
    for number, grand_total in invoices:
        key = (number or "").strip().upper()
        if key:
            if key in seen:
                continue
            seen.add(key)
        count += 1
        if grand_total is None:
            unknown = True
        else:
            total += float(grand_total)
    return (None if unknown else round(total, 2)), count


def _invoices_on_file(
    db: DbSession, order_pks: Sequence[int]
) -> dict[int, dict[str, list[tuple[str | None, float | None]]]]:
    """Every invoice on file for these orders, by the kind the reader gave it,
    as ``{order: {kind: [(invoice number, grand total), …]}}`` — one query per
    :data:`_KEY_CHUNK` orders, never one per order."""
    wanted = sorted({int(pk) for pk in order_pks})
    found: dict[int, dict[str, list[tuple[str | None, float | None]]]] = {}
    for start in range(0, len(wanted), _KEY_CHUNK):
        rows = db.execute(
            select(Invoice.order_pk, Invoice.kind, Invoice.invoice_number, Invoice.grand_total)
            .where(Invoice.order_pk.in_(wanted[start : start + _KEY_CHUNK]))
            .order_by(Invoice.order_pk, Invoice.id)
        ).all()
        for order_pk, kind, number, grand_total in rows:
            found.setdefault(int(order_pk), {}).setdefault(kind, []).append((number, grand_total))
    return found


def find_invoice_amount_mismatches(
    db: DbSession, *, tolerance: float = INVOICE_AMOUNT_TOLERANCE
) -> list[Finding]:
    """Orders whose product invoice comes to another amount than the one the
    order was recorded at ("Invoice amount differs", 26 Oct 2026).

    The row dialog has compared the two since the owner asked to see the
    invoices; this puts the orders where they differ in the queue rather than
    leaving them to be found one dialog at a time. The product invoice is the
    one the dialog takes (``kind`` "product"); an order invoiced in parts —
    a product invoice per shipment — is compared by the parts' sum.

    Flipkart bills its fees and the delivery on invoices of their own (a fee
    invoice each, and a transport invoice for "GT Charges"), while the amount
    recorded at checkout is everything that was charged. So an order whose
    product invoice and fee invoices together come to the amount is no
    difference — an alert on every order that carried a fee would train the
    operator to ignore the queue — and nor is anything within ``tolerance``.

    Warning: the bill may be wrong, or an invoice missing or misread, so
    someone should look; no order is lost. Only placed orders with a recorded
    amount and a product invoice on file are compared, within the order
    rules' look-back window. And only known values are compared, like the GST
    check: an invoice whose total the reader did not get, or a PDF it could
    not place at all — a fee invoice, perhaps — leaves the sum unknown, and an
    unknown sum raises nothing rather than a difference that may not be there.
    """
    rows = _orders_with_email(
        db,
        Order.status.in_(LIVE_ORDER_STATUSES),
        Order.amount.is_not(None),
        _has_product_invoice(),
    )
    on_file = _invoices_on_file(db, [order.id for order, _email in rows])
    findings: list[Finding] = []
    for order, email in rows:
        invoices = on_file.get(order.id, {})
        products = invoices.get(KIND_PRODUCT, [])
        product_total, product_count = _counted_once(products)
        fee_total, fee_count = _counted_once(invoices.get(KIND_FEE, []))
        if product_total is None or fee_total is None or not product_count or invoices.get(KIND_UNKNOWN):
            continue
        amount = float(order.amount)
        with_fees = round(product_total + fee_total, 2) if fee_count else None
        if abs(product_total - amount) <= tolerance:
            continue
        if with_fees is not None and abs(with_fees - amount) <= tolerance:
            continue
        what = (
            f"'s {product_count} product invoices come to"
            if product_count > 1
            else "'s product invoice comes to"
        )
        fees = (
            f" ({_money(with_fees)} with its {fee_count} fee invoice{'s' if fee_count != 1 else ''})"
            if with_fees is not None
            else ""
        )
        # Each invoice once, as _counted_once counts them, by its first spelling.
        numbers: list[str] = []
        for number, _total in products:
            text = (number or "").strip()
            if text and text.upper() not in {each.upper() for each in numbers}:
                numbers.append(text)
        findings.append(
            Finding(
                key=f"invoice_amount_mismatch:order:{order.id}",
                message=(
                    f"Order {order.order_id or order.id}{what} {_money(product_total)}{fees}, "
                    f"not the {_money(amount)} the order was recorded at "
                    f"({_who(email, order.account_id)})"
                ),
                severity=AlertSeverity.warning,
                account_id=order.account_id,
                order_id=order.id,
                details={
                    "amount": amount,
                    "product_invoice_total": product_total,
                    "with_fee_invoices": with_fees,
                    "invoice_numbers": ", ".join(numbers) or None,
                },
            )
        )
    return findings


def _delivered_units(db: DbSession, order_pks: Sequence[int]) -> dict[int, list[datetime | None]]:
    """The delivery date of each delivered unit of these orders (None where
    the page showed no date) — one query per :data:`_KEY_CHUNK` orders."""
    wanted = sorted({int(pk) for pk in order_pks})
    found: dict[int, list[datetime | None]] = {}
    for start in range(0, len(wanted), _KEY_CHUNK):
        rows = db.execute(
            select(OrderUnit.order_pk, OrderUnit.delivery_date).where(
                OrderUnit.order_pk.in_(wanted[start : start + _KEY_CHUNK]),
                OrderUnit.delivery_state.in_(DELIVERED_STATES),
            )
        ).all()
        for order_pk, delivered_on in rows:
            found.setdefault(int(order_pk), []).append(delivered_on)
    return found


def find_missing_product_invoices(
    db: DbSession, *, grace_days: int = MISSING_INVOICE_DAYS
) -> list[Finding]:
    """Delivered orders with no product invoice on file ("Product invoice
    missing", 26 Oct 2026).

    The business keeps the tax invoice of every order it receives, and the
    read-back fetches it by itself once Flipkart offers it — so an order
    delivered more than ``grace_days`` ago with none on file is one the fetch
    missed: someone reads the account back again or puts the PDF on file by
    hand. Fee and transport invoices and credit notes do not count; how many
    other invoices there are is in the details. Nor does a PDF the reader could
    not place — it may be the product invoice, but nothing can be read off it
    (no CSV row, no total) — and the message says so, so it does not read as
    if nothing were on file.

    Delivered means a unit of the order reads delivered — or returned, which a
    unit only becomes once delivered — on the unit's own reading, the grain
    the read-back writes: the order row mirrors one representative unit, and
    reads "in transit" while another unit has already arrived. The clock is
    the unit's delivery date; a delivered unit whose page showed no date counts
    from the order's date instead (or, with none, from when the row was
    recorded), so it is never the one order that cannot be asked about.

    Warning, like the GST and address checks: the order went through, but
    the record the accounts need is not on file. Placed orders only, within
    the order rules' look-back window.
    """
    now = utcnow()
    cutoff = (now - timedelta(days=max(1, int(grace_days)))).replace(tzinfo=None)
    since = func.coalesce(OrderUnit.delivery_date, Order.placed_at, Order.created_at)
    delivered_in_time = (
        exists()
        .where(
            OrderUnit.order_pk == Order.id,
            OrderUnit.delivery_state.in_(DELIVERED_STATES),
            since < cutoff,
        )
        .correlate(Order)
    )
    rows = _orders_with_email(
        db,
        Order.status.in_(LIVE_ORDER_STATUSES),
        delivered_in_time,
        ~_has_product_invoice(),
    )
    order_pks = [order.id for order, _email in rows]
    delivered = _delivered_units(db, order_pks)
    on_file = _invoices_on_file(db, order_pks)
    findings: list[Finding] = []
    for order, email in rows:
        known = sorted(
            day for day in (_naive(each) for each in delivered.get(order.id, [])) if day is not None
        )
        # The earliest dated delivery when one is past the grace period; only
        # otherwise was it an undated unit that qualified, on the order's date.
        dated = bool(known) and known[0] < cutoff
        moment = known[0] if dated else _naive(order.placed_at or order.created_at)
        age = _days_ago(moment, now=now)
        invoices = on_file.get(order.id, {})
        unreadable = len(invoices.get(KIND_UNKNOWN, []))
        missing = (
            f"has no readable product invoice on file ({'one' if unreadable == 1 else unreadable} "
            "of its PDFs could not be read)"
            if unreadable
            else "has no product invoice on file"
        )
        label = order.order_id or order.id
        message = (
            f"Order {label} was delivered {age} days ago and {missing}"
            if dated
            else (
                f"Order {label} is delivered and {missing}; Flipkart showed no delivery "
                f"date, and the order is {age} days old"
            )
        )
        findings.append(
            Finding(
                key=f"product_invoice_missing:order:{order.id}",
                message=f"{message} ({_who(email, order.account_id)})",
                severity=AlertSeverity.warning,
                account_id=order.account_id,
                order_id=order.id,
                details={
                    "days": age,
                    "delivered_on": moment.date().isoformat() if dated and moment else None,
                    "other_invoices_on_file": sum(len(each) for each in invoices.values()),
                    "unreadable_invoices": unreadable,
                    "grace_days": int(grace_days),
                },
            )
        )
    return findings


#: Every rule, in the order ``evaluate`` runs them. Adding a rule is a function
#: plus a line here — nothing else in the engine or the API has to change.
RULES: tuple[Rule, ...] = (
    Rule("order_over_target", find_over_target_orders),
    Rule("order_failed", find_failed_orders),
    Rule("order_unconfirmed", find_unconfirmed_orders),
    Rule("delivery_overdue", find_overdue_deliveries),
    Rule("session_invalid", find_invalid_sessions),
    Rule("stale_sync", find_unsynced_orders),
    Rule("address_mismatch", find_address_mismatches),
    Rule("address_change", find_address_changes),
    Rule("gst_mismatch", find_gst_mismatches),
    Rule("invoice_amount_mismatch", find_invoice_amount_mismatches),
    Rule("product_invoice_missing", find_missing_product_invoices),
)

#: The kinds the rules own. Nothing else may raise alerts under these — see the
#: module docstring: ``evaluate`` resolves what the owning rule stops reporting.
RULE_KINDS: tuple[str, ...] = tuple(rule.kind for rule in RULES)


# --- Raising and resolving ----------------------------------------------------
def _write(
    alert: Alert | None,
    finding: Finding,
    kind: str,
    now: datetime,
    *,
    accepted_stays_closed: bool = True,
) -> Alert:
    """Create or refresh the row for one finding; never commits.

    An existing row is overwritten with the *current* reading, including
    ``details``, because the row is the state of the problem now and not a log
    of past sightings — ``created_at`` keeps the first sighting, ``last_seen_at``
    the latest, and ``updated_at`` moves only when something about the alert
    changed, so a pass that finds everything as it was leaves the queue's order
    and ages alone. "Changed" is judged on the details less the keys that move
    by themselves (:data:`VOLATILE_DETAIL_KEYS`); the message is derived from
    them, so it is refreshed but not compared.

    A row a *rule* had closed reopens here: a problem that comes back must be
    the same row, or the queue stops being a count of real issues. A row a
    *person* closed stays closed while the problem persists — their close is
    an acceptance (spec C-6), and a queue nobody can keep clear gets ignored —
    and is left exactly as they closed it, only marked as seen again; unless
    ``accepted_stays_closed`` is False, which is how an ad-hoc
    :func:`raise_alert` insists on being seen.
    """
    if alert is None:
        return Alert(
            key=finding.key,
            kind=kind,
            severity=finding.severity,
            message=finding.message,
            account_id=finding.account_id,
            order_id=finding.order_id,
            details=finding.details,
            resolved=False,
            created_at=now,
            updated_at=now,
            last_seen_at=now,
        )

    if alert.resolved and accepted_stays_closed and alert.resolved_by == RESOLVED_BY_USER:
        alert.last_seen_at = now
        return alert

    changed = (
        alert.kind != kind
        or alert.severity != finding.severity
        or alert.account_id != finding.account_id
        or alert.order_id != finding.order_id
        or _stable(alert.details) != _stable(finding.details)
    )
    alert.kind = kind
    alert.severity = finding.severity
    alert.message = finding.message
    alert.account_id = finding.account_id
    alert.order_id = finding.order_id
    alert.details = finding.details
    alert.last_seen_at = now
    if alert.resolved:
        alert.resolved = False
        alert.resolved_at = None
        alert.resolved_by = None
        alert.resolution_note = None
        changed = True
    if changed:
        alert.updated_at = now
    return alert


def _stable(details: dict | None) -> dict:
    """The details without the keys that move by themselves."""
    return {key: value for key, value in (details or {}).items() if key not in VOLATILE_DETAIL_KEYS}


def _close(
    alert: Alert, now: datetime, note: str | None = None, *, by: str = RESOLVED_BY_RULE, who: str | None = None
) -> None:
    """Mark one alert resolved; never commits.

    ``by`` says who: a rule, or a person (``RESOLVED_BY_USER``) with their
    reason in ``note``. The note is written either way — a rule's close clears
    one left from an earlier close, so a note never describes a close it has
    nothing to do with.
    """
    alert.resolved = True
    alert.resolved_at = now
    alert.updated_at = now
    alert.resolved_by = by
    alert.resolved_by_user = who if by == RESOLVED_BY_USER else None
    alert.resolution_note = note


def _by_key(db: DbSession, key: str) -> Alert | None:
    return db.scalar(select(Alert).where(Alert.key == key))


def raise_alert(
    db: DbSession,
    *,
    key: str,
    kind: str,
    message: str,
    severity: AlertSeverity = AlertSeverity.warning,
    account_id: int | None = None,
    order_id: int | None = None,
    details: dict | None = None,
) -> Alert:
    """Raise (or refresh) the alert identified by ``key``.

    Upserts: the same key never produces a second row, and a key that had been
    resolved reopens with ``resolved_at`` cleared — even one a person closed,
    because whoever raises an ad-hoc alert is the authority on it, unlike a
    rule that merely keeps seeing an accepted problem. Commits, so a job that
    spots a problem mid-run has it in the queue immediately even if the run
    later dies.
    """
    now = utcnow()
    finding = Finding(
        key=key,
        message=message,
        severity=severity,
        account_id=account_id,
        order_id=order_id,
        details=details,
    )
    existing = _by_key(db, key)
    alert = _write(existing, finding, kind, now, accepted_stays_closed=False)
    if existing is None:
        db.add(alert)
        log.info("alert raised %s (%s)", key, getattr(severity, "value", severity))
    db.commit()
    db.refresh(alert)
    return alert


def resolve_alert(db: DbSession, key: str, note: str | None = None, *, who: str | None = None) -> bool:
    """Close the alert with this key. True only if this call closed an open one.

    False for "no such alert" and for "already closed" alike: both mean there
    was nothing left to clear, which is all a caller can act on. ``note`` is
    kept on the row so a resolved alert says why, and ``who`` (the signed-in
    email) says by whom. A close by hand is an acceptance: the rules leave the
    row closed while the problem persists and reopen it only once the problem
    has gone away and come back.
    """
    alert = _by_key(db, key)
    if alert is None or alert.resolved:
        return False
    _close(alert, utcnow(), note=(note or "").strip() or None, by=RESOLVED_BY_USER, who=who)
    db.commit()
    log.info("alert resolved %s by %s", key, who or "?")
    return True


def resolve_many(db: DbSession, ids: Sequence[int], note: str | None = None, *, who: str | None = None) -> int:
    """Close every open alert among ``ids`` with one note, in one transaction;
    returns how many this call closed. Each close is the same acceptance as
    :func:`resolve_alert`'s — the rules leave it closed while the problem
    persists. An id that is unknown or already closed is passed over: clearing
    a queue of thirty must not stop at the one someone else closed meanwhile.
    """
    wanted = sorted({int(pk) for pk in ids})
    reason = (note or "").strip() or None
    now = utcnow()
    closed = 0
    for start in range(0, len(wanted), _KEY_CHUNK):
        rows = db.scalars(
            select(Alert).where(Alert.id.in_(wanted[start : start + _KEY_CHUNK]), Alert.resolved.is_(False))
        ).all()
        for alert in rows:
            _close(alert, now, note=reason, by=RESOLVED_BY_USER, who=who)
            closed += 1
    db.commit()
    if closed:
        log.info("%d alert(s) resolved together by %s", closed, who or "?")
    return closed


def reopen_alert(db: DbSession, key: str) -> bool:
    """Reopen a closed alert. True only if this call reopened a closed one.

    The operator's "no, this still needs attention". The next :func:`evaluate`
    will close it again if the data says the condition is gone — the rules are
    the authority on rule-owned kinds, by design. The note of the close being
    undone goes with it: it described that close, not whatever comes next.
    """
    alert = _by_key(db, key)
    if alert is None or not alert.resolved:
        return False
    alert.resolved = False
    alert.resolved_at = None
    alert.resolved_by = None
    alert.resolved_by_user = None
    alert.resolution_note = None
    alert.updated_at = utcnow()
    db.commit()
    log.info("alert reopened %s", key)
    return True


def _chunks(keys: list[str], size: int) -> Iterator[list[str]]:
    """Split a key list into IN-clause-sized pieces (see :data:`_KEY_CHUNK`)."""
    for start in range(0, len(keys), max(1, size)):
        yield keys[start : start + size]


def _existing_for(
    db: DbSession, kind: str, findings: Sequence[Finding]
) -> dict[str, Alert]:
    """The rows ``_apply`` may have to touch, keyed by ``key``.

    Everything of this ``kind`` comes back in one query — that is the set the
    rule owns and may have to resolve — rather than one lookup per key: a rule
    can report thousands of findings (37k accounts), and a query per key is the
    difference between a pass that finishes and one that times out.

    Keys that are *not* in that set are then looked up by key, chunked. This
    second query is what keeps a key that has drifted to another ``kind`` from
    being inserted twice: ``key`` is unique, so the INSERT would fail the whole
    pass with an integrity error. :func:`raise_alert` takes an arbitrary kind,
    so that drift is reachable from outside this module and cannot be ruled out
    by convention alone. Normally it finds nothing and costs one query.
    """
    existing = {
        alert.key: alert
        for alert in db.scalars(select(Alert).where(Alert.kind == kind)).all()
    }
    unknown = [finding.key for finding in findings if finding.key not in existing]
    for chunk in _chunks(unknown, _KEY_CHUNK):
        for alert in db.scalars(select(Alert).where(Alert.key.in_(chunk))).all():
            existing[alert.key] = alert
    return existing


def _apply(
    db: DbSession, kind: str, findings: Sequence[Finding], *, now: datetime
) -> int:
    """Make the open alerts of one kind exactly ``findings`` (less the ones a
    person has accepted); never commits. Returns how many are open afterwards."""
    existing = _existing_for(db, kind, findings)

    seen: set[str] = set()
    for finding in findings:
        seen.add(finding.key)
        alert = _write(existing.get(finding.key), finding, kind, now)
        if finding.key not in existing:
            db.add(alert)
            existing[finding.key] = alert

    # Everything of this kind the rule no longer reports is fixed. Closing it
    # here is the half of the job that keeps the queue clearable. A row that
    # ``_existing_for`` pulled in by key rather than by kind is safe from this:
    # it was looked up *because* its key is in the finding set, so it is in
    # ``seen`` by now.
    for key, alert in existing.items():
        if key in seen:
            continue
        if not alert.resolved:
            _close(alert, now)
        elif alert.resolved_by == RESOLVED_BY_USER:
            # A problem a person accepted has gone away by itself. The
            # acceptance is spent — if the problem comes back, the row reopens
            # — and the row now reads as a rule's close, which carries no note.
            alert.resolved_by = RESOLVED_BY_RULE
            alert.resolved_by_user = None
            alert.resolution_note = None
            alert.updated_at = now

    # Open after the write: a finding a person has accepted is not in the queue.
    return sum(1 for key in seen if not existing[key].resolved)


def evaluate(db: DbSession) -> dict[str, int]:
    """Run every rule, raising and resolving, and report ``{kind: open count}``.

    Every registered kind is in the result, zeros included, so a caller can
    render a stable set of counters instead of guessing which keys exist.
    Commits once: a pass is one transaction, so the queue is never read halfway
    through a re-evaluation.

    One ``now`` is used throughout, which makes the pass atomic in time — two
    alerts raised by the same pass sort together rather than by microsecond.
    Passes are serialised on :data:`_PASS_LOCK`: see there.
    """
    with _PASS_LOCK:
        now = utcnow()
        counts: dict[str, int] = {}
        for rule in RULES:
            counts[rule.kind] = _apply(db, rule.kind, list(rule.find(db)), now=now)
        db.commit()
    log.info(
        "alerts evaluated: %s",
        ", ".join(f"{kind}={count}" for kind, count in counts.items()) or "(no rules)",
    )
    return counts


def evaluate_in_own_session() -> dict[str, int]:
    """Run the rules from outside a request: at startup, and when a read-back
    finishes — a delivery that just arrived must close its overdue alert
    without anyone pressing "Evaluate now".

    Never the reason the caller fails: a rule that chokes is logged, and the
    sync's result or the app's start stands.
    """
    from app.db.base import SessionLocal  # local: base imports the models this module uses

    db = SessionLocal()
    try:
        return evaluate(db)
    except Exception:  # noqa: BLE001 — logged, never fatal to the caller
        log.exception("alert evaluation failed")
        return {}
    finally:
        db.close()


# --- Reading the queue --------------------------------------------------------
def open_count(db: DbSession) -> int:
    """How many alerts are open, across every kind."""
    return int(
        db.scalar(
            select(func.count()).select_from(Alert).where(Alert.resolved.is_(False))
        )
        or 0
    )


def summary(db: DbSession) -> dict:
    """Counts for the queue header: ``open``, ``resolved`` and two breakdowns.

    The breakdowns cover **open** alerts only — a queue is about what is still
    outstanding, and mixing closed rows in would make "3 critical" unreadable.
    ``by_severity`` always carries all three severities so the UI's badges do
    not appear and disappear; ``by_kind`` carries only kinds that are actually
    open, because kind is free text (an ad-hoc alert can invent one) and there
    is no fixed list to zero-fill.
    """
    rows = db.execute(
        select(Alert.severity, Alert.kind, func.count())
        .where(Alert.resolved.is_(False))
        .group_by(Alert.severity, Alert.kind)
    ).all()

    by_severity = {severity.value: 0 for severity in AlertSeverity}
    by_kind: dict[str, int] = {}
    total_open = 0
    for severity, kind, count in rows:
        number = int(count or 0)
        total_open += number
        name = getattr(severity, "value", str(severity))
        by_severity[name] = by_severity.get(name, 0) + number
        by_kind[kind] = by_kind.get(kind, 0) + number

    resolved = int(
        db.scalar(
            select(func.count()).select_from(Alert).where(Alert.resolved.is_(True))
        )
        or 0
    )
    return {
        "open": total_open,
        "resolved": resolved,
        "by_severity": by_severity,
        # Worst first, name breaking ties: the operator reads the top of this.
        "by_kind": dict(sorted(by_kind.items(), key=lambda item: (-item[1], item[0]))),
    }
