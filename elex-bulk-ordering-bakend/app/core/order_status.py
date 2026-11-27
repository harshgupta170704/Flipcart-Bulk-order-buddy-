"""One reading of a platform's order-status wording, shared by the whole portal.

Flipkart's status is stored verbatim ("Cancelled - Order shipped", "Out for
delivery", "Return started"...) and never rewritten. What the portal needs from
it — delivered or not, cancelled or not — used to be decided in three places
with two different answers: the sync scraper, the alert rules and the orders
table each kept their own list, and the table counted a return as a failure
while the backend counted it as neither. A dashboard built on one of those and a
table built on another would disagree. This module is the single answer; every
other part asks it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only: this module reads wording, not rows
    from app.db.models import OrderUnit

DELIVERED = "delivered"
CANCELLED = "cancelled"
RETURNED = "returned"      # return started / refunded
FAILED = "failed"          # undelivered, lost, returned to origin
IN_TRANSIT = "in_transit"  # anything live that is not yet delivered
UNKNOWN = "unknown"        # nothing recorded, or wording nothing here recognises

_PARTIAL = re.compile(r"\bpartial", re.IGNORECASE)

#: Wording that says Flipkart never took the order as a real one: the payment
#: did not go through, so nothing is on its way and nothing will be. Flipkart
#: gives such an attempt an order id all the same and words it "Order Not
#: Placed" (read back live on 28 Oct 2026, the day after a card payment the
#: bank never confirmed). Until 11 Nov 2026 the word "placed" in it read as on
#: its way: the order stood as placed, counted as to be delivered, and went
#: overdue. "Payment Failed" read as nothing at all, which counts as still to
#: be delivered too.
_NEVER_PLACED = re.compile(
    r"\bnot\s+placed\b"
    r"|\b(?:could\s*n[o']?t|can\s*not|cannot|unable\s+to)\s+(?:be\s+)?place"
    r"|\bpayment\s+(?:has\s+|was\s+|is\s+)?(?:failed|declined|unsuccessful|rejected)\b"
    r"|\b(?:failed|declined|unsuccessful)\s+payment\b"
    r"|\btransaction\s+(?:has\s+|was\s+)?(?:failed|declined)\b"
    r"|\border\s+failed\b",
    re.IGNORECASE,
)

#: "Cancelled", wherever the wording puts it: "Cancelled", "Order Cancelled",
#: "Cancelled - Order shipped", "Seller Cancelled", "Auto Cancelled". Past
#: tense only: "Cancellation requested" is a request the platform may still
#: refuse, so that unit keeps counting until the platform says "Cancelled".
_CANCELLED = re.compile(r"\bcancell?ed\b", re.IGNORECASE)
#: …unless what was cancelled is not the order: "Return Cancelled" /
#: "Replacement Cancelled" are a withdrawn return on a unit that was delivered
#: and stays delivered. Reading those as a cancellation would flip a placed
#: order to failed, one way, and hand its unit-limit quota back.
_NOT_THE_ORDER_CANCELLED = re.compile(
    r"\b(?:return|replacement|exchange|refund|pick\s*-?\s*up)\b[^.;]*\bcancell?ed\b", re.IGNORECASE
)

#: Checked in order, after the two readings above. "Undelivered" contains
#: "delivered" but is a failure, which is why DELIVERED is matched on the
#: whole word last.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (FAILED, re.compile(r"\b(?:undelivered|not\s+delivered|lost|rto|returned\s+to\s+origin)\b", re.IGNORECASE)),
    # The return / replacement flow, whatever stage it is at: the unit was
    # delivered and is now being sent back or swapped. Settled either way.
    (RETURNED, re.compile(r"\b(?:return|refund|replacement)", re.IGNORECASE)),
    (DELIVERED, re.compile(r"\bdelivered\b", re.IGNORECASE)),
    (
        IN_TRANSIT,
        re.compile(
            r"\b(?:shipped|dispatched|out\s+for\s+delivery|arriving|packed|ready\s+to\s+ship|"
            # "Delivery expected by Nov 01" — Flipkart's row wording for an
            # order on its way, read back live on 26 Oct 2026.
            r"delivery\s+expected|expected\s+delivery|"
            r"in\s+transit|order\s+confirmed|order\s+received|approved|purchased|pending|"
            # More of the same, each a unit still on its way: a courier's
            # failed first try is followed by another, and a delay or a new
            # slot is still a delivery to come.
            r"processing|processed|placed|delayed|rescheduled|delivery\s+attempted|handed\s+over|on\s+hold)\b"
            r"|^confirmed$",
            re.IGNORECASE,
        ),
    ),
)

#: The states in which nothing more will happen to the unit.
SETTLED = frozenset({DELIVERED, CANCELLED, RETURNED, FAILED})
#: The states that count as "still to be delivered".
UNDELIVERED = frozenset({IN_TRANSIT, UNKNOWN})


def classify(status: str | None) -> str:
    """The portal's reading of a platform status; UNKNOWN for blank or unfamiliar."""
    text = (status or "").strip()
    if not text:
        return UNKNOWN
    # "Partially delivered" is neither delivered nor not: unrecognised, on
    # purpose, rather than guessed into a bucket.
    if _PARTIAL.search(text):
        return UNKNOWN
    # An order that was never placed no longer stands, exactly like one that
    # was cancelled: nothing is to be delivered and its units stop counting.
    # Who cancelled and what was charged ask never_placed() to tell it apart.
    if _NEVER_PLACED.search(text):
        return CANCELLED
    # Cancellation before the rest: "Cancelled - Order shipped" mentions
    # shipping but is a cancellation.
    if _CANCELLED.search(text) and not _NOT_THE_ORDER_CANCELLED.search(text):
        return CANCELLED
    for state, pattern in _RULES:
        if pattern.search(text):
            return state
    return UNKNOWN


#: Wording where Flipkart has an order id, but bank payment has NOT yet been
#: confirmed (seen live 23 Nov 2026: "Payment not yet confirmed from the bank").
#: Such an order must stay unconfirmed until Flipkart confirms or fails it —
#: promoting it to placed falsely reports an unpaid order as bought.
_PAYMENT_UNCONFIRMED = re.compile(
    r"\bpayment\s+(?:not\s+(?:yet\s+)?confirmed|pending|awaiting|verification\s+pending|on\s+hold)\b"
    r"|\bawaiting\s+(?:bank\s+)?payment\b",
    re.IGNORECASE,
)


def payment_unconfirmed(*wordings: str | None) -> bool:
    """Whether any platform wording says bank payment is still unconfirmed."""
    return any(_PAYMENT_UNCONFIRMED.search(text) for text in wordings if text)


def never_placed(*wordings: str | None) -> bool:
    """Whether any of these platform wordings says the order was never placed
    — "Order Not Placed", "Payment Failed": the payment did not go through, so
    Flipkart holds an order id for an order that never was.

    Such a unit reads cancelled (:func:`classify`), so the order stops
    standing; this is what lets the alert, the order dialog and the Order
    Details API say "not placed, nothing charged" instead of "cancelled by
    Flipkart, refund due"."""
    return any(_NEVER_PLACED.search(text) for text in wordings if text)


def is_delivered(status: str | None) -> bool:
    return classify(status) == DELIVERED


def is_cancelled(status: str | None) -> bool:
    return classify(status) == CANCELLED


def is_settled(status: str | None) -> bool:
    """Delivered, cancelled, returned or failed: no delivery is still pending."""
    return classify(status) in SETTLED


#: Wording of a returned unit that is no refund done, whatever else it says:
#: a return or replacement withdrawn ("Return Cancelled", "Replacement
#: Cancelled"), a replacement, and a unit the courier took back undelivered
#: ("Return to Origin").
_NOT_A_REFUND = re.compile(r"cancel|replac|return\s*to\s*origin", re.IGNORECASE)
_REFUND = re.compile(r"refund", re.IGNORECASE)
_REFUND_DONE = re.compile(r"complet|credit|success", re.IGNORECASE)


def refund_completed(status: str | None) -> bool:
    """Whether Flipkart's wording says a unit's money is back: "Refund
    Completed", or a refund credited or successful.

    The order dialog's rule, kept here once for the backend: returnedPayment in
    frontend/src/components/OrderDetail.tsx says "— refunded" for exactly this
    wording. It is asked only of a unit the portal reads as returned (the
    dialog's paidLine), so this says no for any other reading too; then, in the
    dialog's order, a withdrawn return, a replacement or a return to origin is
    no refund done, and "refund" beside "complet", "credit" or "success" is.
    """
    text = status or ""
    return (
        classify(text) == RETURNED
        and not _NOT_A_REFUND.search(text)
        and bool(_REFUND.search(text))
        and bool(_REFUND_DONE.search(text))
    )


#: "Paid By" wording that is cash on delivery: the order dialog's own test
#: (PAID_ON_DELIVERY in frontend/src/components/OrderDetail.tsx).
_PAID_ON_DELIVERY = re.compile(r"cash on delivery|pay on delivery", re.IGNORECASE)


def paid_on_delivery(payment_method: str | None) -> bool:
    """Whether a unit's order page says it is paid at the door ("Paid By:
    Cash On Delivery"): the order dialog's rule, kept here once for the
    backend."""
    return bool(_PAID_ON_DELIVERY.search(payment_method or ""))


#: Wording that says the user — the account that bought — cancelled, not the
#: platform or its seller. The spec's statuses "Cancelled by user - Order
#: shipped" and "Cancelled by user - Order confirmed but not shipped"
#: (ui-evidence, the Order Status picker) lead the list; the other two are how
#: an order page words a cancellation the account asked for ("You requested a
#: cancellation because…", "as per your request"). Flipkart's own cancellation
#: reads otherwise — "Sorry, your order was cancelled as per seller policy
#: restrictions applicable to this brand." (seen live 24–25 Oct 2026) — and
#: matches none of them.
_BY_USER = re.compile(
    r"\bcancell?ed\s+by\s+(?:the\s+)?(?:user|buyer|customer|you)\b"
    r"|\byou\s+(?:have\s+)?(?:requested|asked\s+for)\s+(?:a\s+|the\s+|this\s+)?cancell?ation\b"
    r"|\b(?:as\s+per|at|on)\s+your\s+request\b",
    re.IGNORECASE,
)


def cancelled_by_user(*wordings: str | None) -> bool:
    """Whether any of these platform wordings — a status, a cancellation
    reason — says the user cancelled. False for blank wording: who cancelled
    is then simply not known."""
    return any(_BY_USER.search(text) for text in wordings if text)


#: Wording that says the platform or its seller cancelled: Flipkart's own
#: "Sorry, your order was cancelled as per seller policy restrictions
#: applicable to this brand." (seen live 24–25 Oct 2026), a seller's
#: cancellation, or stock the seller did not have.
_BY_PLATFORM = re.compile(
    r"\bsellers?\b|\bpolic(?:y|ies)\b|\bflipkart\b|\bout\s+of\s+stock\b|\b(?:un|non[-\s]?)availab",
    re.IGNORECASE,
)


def cancelled_by_platform(*wordings: str | None) -> bool:
    """Whether these wordings clearly say Flipkart or its seller cancelled —
    and not the user. False when they say neither: who cancelled is then not
    known, which the CSV leaves blank rather than answering "No"."""
    return not cancelled_by_user(*wordings) and any(_BY_PLATFORM.search(text) for text in wordings if text)


def days_late(promised: date | datetime | None, delivered: date | datetime | None) -> int | None:
    """How many days after its promise a unit was delivered: 0 when on time
    (or early), N when N days late, None while either date is unknown. By
    calendar day: Flipkart promises a day ("Delivery expected by Nov 01"), not
    an hour, and a delivery at 9 pm on that day is on time."""
    if promised is None or delivered is None:
        return None
    day = lambda moment: moment.date() if isinstance(moment, datetime) else moment  # noqa: E731
    return max(0, (day(delivered) - day(promised)).days)


def unit_days_late(unit: OrderUnit) -> int | None:
    """How late this unit came (:func:`days_late`): from the first promise
    Flipkart gave (``first_promised_date``; ``promised_date`` on a row read
    before the first was kept) to the day it was delivered (``delivered_date``).

    Whatever the unit reads now: ``delivered_date`` is only ever written from a
    delivery, so a unit delivered and since sent back keeps how late it came,
    and a unit never delivered has none. The one rule the ledger page and the
    CSV's Days Late both read, so the two cannot drift apart.
    """
    return days_late(unit.first_promised_date or unit.promised_date, unit.delivered_date)
