"""Read an account's real order state back off Flipkart (Phase 10).

Until now the tool recorded what it ORDERED and never looked again. This is the
read side: open the account's orders list, take what each row shows, then follow
each row to the order's OWN page for the ids, seller, amount and timeline — the
columns the operator's portal reports per order unit.

Two live findings (2026-10-23, signed in) shape the whole module:

* ``ORDER_ID`` / ``ORDER_MODEL`` / ``ORDER_COLOR`` / ``ORDER_AMOUNT`` match ZERO
  elements on ``/account/orders``. Reading them there is the exact bug that
  recorded blanks for every placed order, so detail fields are only ever read
  after we have confirmed the browser is standing on an order's own page.
* The row's ``href`` carries ``order_id``, ``item_id`` and ``unit_id`` as query
  parameters, so the ids are parsed from the link rather than scraped — cheaper
  and immune to class-name churn.

``platform_status`` keeps Flipkart's own wording and is never mapped onto
``OrderStatus``: a platform state nobody has seen yet must not be silently
flattened into the wrong bucket. Callers that need a decision consult
``DELIVERED_STATES`` / ``CANCELLED_STATES``.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from playwright.async_api import Page

from app.automation.ordering import parse_amount
from app.core import order_status
from app.core import selectors as S

log = logging.getLogger("ordering.sync")


# --- Platform states ---------------------------------------------------------
#: Terminal states, for callers that must decide something (close an order,
#: raise an alert). Deliberately tight and compared on ``text.strip().lower()``:
#: anything Flipkart invents later stays unrecognised rather than being guessed
#: into one of these buckets.
DELIVERED_STATES = frozenset({"delivered"})
CANCELLED_STATES = frozenset({"cancelled", "canceled"})

# Decided by the portal's one shared reading (app.core.order_status) rather
# than by the sets above: this module, the alert rules and the orders table
# each kept their own list until 2026-10-24 and disagreed, and "Cancelled -
# Order shipped" matched none of them.
is_delivered = order_status.is_delivered
is_cancelled = order_status.is_cancelled


class OrderListUnavailable(RuntimeError):
    """Flipkart answered the orders page with its own error ("Something's not
    right! Please try again") however often it was asked. Raised instead of an
    empty list so the read-back marks the account failed, not "0 orders"."""


#: Times the orders page is loaded again when Flipkart shows its error page,
#: and the wait before each (growing: 2 s, then 4 s).
ORDER_LIST_RETRIES = 2
ORDER_LIST_RETRY_WAIT_MS = 2_000
_FLIPKART_ERROR = re.compile(r"something'?s not right", re.IGNORECASE)


async def shows_flipkart_error(page: Page) -> bool:
    """Whether the page is Flipkart's "Something's not right! Please try again"."""
    try:
        text = await page.locator("body").inner_text(timeout=5_000)
    except Exception:  # noqa: BLE001
        return False
    return bool(_FLIPKART_ERROR.search((text or "").replace("’", "'")))


class NotSignedIn(RuntimeError):
    """The account's session is gone: Flipkart bounced the orders page to login.
    Raised instead of an empty list so a read-back never reports a dead session
    as a clean account with no orders."""


# --- Result ------------------------------------------------------------------
@dataclass
class ScrapedOrder:
    """One order unit as Flipkart currently reports it.

    Every field is optional: a sync must never fail because one column was
    missing, and a half-read order is still worth recording.
    """

    order_id: str | None = None
    item_id: str | None = None
    unit_id: str | None = None
    model: str | None = None
    color: str | None = None
    seller: str | None = None
    amount: float | None = None
    #: Flipkart's raw wording ("Out for Delivery", "Delivered", "Cancelled").
    platform_status: str | None = None
    #: The delivered date for a delivered unit, otherwise the promised ETA —
    #: consult ``platform_status`` / ``DELIVERED_STATES`` before reading it as
    #: "this arrived on".
    delivery_date: datetime | None = None
    delivery_otp: str | None = None
    #: The courier and its tracking id, from the order's "See All Updates"
    #: history: the line right under its "Shipped" header, "Ekart Logistics -
    #: FMPC…" (seen live 27 Oct 2026). The order page itself shows neither.
    #: :func:`parse_shipment`.
    tracking_id: str | None = None
    shipment_partner: str | None = None
    invoice_url: str | None = None
    #: The address block of the order's page as one line ("Work · A-39, … 201307
    #: · Akansha Agarwal · 8957768866") and the pincode found in it — what the
    #: address-mismatch and address-change alerts compare (spec FR-044/045).
    delivery_address: str | None = None
    delivery_pincode: str | None = None
    #: "GST details" on the order page: the GSTIN the order was billed under
    #: and the business name beside it (seen live 25 Oct 2026).
    billed_gstin: str | None = None
    billed_business_name: str | None = None
    #: Why Flipkart cancelled the order, in its own words ("Sorry, your order
    #: was cancelled as per seller policy restrictions applicable to this
    #: brand.", seen live 24–25 Oct 2026) — see :func:`parse_cancel_reason`.
    cancel_reason: str | None = None
    #: The two delivery dates kept apart (26 Oct 2026): the day Flipkart
    #: promises while the order is on its way ("Delivery expected by Nov 01"),
    #: and the day it says the order was delivered. ``delivery_date`` keeps
    #: whichever of the two applies, as it always has.
    promised_date: datetime | None = None
    delivered_date: datetime | None = None
    #: The order page's price details (seen live 26 Oct 2026): how it is paid,
    #: "Paid By" — "Cash On Delivery", a card — and its "Total amount", paid or
    #: to be paid on delivery that way. See :func:`parse_price_details`.
    payment_method: str | None = None
    amount_paid: float | None = None
    #: The order page's timeline, step by step and in page order: each a dict
    #: ``{"status": "Shipped", "date": "2026-10-22T00:00:00"}`` — ``date`` None
    #: for a step that has not happened yet (Flipkart dates only those that
    #: have). What the operator sees as the order's history (26 Oct 2026).
    timeline: list[dict] | None = None
    #: The row's raw href, so a caller can re-open this exact unit later.
    detail_href: str | None = None


# --- Ids from the row link ---------------------------------------------------
#: Flipkart's own parameter names, plus the camelCase spellings seen in some of
#: its links. Order and extra parameters are irrelevant — we only look up keys.
_ID_PARAMS: dict[str, tuple[str, ...]] = {
    "order_id": ("order_id", "orderId"),
    "item_id": ("item_id", "itemId"),
    "unit_id": ("unit_id", "unitId"),
}


def parse_order_ids(href: str) -> dict:
    """Pull ``{order_id, item_id, unit_id}`` out of an order row's href.

    These are the three ids the operator's portal reports per order unit, and
    the link carries all of them. Missing (or junk input) gives ``None`` for
    that id rather than raising — a row we cannot identify must not abort a run.
    """
    ids: dict[str, str | None] = dict.fromkeys(_ID_PARAMS)
    if not href:
        return ids

    try:
        params = parse_qs(urlsplit(href).query)
    except ValueError as exc:  # malformed URL; nothing to read
        log.warning("sync: could not parse ids from %r: %s", href, exc)
        return ids

    for field, aliases in _ID_PARAMS.items():
        for alias in aliases:
            values = [v.strip() for v in params.get(alias, []) if v.strip()]
            if values:
                ids[field] = values[0]
                break
    return ids


# --- Status line -------------------------------------------------------------
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_ALT = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    "|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
#: "Oct 31, 2018" / "Oct 28" / "31 Oct 2018" / "31st Oct". Both orders, an
#: optional ordinal suffix and an optional year — nothing else is accepted,
#: because a half-recognised date is worse than no date.
#: The ``(?!\d)`` guards keep a day from being chewed out of a longer number:
#: without them "May 2018" reads as May 20th.
_DATE_RE = re.compile(
    rf"\b(?:(?P<mon1>{_MONTH_ALT})\.?\s+(?P<day1>\d{{1,2}})(?!\d)(?:st|nd|rd|th)?"
    rf"|(?P<day2>\d{{1,2}})(?!\d)(?:st|nd|rd|th)?\s+(?P<mon2>{_MONTH_ALT})\.?)"
    # A year as four digits ("Oct 31, 2018"), or as two after an apostrophe,
    # the way the order's history prints it ("Sun, 25th Oct '26").
    rf"(?:\s*,?\s*(?P<year>\d{{4}})(?!\d)|\s*['’]\s*(?P<yy>\d{{2}})(?!\d))?",
    re.IGNORECASE,
)
#: Wording that makes a status line's date a promise, not an event:
#: "Delivery expected by Nov 01", "Arriving by Mon Oct 28".
_EXPECTATION_RE = re.compile(r"\b(?:expected|arriving|delivery\s+by)\b", re.IGNORECASE)

#: Words that only join a status to its date ("Delivered on", "Arriving by"),
#: plus weekday names, which Flipkart prefixes to ETAs ("Arriving by Mon Oct 28"),
#: plus the relative days it uses for recent events ("Cancelled Today, Oct 24").
#: Seen live 2026-10-24: without "today" here, two cancelled orders synced as
#: status "Cancelled Today", which is not "Cancelled", and so stayed *placed*.
_STATUS_TAIL_WORDS = frozenset(
    {"on", "by", "at", "from", "mon", "tue", "tues", "wed", "thu", "thur",
     "thurs", "fri", "sat", "sun", "monday", "tuesday", "wednesday",
     "thursday", "friday", "saturday", "sunday", "today", "yesterday", "tomorrow"}
)
#: A relative day standing in for the date ("Arriving Tomorrow").
_RELATIVE_DAY_RE = re.compile(r"\b(today|yesterday|tomorrow)\b", re.IGNORECASE)
_RELATIVE_DAY_OFFSET = {"today": 0, "yesterday": -1, "tomorrow": 1}

#: How far ahead a year-less date may land before we read it as last year's.
#: Flipkart drops the year on nearby dates, and an ETA is days or weeks out at
#: most — so "Dec 28" seen in early January is last December's delivery, not a
#: delivery eleven months from now. (The mirror case, a year-less date far in
#: the PAST, is left alone: those are genuinely old orders, and rolling them
#: forward would invent a future delivery date.)
_NO_YEAR_FUTURE_GRACE_DAYS = 30
#: The mirror rule for a promise ("Expected By Sat 2nd Jan", read on 29 Dec):
#: an expected date lands this year unless that is this far in the past, when
#: it is next year's. One overdue by less is still this year's.
_EXPECTED_PAST_GRACE_DAYS = 60


def parse_date(text: str | None, *, today: datetime | None = None, expected: bool = False) -> datetime | None:
    """Best-effort date out of a Flipkart date fragment; ``None`` if unsure.

    ``today`` is injectable so the year-less branch can be tested without
    depending on when the suite runs. ``expected`` says the date is a promise,
    days or weeks ahead, so a year-less one is never read as last year's.
    """
    if not text:
        return None
    match = _DATE_RE.search(text)
    if match is None:
        return None

    name = (match.group("mon1") or match.group("mon2") or "").lower()
    month = _MONTHS.get(name[:3])
    if month is None:
        return None
    day = int(match.group("day1") or match.group("day2"))
    year_text = match.group("year") or (f"20{match.group('yy')}" if match.group("yy") else None)
    now = today or datetime.now()

    try:
        if year_text:
            return datetime(int(year_text), month, day)
        candidate = datetime(now.year, month, day)
        if expected:
            if (now - candidate).days > _EXPECTED_PAST_GRACE_DAYS:
                return datetime(now.year + 1, month, day)
            return candidate
        if (candidate - now).days > _NO_YEAR_FUTURE_GRACE_DAYS:
            return datetime(now.year - 1, month, day)
        return candidate
    except ValueError:  # 31 Feb, day 99, a leap day in a non-leap year...
        log.info("sync: unusable date in %r", text)
        return None


def _clean_status(text: str) -> str:
    """Strip the connective tail a date leaves behind ("Delivered on" -> ...)."""
    words = text.replace(",", " ").split()
    while words and words[-1].strip(".").lower() in _STATUS_TAIL_WORDS:
        words.pop()
    return " ".join(words).strip(" ,-–:")


def parse_status_line(
    text: str, *, today: datetime | None = None
) -> tuple[str | None, datetime | None]:
    """Split a row's status line into raw status and date.

    "Delivered on Oct 31, 2018" -> ("Delivered", 2018-10-31)
    "Arriving by Mon Oct 28"    -> ("Arriving", Oct 28 of the inferred year)
    "Out for Delivery"          -> ("Out for Delivery", None)

    Never raises: an unreadable date gives ``None`` for the date and keeps
    whatever status wording we did see.
    """
    if not text:
        return None, None

    collapsed = " ".join(text.split())
    match = _DATE_RE.search(collapsed)
    if match is None:
        # "Arriving Tomorrow" / "Cancelled Today": the day is relative, and
        # only readable against a known today.
        relative = _RELATIVE_DAY_RE.search(collapsed)
        when = None
        if relative is not None:
            now = today or datetime.now()
            when = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                days=_RELATIVE_DAY_OFFSET[relative.group(1).lower()]
            )
            collapsed = collapsed[: relative.start()]
        return _clean_status(collapsed) or None, when

    status = _clean_status(collapsed[: match.start()])
    expected = bool(_EXPECTATION_RE.search(collapsed[: match.start()]))
    return status or None, parse_date(match.group(0), today=today, expected=expected)


# --- Delivery OTP ------------------------------------------------------------
#: Must name an OTP specifically. A bare "delivery" label is not enough: the
#: email reader was burnt by exactly this, returning a six-digit run from
#: unrelated text as a code, and a detail page is full of numbers (amounts,
#: pincodes, order ids) sitting next to the word "delivery".
_OTP_LABEL_RE = re.compile(
    r"\b(?:otp|o\.t\.p\.?|one[\s-]*time\s*(?:password|code|pin)"
    r"|delivery\s*code)\b",
    re.IGNORECASE,
)
#: A standalone 4-8 digit run. The lookarounds keep us off slices of a longer
#: number, so an order id ("OD113741449375753000") can never yield a "code".
_OTP_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
#: How close to the label the digits must sit. Flipkart renders the code right
#: under its label; anything further away is some other number on the page.
_OTP_WINDOW_AFTER = 60
_OTP_WINDOW_BEFORE = 25
#: A currency marker immediately before the digits — "₹9891" sitting near OTP
#: wording is a price, not a code. Anchored on a word boundary so an ordinary
#: word ending in those letters ("hours") is not mistaken for "Rs".
_MONEY_BEFORE_RE = re.compile(r"(?:₹|\$|\b(?:rs|rs\.|inr))\s*$", re.IGNORECASE)


#: The same code drawn one digit to a box, as OTP widgets do: "4 8 2 7" once
#: the page's text is collapsed to single spaces.
_OTP_SPACED_RE = re.compile(r"(?<!\d)(\d(?: \d){3,7})(?!\d)")


def _code_in(window: str, exclude: frozenset[str] = frozenset()) -> str | None:
    """First standalone 4-8 digit run in ``window`` that is not an amount —
    nor one of ``exclude`` (the unit's own pincode, which can sit near the
    code) — then the same as single digits one space apart."""
    for pattern in (_OTP_CODE_RE, _OTP_SPACED_RE):
        for match in pattern.finditer(window):
            if _MONEY_BEFORE_RE.search(window[: match.start()]):
                continue
            code = match.group(1).replace(" ", "")
            if code in exclude:
                continue
            return code
    return None


#: The block runs from this heading to the next one (or the footer).
_ADDRESS_START = re.compile(r"^\s*Delivery details\s*$", re.IGNORECASE | re.MULTILINE)
_ADDRESS_END = re.compile(
    r"^\s*(Price details|Payment details|Order summary|Chat with us|ABOUT)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
#: A pincode: six digits standing alone, the first digit not 0. The LAST such
#: run in the block is the pincode: an Indian address ends with it, and a plot
#: or society number earlier in the line can be six digits too. The ten-digit
#: phone on its own line never matches — the digit boundaries keep the
#: pattern off any slice of a longer number.
_PINCODE_RE = re.compile(r"(?<!\d)([1-9]\d{5})(?!\d)")
#: The most lines an address block can sensibly have; past that we are
#: reading something else.
_ADDRESS_MAX_LINES = 8
_ADDRESS_JOIN = " · "


def parse_delivery_address(text: str | None) -> tuple[str | None, str | None]:
    """``(address, pincode)`` from a detail page's text, or ``(None, None)``.

    Flipkart folds the address under a "Delivery details" heading; once
    opened it lists the address type, the address, the name and the phone,
    one per line, up to the next heading ("Price details"). Those lines are
    kept in order, joined with " · ", and the pincode is the last six-digit
    run among them (the address ends with it; a plot number may be six
    digits too) — so the snapshot reads the way the page showed it, and the
    alerts have a pincode to compare without parsing addresses.
    """
    if not text:
        return None, None
    start = _ADDRESS_START.search(text)
    if start is None:
        return None, None
    rest = text[start.end() :]
    end = _ADDRESS_END.search(rest)
    block = rest[: end.start()] if end else rest
    lines = [" ".join(line.split()) for line in block.splitlines()]
    lines = [line for line in lines if line][:_ADDRESS_MAX_LINES]
    if not lines:
        return None, None
    joined = _ADDRESS_JOIN.join(lines)
    runs = _PINCODE_RE.findall(" ".join(lines))
    return joined, (runs[-1] if runs else None)


#: "GST details" on the order page (seen live on Ayush's orders, 25 Oct 2026):
#: the heading, then "GST Number<GSTIN>" (the label and the number share a line
#: in the page's text), then the business name on its own line, then the next
#: block ("Order #…"). A GSTIN is 15 characters: state code, PAN, entity
#: number, "Z", check character.
_GST_START = re.compile(r"^\s*GST details\s*$", re.IGNORECASE | re.MULTILINE)
#: Not ``\b`` at the front: the page glues the label to the number ("GST
#: Number09AAUFT2204P1ZI"), so a lowercase letter may sit right before it;
#: only another digit or capital would mean this is a slice of something longer.
_GSTIN_RE = re.compile(r"(?<![0-9A-Z])(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])(?![0-9A-Z])")
_GST_END = re.compile(r"^\s*(Order #|Delivery details|Price details|Payment details|Chat with us|ABOUT)", re.IGNORECASE)
_GST_MAX_LINES = 4


def parse_gst_details(text: str | None) -> tuple[str | None, str | None]:
    """``(gstin, business_name)`` from a detail page's text, or ``(None, None)``.

    Only a GSTIN-shaped run under the "GST details" heading counts, and the
    business name is the next line after it — never a guess from elsewhere on
    the page, so an order placed without GST reads as having none.
    """
    if not text:
        return None, None
    start = _GST_START.search(text)
    if start is None:
        return None, None
    lines = [" ".join(line.split()) for line in text[start.end() :].splitlines()]
    lines = [line for line in lines if line][:_GST_MAX_LINES]
    gstin: str | None = None
    for index, line in enumerate(lines):
        if _GST_END.match(line):
            break
        match = _GSTIN_RE.search(line)
        if match is None:
            continue
        gstin = match.group(1)
        following = lines[index + 1 : index + 2]
        name = following[0] if following and not _GST_END.match(following[0]) else None
        return gstin, name
    return None, None


def parse_delivery_otp(text: str | None, *, exclude: frozenset[str] | set[str] | tuple[str, ...] = ()) -> str | None:
    """The delivery code from a detail page's text, or ``None``.

    Only digits that sit beside OTP wording count. The code normally appears
    just for out-for-delivery units, so ``None`` is the usual, healthy answer —
    inventing one from a nearby number would be far worse than returning
    nothing.
    """
    if not text:
        return None

    collapsed = " ".join(text.split())
    skip = frozenset(value for value in exclude if value)
    for label in _OTP_LABEL_RE.finditer(collapsed):
        after = collapsed[label.end() : label.end() + _OTP_WINDOW_AFTER]
        code = _code_in(after, skip)
        if code:
            return code
        # "123456 is your delivery OTP" puts the code in front of the label.
        start = max(0, label.start() - _OTP_WINDOW_BEFORE)
        code = _code_in(collapsed[start : label.start()], skip)
        if code:
            return code
    return None


# --- Why it was cancelled --------------------------------------------------------
#: The timeline's cancelled step as the order page prints it: "Cancelled, Oct 24",
#: "Cancelled, Today, Oct 25" (seen live 24–25 Oct 2026). A short line, so a
#: reason that itself opens with "Cancelled as per…" is not taken for the step,
#: and never the footer's "Cancellation & Returns".
_CANCELLED_STEP_RE = re.compile(r"^cancell?ed(?:\s*,.{0,30})?$", re.IGNORECASE)
#: Where the status block ends and the order's own blocks begin.
_STATUS_BLOCK_END_RE = re.compile(
    r"^(?:chat with us|send order details|gst details|order #|delivery details|price details)", re.IGNORECASE
)
#: The platform's other wording (spec: "Cancelled by user", "Cancelled by user - Order shipped").
_CANCELLED_BY_USER_RE = re.compile(r"cancell?ed by (?:the )?user\b.*", re.IGNORECASE)
#: What the unit row keeps of a reason.
CANCEL_REASON_MAX = 255


def parse_cancel_reason(text: str | None) -> str | None:
    """Why the order page says the order was cancelled, or ``None``.

    The line right after the timeline's "Cancelled, <date>" step, when it reads
    as a reason — it mentions cancelling, as "Sorry, your order was cancelled as
    per seller policy restrictions applicable to this brand." does (seen live
    24–25 Oct 2026); failing that, a "Cancelled by user…" wording. Only the
    status block is read, so nothing comes from the GST details, the address
    or the footer below it.
    """
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    end = next((index for index, line in enumerate(lines) if _STATUS_BLOCK_END_RE.match(line)), len(lines))
    block = lines[:end]
    for index, line in enumerate(block[:-1]):
        following = block[index + 1]
        if _CANCELLED_STEP_RE.match(line) and "cancel" in following.lower():
            return following[:CANCEL_REASON_MAX]
    for line in block:
        found = _CANCELLED_BY_USER_RE.search(line)
        if found:
            return found.group(0).strip()[:CANCEL_REASON_MAX]
    return None


# --- What was paid ---------------------------------------------------------------
_PRICE_DETAILS_RE = re.compile(r"^price details$", re.IGNORECASE)
#: Where the price details end: the page's footer.
_PRICE_DETAILS_END_RE = re.compile(r"^(?:offers earned|about|contact us)$", re.IGNORECASE)
#: What a payment method's wording is cut to.
PAYMENT_METHOD_MAX = 100


def parse_price_details(text: str | None) -> tuple[float | None, str | None]:
    """``(total amount, how it is paid)`` from the order page's price details,
    which list "Total amount" / "₹235" and "Paid By" / "Cash On Delivery", one
    to a line (seen live 26 Oct 2026). ``(None, None)`` with no such block."""
    lines = _lines(text)
    start = next((index for index, line in enumerate(lines) if _PRICE_DETAILS_RE.match(line)), None)
    if start is None:
        return None, None
    amount: float | None = None
    method: str | None = None
    block = lines[start + 1 :]
    for index, line in enumerate(block):
        if _PRICE_DETAILS_END_RE.match(line):
            break
        following = block[index + 1] if index + 1 < len(block) else None
        if following is None or _PRICE_DETAILS_END_RE.match(following):
            continue
        if line.lower() == "total amount":
            amount = _amount_or_none(following)
        elif line.lower() == "paid by":
            method = following[:PAYMENT_METHOD_MAX]
    return amount, method


# --- The order's history -------------------------------------------------------------
#: The link that lays the order's dated history over its page (seen live 26 Oct 2026).
_ALL_UPDATES_TEXT = "See All Updates"
#: A step's header in that history: the step's name, then the day it happened
#: ("Order ConfirmedSun, 25th Oct '26") or the day it is expected ("ShippedExpected
#: By Tue 27th Oct") — run together, as the page's text has them.
_HISTORY_STEP_RE = re.compile(
    r"^(order confirmed|order received|order placed|shipped|out for delivery|delivered|delivery|cancelled|returned|refunded)(.*)$",
    re.IGNORECASE,
)
_EXPECTED_RE = re.compile(r"^\s*expected\b", re.IGNORECASE)
#: Its final step, by either name: dated, the day it was delivered; expected, the promised day.
_DELIVERY_STEPS = frozenset({"delivery", "delivered"})
#: How long the history is given to appear once its link is pressed.
_HISTORY_WAIT_MS = 3_000


def _lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _history_step(line: str, *, today: datetime | None = None) -> tuple[str, datetime | None, bool] | None:
    """``(name, day, expected)`` for a header of the history, else None —
    an event line ("Item yet to be shipped.") names no step and is skipped."""
    match = _HISTORY_STEP_RE.match(line.strip())
    if match is None:
        return None
    name, rest = match.group(1), match.group(2).strip()
    if not rest:
        return None
    expected = bool(_EXPECTED_RE.match(rest))
    when = parse_date(rest, today=today, expected=expected)
    if when is None:
        return None
    return name, when, expected


#: A step line of the order page itself, dated: "Order Confirmed, Sun Oct 25",
#: "Cancelled, Today, Oct 26" (seen live on a cancelled order, 26 Oct 2026).
#: Events only — "Delivery, Sun Nov 01 By 11 PM" is the promise, never a step
#: that happened, and so is anything worded as one ("Shipped, Expected By Oct 27").
_PAGE_STEP_RE = re.compile(
    r"^(order confirmed|order received|order placed|shipped|out for delivery|delivered|cancelled|returned|refunded)\s*,",
    re.IGNORECASE,
)


def parse_page_steps(text: str | None, *, today: datetime | None = None) -> list[tuple[str, datetime]]:
    """The dated steps the order page prints in its status block, in order:
    what a page with no history panel — a cancelled order's — says happened."""
    lines = _lines(text)
    end = next((index for index, line in enumerate(lines) if _STATUS_BLOCK_END_RE.match(line)), len(lines))
    steps: list[tuple[str, datetime]] = []
    for line in lines[:end]:
        match = _PAGE_STEP_RE.match(line)
        if match is None or _EXPECTATION_RE.search(line):
            continue
        _status, when = parse_status_line(line, today=today)
        if when is not None:
            steps.append((match.group(1), when))
    return steps


def parse_history(
    lines: list[str], *, today: datetime | None = None
) -> tuple[list[tuple[str, datetime | None]], datetime | None, datetime | None]:
    """The order's history as ``(steps, promised day, delivered day)``: each
    step ``(name, the day it happened)`` in page order, None for a step still to
    come — the dated steps are the ones that have happened."""
    steps: list[tuple[str, datetime | None]] = []
    named: set[str] = set()
    promised: datetime | None = None
    delivered: datetime | None = None
    for line in lines:
        step = _history_step(line, today=today)
        if step is None:
            continue
        name, when, expected = step
        if name.lower() in named:
            continue  # a step's header comes first; a later line naming it is one of its events
        named.add(name.lower())
        steps.append((name, None if expected else when))
        if name.lower() in _DELIVERY_STEPS:
            if expected:
                promised = when
            else:
                delivered = when
    return steps, promised, delivered


#: The courier line right under a shipped order's "Shipped" header in its
#: history: "Ekart Logistics - FMPC6682013147" (seen live 27 Oct 2026). The
#: courier's name may carry a hyphen or brackets ("Xpress-Bees", "Delhivery
#: (Surface)") and the dash may be a long one; the id is the courier's:
#: capitals and digits, or digits alone (an AWB number).
_COURIER_LINE_RE = re.compile(
    r"^(?P<courier>[A-Za-z][A-Za-z0-9.&'() -]{0,60}?)\s+[-\u2013\u2014]\s+(?P<id>[A-Z0-9]{8,32})$"
)
#: Fewer digits than this and it is a name ("Seller - SUPERCOMNET"), not an id.
_TRACKING_MIN_DIGITS = 6


def parse_shipment(lines: list[str], *, today: datetime | None = None) -> tuple[str, str] | None:
    """``(courier, tracking id)`` from the order's history, or None: the line
    right under its first "Shipped" header, when it has that shape. Only that
    line: another one that happens to look like it is never taken for this
    delivery's, nor a return's pickup, which brings a courier and an id of its
    own under a later step."""
    for index, line in enumerate(lines):
        step = _history_step(line, today=today)
        if step is None or step[0].lower() != "shipped":
            continue
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        match = _COURIER_LINE_RE.match(following)
        if match and sum(char.isdigit() for char in match.group("id")) >= _TRACKING_MIN_DIGITS:
            return match.group("courier").strip(), match.group("id")
        return None
    return None


async def _history_lines(page: Page) -> list[str]:
    """The lines the order's history adds when its link is pressed; [] when
    the page has no such link or nothing opens. Read-only: the one control
    pressed is that link, by a click on that element alone."""
    try:
        link = page.get_by_text(_ALL_UPDATES_TEXT, exact=True)
        if not await link.count():
            return []
        before = set(_lines(await _field(page, _DETAIL_TEXT)))
        await link.first.evaluate("element => element.click()")
        deadline = time.monotonic() + _HISTORY_WAIT_MS / 1000
        while True:
            await page.wait_for_timeout(250)
            added = [line for line in _lines(await _field(page, _DETAIL_TEXT)) if line not in before]
            if any(_history_step(line) for line in added) or time.monotonic() >= deadline:
                return added
    except Exception as exc:  # noqa: BLE001 — the history is extra; the page was read without it
        log.warning("sync: could not open the order's history: %s", exc)
        return []


# --- Page plumbing -----------------------------------------------------------
#: Visible text of the order's own page, used for the OTP. There is no
#: confirmed OTP selector — no order in the account was out for delivery on
#: 2026-10-23 — so rather than invent a class name we read the page text and
#: require the label to be next to the digits.
_DETAIL_TEXT = "//body"
#: Best-effort invoice link, matched on visible wording rather than a class
#: because none was visible to confirm. Absent on older orders; see
#: selectors.py — ``invoice_url`` stays None instead of being guessed.
_INVOICE_LINK = '//a[contains(translate(., "INVOCE", "invoce"), "invoice")]'

#: Per-field read budget. Playwright's 30s default would turn one missing field
#: on each of 20 rows into a ten-minute sync, and a field that is not there is
#: not there — waiting longer does not help.
_FIELD_TIMEOUT_MS = 5_000
#: The list itself is worth waiting for; it is one navigation per account.
_LIST_TIMEOUT_MS = 60_000
#: How long a page is looked at for an invoice link it should not have yet —
#: an order on its way or cancelled. The page has rendered by then. Before
#: 26 Oct 2026 this one read carried no budget at all, so the browser's 30 s
#: default ran out on every such page: measured live, 30.0 s of a page's 31.4 s,
#: and ten minutes for one account's read-back of 20 pages.
_INVOICE_WAIT_MS = 1_500
#: The states with no invoice to offer yet: only these get the short look.
_NO_INVOICE_YET = frozenset({order_status.IN_TRANSIT, order_status.CANCELLED})
#: Guard against a pathological page; a real timeline has about five steps.
_MAX_TIMELINE_STEPS = 12

#: Amounts are read with the first number in the string: feeding a whole
#: "₹9,891 · 2 items" to parse_amount would concatenate the digits into 98912.
_AMOUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _amount_or_none(text: str | None) -> float | None:
    """'₹9,891' -> 9891.0; nothing numeric -> ``None``, never 0.0.

    ``parse_amount`` answers 0.0 when it cannot read a number, which would
    record a real order as free — so an unreadable amount stays unknown here.
    """
    if not text:
        return None
    match = _AMOUNT_RE.search(text)
    if match is None:
        return None
    amount = parse_amount(match.group(0))
    return amount if amount > 0 else None


async def _text(locator) -> str | None:
    """Text of one element, or ``None`` when it is not on the page."""
    try:
        value = await locator.first.inner_text(timeout=_FIELD_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — a missing field is normal
        return None
    return value.strip() or None


async def _field(page: Page, xpath: str) -> str | None:
    return await _text(page.locator(f"xpath={xpath}"))


async def _row_field(row, xpath: str) -> str | None:
    """Row-relative read, so two listed orders cannot bleed into each other."""
    return await _text(row.locator(f"xpath={xpath}"))


async def _goto(page: Page, url: str, *, attempts: int = 2) -> bool:
    """Navigate to ``url``; ``False`` if it never loaded.

    A Flipkart redirect still in flight aborts the first goto with
    ERR_ABORTED (the same race ``login.verify_logged_in`` retries through),
    which is not a reason to give up on an order, so this tries twice.
    """
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, wait_until="domcontentloaded")
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("sync: navigation to %s failed (try %d): %s", url, attempt, exc)
            if attempt < attempts:
                with contextlib.suppress(Exception):
                    await page.wait_for_timeout(1000)
    return False


def _strip_seller_label(text: str | None) -> str | None:
    """'Seller: Flashtech Retail' -> 'Flashtech Retail'."""
    if not text:
        return None
    if text.lower().startswith("seller"):
        _, _, rest = text.partition(":")
        return rest.strip() or None
    return text


async def _scrape_row(row) -> ScrapedOrder:
    """Everything one listed row shows, including the ids in its link."""
    href: str | None = None
    try:
        href = (await row.get_attribute("href")) or None
    except Exception as exc:  # noqa: BLE001
        log.warning("sync: could not read a row's href: %s", exc)

    ids = parse_order_ids(href or "")
    line = await _row_field(row, S.ORDER_STATUS_IN_ROW) or ""
    status, when = parse_status_line(line)
    state = order_status.classify(status)
    promise = state == order_status.IN_TRANSIT and bool(_EXPECTATION_RE.search(line))

    return ScrapedOrder(
        order_id=ids["order_id"],
        item_id=ids["item_id"],
        unit_id=ids["unit_id"],
        model=await _row_field(row, S.ORDER_MODEL_IN_ROW),
        color=await _row_field(row, S.ORDER_COLOR_IN_ROW),
        amount=_amount_or_none(await _row_field(row, S.ORDER_AMOUNT_IN_ROW)),
        platform_status=status,
        delivery_date=when,
        promised_date=when if promise else None,
        delivered_date=when if state == order_status.DELIVERED else None,
        detail_href=href,
    )


async def _status_from_timeline(
    page: Page,
) -> tuple[str | None, datetime | None]:
    """Current state from the detail page's timeline, or ``(None, None)``.

    Only steps that carry a date count. Flipkart stamps a date on the steps
    that have HAPPENED and leaves the rest undated, so taking the last step on
    the page would report a merely-shipped order as "Delivered" — precisely the
    silent mislabelling ``platform_status`` exists to avoid. If no step is
    dated we would be guessing which one is current, so we say nothing.
    """
    return latest_dated_step(await _timeline_steps(page))


async def _timeline_steps(page: Page) -> list[tuple[str | None, datetime | None]]:
    """Every step of the detail page's timeline, in page order, as the
    ``(status, date)`` pairs :func:`parse_status_line` reads off them."""
    steps = page.locator(f"xpath={S.ORDER_TIMELINE_STEPS}")
    try:
        count = await steps.count()
    except Exception as exc:  # noqa: BLE001
        log.warning("sync: could not count timeline steps: %s", exc)
        return []
    out: list[tuple[str | None, datetime | None]] = []
    for index in range(min(count, _MAX_TIMELINE_STEPS)):
        out.append(parse_status_line(await _text(steps.nth(index)) or ""))
    return out


def latest_dated_step(steps: list[tuple[str | None, datetime | None]]) -> tuple[str | None, datetime | None]:
    """The last step that carries a date — the one that has happened."""
    latest: tuple[str | None, datetime | None] = (None, None)
    for status, when in steps:
        if status and when:
            latest = (status, when)
    return latest


def timeline_as_dicts(steps: list[tuple[str | None, datetime | None]]) -> list[dict]:
    """The steps as the unit row keeps them: ``{"status", "date"}`` per step,
    the date ISO or None, steps with no wording left out."""
    return [
        {"status": status, "date": when.isoformat() if when is not None else None}
        for status, when in steps
        if status
    ]


async def _fill_from_detail(page: Page, order: ScrapedOrder) -> None:
    """Follow one row to its own page and complete the record in place.

    The detail page is authoritative for model/colour/amount/seller, but a
    field it does not show never overwrites what the row already gave us.
    """
    url = urljoin(S.ORDERS_URL, order.detail_href or "")
    if not await _goto(page, url):
        return

    landed = page.url or ""
    if S.ORDER_DETAIL_PATH not in landed:
        # A redirect (usually an expired session bouncing us to login) raced the
        # navigation. Reading ORDER_ID/MODEL/COLOR/AMOUNT off whatever we landed
        # on is the bug that blanked every order record, so stop here and keep
        # the row-level data we already have.
        log.error(
            "sync: %s did not open an order page (landed on %s); "
            "keeping row-level data only",
            order.detail_href,
            landed,
        )
        return
    await _read_detail(page, order)


async def _read_detail(page: Page, order: ScrapedOrder) -> None:
    """Complete ``order`` from the order page the browser is already on."""
    order.order_id = order.order_id or await _field(page, S.ORDER_ID)
    order.model = await _field(page, S.ORDER_MODEL) or order.model
    order.color = await _field(page, S.ORDER_COLOR) or order.color
    order.seller = _strip_seller_label(await _field(page, S.ORDER_SELLER)) or order.seller
    order.amount = _amount_or_none(await _field(page, S.ORDER_AMOUNT)) or order.amount

    steps = await _timeline_steps(page)
    if steps:
        order.timeline = timeline_as_dicts(steps) or None
    if not order.platform_status or not order.delivery_date:
        status, when = latest_dated_step(steps)
        order.platform_status = order.platform_status or status
        order.delivery_date = order.delivery_date or when

    # The address sits folded under its heading: open it before reading the
    # page's text, which also carries the OTP when there is one.
    await _reveal(page, S.DELIVERY_DETAILS_HEADING)
    body = await _field(page, _DETAIL_TEXT)
    # The page's own step lines carry the dates its timeline elements do not;
    # a page with no history panel (a cancelled order's) is dated from them,
    # and an order read by id, with no list row, takes its state from them.
    page_steps = parse_page_steps(body)
    if page_steps and not any(when for _status, when in steps):
        order.timeline = timeline_as_dicts(page_steps) or order.timeline
        order.platform_status = order.platform_status or latest_dated_step(page_steps)[0]
        delivered_on = next((when for name, when in page_steps if name.lower() == "delivered"), None)
        order.delivered_date = order.delivered_date or delivered_on
    address, pincode = parse_delivery_address(body)
    if address:
        order.delivery_address = address
        order.delivery_pincode = pincode
    # Read after the address, so the address's own pincode — six digits that
    # can sit near the code — is never taken for it.
    not_the_code = frozenset({pincode}) if pincode else frozenset()
    order.delivery_otp = parse_delivery_otp(body, exclude=not_the_code)
    gstin, business = parse_gst_details(body)
    if gstin:
        order.billed_gstin = gstin
        order.billed_business_name = business
    reason = parse_cancel_reason(body)
    if reason:
        order.cancel_reason = reason
    paid, method = parse_price_details(body)
    if paid is not None:
        order.amount_paid = paid
    if method:
        order.payment_method = method

    # A delivered order (or one whose state is unread or unfamiliar) is given
    # the whole field budget to show its invoice link; one still on its way, a
    # short look — its page has no link to wait for.
    expected = order_status.classify(order.platform_status) not in _NO_INVOICE_YET
    invoice = await _invoice_url(page, expected=expected)
    if invoice:
        order.invoice_url = invoice

    # Last, as it lays a panel over the page: the dated history. Its steps are
    # dated once they have happened, which the page's own timeline is not
    # (seen live 26 Oct 2026), so they replace that timeline when it opens.
    history_lines = await _history_lines(page)
    shipment = parse_shipment(history_lines)
    if shipment is not None:
        order.shipment_partner, order.tracking_id = shipment
    history, promised, delivered = parse_history(history_lines)
    if order.delivery_otp is None and history_lines:
        # Where Flipkart puts the code is still to be seen on a real order out
        # for delivery: the history panel is looked in as well as the page.
        order.delivery_otp = parse_delivery_otp("\n".join(history_lines), exclude=not_the_code)
    if history:
        order.timeline = timeline_as_dicts(history) or order.timeline
        if not order.platform_status:
            order.platform_status = latest_dated_step(history)[0]
    order.promised_date = order.promised_date or promised
    order.delivered_date = order.delivered_date or delivered
    order.delivery_date = order.delivery_date or delivered or promised
    if order.delivery_otp is None and _OUT_FOR_DELIVERY_RE.search(order.platform_status or ""):
        # Out for delivery and no code read: what both showed is kept, so the
        # code's place can be mapped from the first real one.
        _keep_page_text(order, "\n".join([body or "", *history_lines]))


#: A unit on the courier's van, as Flipkart words it.
_OUT_FOR_DELIVERY_RE = re.compile(r"out\s*for\s*delivery", re.IGNORECASE)
#: Where the page text of an out-for-delivery order with no readable code is
#: kept, so the code's place on the page can be mapped from a real one.
DIAGNOSTICS_DIR = "diagnostics"


def _keep_page_text(order: "ScrapedOrder", body: str | None) -> None:
    """Best effort: keep the text of an out-for-delivery order page on which
    no delivery code could be read. No order had been out for delivery when
    the reader was written (26 Oct 2026), so the first real page is the one
    that shows where Flipkart puts the code. Never raises."""
    import os
    from datetime import timezone

    try:
        os.makedirs(DIAGNOSTICS_DIR, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = re.sub(r"[^A-Za-z0-9]", "", order.order_id or "unknown")[:40] or "unknown"
        path = os.path.join(DIAGNOSTICS_DIR, f"{stamp}-out-for-delivery-{name}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write((body or "")[:20_000])
        log.warning("sync: %s is out for delivery but no delivery code was read; page text kept in %s", order.order_id, path)
    except Exception as exc:  # noqa: BLE001 — a diagnostic must never fail a read-back
        log.warning("sync: could not keep the page text of %s: %s", order.order_id, exc)


def toggle_of(xpath: str) -> str:
    """The nearest ``aria-expanded`` control around a heading: Flipkart's
    section headers are ``div[role=button][aria-expanded]`` toggles (seen on
    Ayush's cancelled orders, 25 Oct 2026), so the attribute says whether the
    block is open before anything is clicked."""
    return f"({xpath})/ancestor-or-self::*[@aria-expanded][1]"


async def _reveal(page: Page, xpath: str) -> None:
    """Open a folded heading so its block is in the page text; best effort.

    The heading is a toggle: on a page where the block starts open (every
    cancelled order's page does, with nothing under it) a click would fold
    it shut and the read would miss the block, so the click happens only
    when the toggle says it is closed, or says nothing. A page without the
    heading changes nothing. The lower blocks render about a second after
    the page loads, which the wait here covers.
    """
    heading = page.locator(f"xpath={xpath}").first
    try:
        await heading.wait_for(state="attached", timeout=_FIELD_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — no such heading is the common case
        return
    toggle = page.locator(f"xpath={toggle_of(xpath)}")
    with contextlib.suppress(Exception):
        if await toggle.count():
            state = await toggle.first.get_attribute("aria-expanded", timeout=_FIELD_TIMEOUT_MS)
            if (state or "").strip().lower() == "true":
                return
    try:
        await heading.click(timeout=_FIELD_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — a heading that cannot be clicked stays as it is
        return
    with contextlib.suppress(Exception):
        await page.wait_for_timeout(300)


async def _invoice_url(page: Page, *, expected: bool = True) -> str | None:
    """Absolute invoice link if the page offers one, else ``None``. Looked
    for for the field budget, or only briefly when none is ``expected``."""
    try:
        href = await page.locator(f"xpath={_INVOICE_LINK}").first.get_attribute(
            "href", timeout=_FIELD_TIMEOUT_MS if expected else _INVOICE_WAIT_MS
        )
    except Exception:  # noqa: BLE001 — no invoice link is normal on old orders
        return None
    if not href:
        return None
    return urljoin(page.url or S.ORDERS_URL, href)


async def scrape_orders(
    page: Page,
    *,
    limit: int = 20,
    open_details: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> list[ScrapedOrder]:
    """Read up to ``limit`` of an account's most recent orders.

    Rows are read first and the detail pages visited afterwards, in two passes:
    navigating away invalidates the row handles, and collecting the list up
    front means a detail page that fails costs only that order's extra fields.

    With ``open_details=False`` the browser never leaves the list — a cheap
    pass for "what changed?" that reads only what the rows show.

    ``should_stop`` is asked before each order's page. Once it says yes, the
    orders not yet opened keep what their rows showed and the browser stays
    where it is: how a read-back gives the account over to an order run that
    starts on it (app.jobs.account_guard).
    """
    if limit <= 0:
        return []

    if not await _goto(page, S.ORDERS_URL):
        return []
    if "/login" in (page.url or ""):
        # Same check as login.verify_logged_in: an expired session is bounced to
        # the login page, where every selector below matches nothing. Said as a
        # failure, not as "0 orders seen": a dead session silently stopping
        # every read-back while the screens stay green is the worse outcome
        # (audit F-8, 25 Oct 2026); the worker marks the session invalid.
        log.error("sync: not signed in — orders list bounced to login")
        raise NotSignedIn("the account is not signed in: Flipkart sent the orders page to its login page")

    rows = page.locator(f"xpath={S.ORDER_ROW}")
    count: int | None = None
    for attempt in range(1 + ORDER_LIST_RETRIES):
        try:
            await rows.first.wait_for(timeout=_LIST_TIMEOUT_MS)
            count = await rows.count()
            break
        except Exception as exc:  # noqa: BLE001
            if not await shows_flipkart_error(page):
                # A list with no rows and no complaint: the account has no orders.
                log.error("sync: orders list did not render any rows: %s", exc)
                return []
            # Flipkart's own "Something's not right! Please try again" page
            # (seen live 17 Nov 2026): read as "0 orders" it hid every order of
            # the account while the read-back said done. Try again, then fail.
            log.warning("sync: Flipkart's orders page said it went wrong (try %d); loading it again", attempt + 1)
            await page.wait_for_timeout(ORDER_LIST_RETRY_WAIT_MS * (attempt + 1))
            if not await _goto(page, S.ORDERS_URL):
                break
    if count is None:
        raise OrderListUnavailable(
            "Flipkart's orders page kept saying \"Something's not right\" — this account's orders were not read; "
            "the next read-back tries again"
        )

    scraped: list[ScrapedOrder] = []
    for index in range(min(count, limit)):
        try:
            scraped.append(await _scrape_row(rows.nth(index)))
        except Exception as exc:  # noqa: BLE001
            # One unreadable row must not cost us the other nine.
            log.error("sync: row %d unreadable, skipping it: %s", index, exc)

    if not open_details:
        return scraped

    opened = False
    for order in scraped:
        if should_stop is not None and should_stop():
            log.info("sync: stopped before %s's page; the rest is read back later", order.order_id)
            return scraped
        if not order.detail_href:
            log.warning("sync: order %s has no detail link", order.order_id)
            continue
        opened = True
        try:
            await _fill_from_detail(page, order)
        except Exception as exc:  # noqa: BLE001
            log.error("sync: detail page for %s failed: %s", order.order_id, exc)

    if opened and not (should_stop is not None and should_stop()):
        # Leave the browser on the list it started from, so a caller can carry
        # on with account-level work without re-navigating — unless an order
        # run has started on the account during the last page.
        await _goto(page, S.ORDERS_URL)

    log.info("sync: scraped %d order(s)", len(scraped))
    return scraped


# --- one order, by id ----------------------------------------------------------
#: Links on an order's page that point at its units — the same links the orders
#: list carries, which is how a unit's own ids are known at all. UNIT_LINKS
_UNIT_LINKS_JS = """() => {  /* UNIT_LINKS */
  return [...document.querySelectorAll('a[href*="unit_id="]')].map(a => a.href);
}"""


def detail_url(order_id: str) -> str:
    """The order's own page, by id alone — for an order the recent list does
    not reach (handed in to track, or older than the list's window)."""
    return urljoin(S.ORDERS_URL, f"{S.ORDER_DETAIL_PATH}?order_id={quote(order_id)}")


async def scrape_order(page: Page, order_id: str) -> list[ScrapedOrder]:
    """Read one order by id: one observation per unit its page links, or one
    for the order when the page links none.

    Nothing here is confirmed against a live page yet (no delivered order was
    available on 24 Oct 2026): the order page is expected to carry the same
    ``order_id``/``unit_id`` links the list does. Until that is seen, an order
    whose page shows no unit links is read as a single unit with no unit id.
    """
    url = detail_url(order_id)
    if not await _goto(page, url):
        return []
    landed = page.url or ""
    if "/login" in landed:
        log.error("sync: not signed in — order %s bounced to login", order_id)
        return []
    if S.ORDER_DETAIL_PATH not in landed:
        log.error("sync: order %s did not open an order page (landed on %s)", order_id, landed)
        return []

    try:
        hrefs = await page.evaluate(_UNIT_LINKS_JS) or []
    except Exception as exc:  # noqa: BLE001 — a page without the script is a page without links
        log.warning("sync: could not read unit links for %s: %s", order_id, exc)
        hrefs = []
    wanted = order_id.strip().upper()
    by_unit: dict[str, str] = {}
    for href in hrefs:
        ids = parse_order_ids(str(href))
        if (ids["order_id"] or "").strip().upper() == wanted and ids["unit_id"]:
            by_unit.setdefault(ids["unit_id"], str(href))

    if not by_unit:
        # The page may have landed on one unit's own address (Flipkart adding
        # item_id/unit_id to a bare order_id URL): those ids are this unit's.
        landed_ids = parse_order_ids(landed)
        same_order = (landed_ids["order_id"] or "").strip().upper() == wanted
        order = ScrapedOrder(
            order_id=wanted,
            item_id=landed_ids["item_id"] if same_order else None,
            unit_id=landed_ids["unit_id"] if same_order else None,
            detail_href=landed if same_order else url,
        )
        await _read_detail(page, order)
        return [order]

    units: list[ScrapedOrder] = []
    for unit_id, href in by_unit.items():
        ids = parse_order_ids(href)
        unit = ScrapedOrder(order_id=wanted, unit_id=unit_id, item_id=ids["item_id"], detail_href=href)
        try:
            await _fill_from_detail(page, unit)
        except Exception as exc:  # noqa: BLE001 — one unit's page failing costs that unit's extras
            log.error("sync: unit page for %s/%s failed: %s", wanted, unit_id, exc)
        units.append(unit)
    return units
