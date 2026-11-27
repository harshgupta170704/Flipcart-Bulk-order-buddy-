"""Ordering flow (ported from flipkart-ordering-tool/Orderingfunctions + main).

Sequence: buy-now → address → quantity → GST → payment method → enter card →
**price-target gate (PLAN.md §6a)** → submit → Flipkart's confirmation page.

The gate sits immediately before the submit — the only step that actually
orders. Entering the card does not charge; so we read the final payable total
on the confirm screen and, if it exceeds the target by any amount, we return
without ever submitting. Strict rule: order only if ``final_amount <= target``.

Submitting is two clicks for Cash on Delivery (Place Order, then the popup's
Confirm order) and one for a card (Pay). Only Flipkart's confirmation page
proves an order exists; a submit it never confirms is recorded as
``unconfirmed``, never as ``failed``.

A run may name the seller to buy from (``choose_seller``): the buy-now step
is then the BUY NOW of that seller's row on the product's sellers page, and
the checkout must not name another seller before any payment step.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.config import get_settings

from playwright.async_api import Page

from app.automation import challenges
from app.automation.product import read_product_preview
from app.automation.sellers import (
    choose_pid,
    count_matches,
    element_text,
    is_named_seller,
    read_product_sellers,
    read_sellers_page,
    row_problem,
    same_seller,
    seller_add_to_cart_xpath,
    seller_buy_now_xpath,
    seller_name_xpath,
    seller_row_xpath,
    sellers_named,
)
from app.core import selectors as S
from app.core.step_timer import mark as step_mark
from app.core.money import rupees
from app.db.models import OrderStatus

log = logging.getLogger("ordering.order")


# --- Inputs ------------------------------------------------------------------
@dataclass
class AddressInfo:
    name: str
    phone: str
    pin: str
    #: The address as one free-text line (the legacy checkout form took one).
    area: str
    #: The same address in parts, for Flipkart's current add-address form,
    #: which asks for the house/flat separately from the locality it reads
    #: off the map pin. Empty when the caller only had the one line.
    line1: str = ""
    floor: str = ""
    city: str = ""
    state: str = ""


@dataclass
class GstInfo:
    gstin: str
    business_name: str


@dataclass(repr=False)
class CardInfo:
    number: str
    month: str
    year: str
    cvv: str
    #: What the bank's own page asks for after Pay, when it asks for a password
    #: rather than a one-time code (HDFC's corporate cards); blank when none.
    password: str = ""

    def __repr__(self) -> str:
        """Masked, like the card sheet's own records: a stray log line or a
        printed object must not carry the card, its expiry, CVV or password."""
        tail = self.number[-4:] if len(self.number) >= 4 else ""
        return f"CardInfo(number='•••• {tail}', password={'set' if self.password else 'none'})"

    __str__ = __repr__


# --- Payment modes -----------------------------------------------------------
#: Pay with a card (the legacy behaviour; needs a CardInfo).
PAYMENT_CARD = "card"
#: Cash on Delivery — no card is involved at all.
PAYMENT_COD = "cod"

#: Gift cards cover the whole order.
PAYMENT_GV = "gv"
#: Gift cards first, the rest by card.
PAYMENT_GV_CARD = "gv_card"
#: Cash on Delivery with a card advance, RTGS and IMPS: chosen by their
#: wording on the payment page, when Flipkart offers them to the account.
PAYMENT_COD_CARD = "cod_card"
PAYMENT_RTGS = "rtgs"
PAYMENT_IMPS = "imps"

PAYMENT_MODES = (PAYMENT_CARD, PAYMENT_COD, PAYMENT_COD_CARD, PAYMENT_GV, PAYMENT_GV_CARD, PAYMENT_RTGS, PAYMENT_IMPS)
#: The modes that pay (part) by card and so need the card sheet.
CARD_MODES = (PAYMENT_CARD, PAYMENT_GV_CARD, PAYMENT_COD_CARD)
#: The modes that use gift cards and so need the voucher sheet.
GIFT_CARD_MODES = (PAYMENT_GV, PAYMENT_GV_CARD)


@dataclass
class GiftCardInfo:
    """One gift card for one order, in memory only — never written anywhere."""

    number: str
    pin: str

    def __repr__(self) -> str:  # never print a voucher
        return f"GiftCardInfo(number='****{self.number[-4:]}', pin='***')"

    __str__ = __repr__

#: How long any single page step may wait. Deliberately short: a selector that
#: is going to match does so in a second or two, so a long timeout only buys
#: time for selectors that are already dead. The legacy values (10s + 30s + 60s
#: per step) meant one failing order burned ~2.5 minutes before saying so.
STEP_TIMEOUT_MS = 12_000
#: The page we actually wait on — loading a checkout can genuinely be slow.
PAGE_TIMEOUT_MS = 30_000
#: For steps already known to be unreliable on the live site. Short enough that
#: a dead selector costs seconds, not minutes.
QUICK_TIMEOUT_MS = 4_000


# --- Pure helpers (unit-tested) ---------------------------------------------
def parse_amount(text: str | None) -> float:
    """'₹1,000.01' -> 1000.01. Strips currency symbol, commas, and spaces."""
    if not text:
        return 0.0
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    return float(cleaned) if cleaned else 0.0


def within_target(amount: float | None, target: float | None) -> bool:
    """Strict cap: True only if amount <= target. No target => always allowed.

    ``amount is None`` means we could not read the total, and that must FAIL
    CLOSED. The old code returned 0.0 on a read failure, and 0.0 <= target is
    True — so a broken selector silently approved every order. An unknown
    amount is never "within" a cap.
    """
    if amount is None:
        return False
    if target is None:
        return True
    return amount <= target


def per_unit(amount: float | None, units: int | None) -> float | None:
    """An order's amount for one unit, rounded to the paisa: the Final Amount
    Limit and the Cart Amount Limit are per unit, as BulkOrderBuddy's are (audit
    of 19 Nov 2026). An unknown amount stays unknown; fewer than one unit counts
    as one, so a cap is never divided into a looser one."""
    if amount is None:
        return None
    return round(amount / max(1, int(units or 1)), 2)


# --- Page steps --------------------------------------------------------------
async def _xpath(page: Page, xpath: str):
    return page.locator(f"xpath={xpath}")


#: Product-page coupons ("Coupons · ₹500 off · Apply"). Measured live on
#: 23–24 Nov 2026 (vivo T5 Lite, signed-in catbar accounts): the page arrives
#: as server-drawn HTML and Flipkart's own code wakes it up 1–4 s later (longer
#: on a busy machine). A press before that does nothing — no request is sent
#: and the tile still says "Apply" — and the wake-up then redraws the tile, so
#: the pressed element vanishes. The first version took "vanished" for
#: "applied": every order reported a coupon it never got and reached checkout
#: ₹500 over target. Now the step waits for the button to be live, presses it,
#: and believes only the pressed tile reading "Remove" afterwards. A live press
#: is a POST to /api/1/action/view; the tile turns a moment after the answer.
#:
#: Time: about 1.5 s with a coupon, 1–2 s without one. No press starts after
#: COUPON_READY_TIMEOUT_MS — or COUPON_SLOW_PAGE_TIMEOUT_MS while the page's
#: Buy now is still asleep (a loaded machine; Buy now's own retries run ~70 s)
#: — and a press then gets at most one confirm window plus the slow-answer
#: extra. Worst cases: ~32 s on a woken page; 60 s on one that never wakes
#: (whose Buy now would fail next anyway); a page that wakes late gets
#: COUPON_AFTER_WAKE_MS after waking.
COUPON_READY_TIMEOUT_MS = 20_000
COUPON_SLOW_PAGE_TIMEOUT_MS = 60_000
#: A page whose Buy now wakes late still gets this long after waking.
COUPON_AFTER_WAKE_MS = 5_000
#: How long one press gets to turn the tile when nothing reached Flipkart.
COUPON_CONFIRM_TIMEOUT_MS = 6_000
#: Extra time when the press reached Flipkart but its answer has not come back.
COUPON_SLOW_ANSWER_MS = 6_000
#: Once Flipkart has answered yes (2xx), how long the tile gets to read
#: "Remove" before the answer is taken as a no (a coupon this account cannot
#: have). A no is never pressed again: pressing harder does not change it.
COUPON_AFTER_ANSWER_MS = 3_000
#: Presses before giving up when they reach nobody, or Flipkart answers with
#: an error (busy, 429/5xx, dropped connection) — those are worth repeating.
COUPON_CLICK_ATTEMPTS = 3
COUPON_ERROR_PAUSE_MS = 1_000
COUPON_POLL_MS = 250
#: A page with no coupon tile is given this long after it is live, in case the
#: tile is drawn late, before the step stops looking.
COUPON_LATE_TILE_MS = 1_000
#: A page with no Flipkart page code at all (a test page) counts as live once
#: it has finished loading and stayed that way this long.
COUPON_PLAIN_PAGE_MS = 1_000
#: "This page has no coupon" is decided only once the page's Buy now / Add to
#: cart is live. Flipkart marks its root the moment it STARTS waking the page
#: (~2.0 s), before the coupon button (~2.4 s) or Buy now (~2.9 s) can be
#: pressed (measured 24 Nov 2026), so that mark alone came too early. A Buy now
#: that never shows as live holds the decision this long at most.
COUPON_BUY_WAIT_MS = 8_000

_PRESS_APPLIED = "applied"
_PRESS_REFUSED = "refused"  # Flipkart answered yes-the-request-worked, the tile still reads "Apply"
_PRESS_NO_ANSWER = "no_answer"  # sent, no answer in time: never pressed again over it
_PRESS_NO_EFFECT = "no_effect"  # nothing reached Flipkart, or it answered with an error

_SCAN_PRODUCT_COUPONS_JS = """() => {
    document.querySelectorAll('[data-bob-coupon]').forEach(el => el.removeAttribute('data-bob-coupon'));
    // Any React key: React has started on this page (the root gets
    // __reactContainer the moment waking STARTS). Fiber/props keys: React has
    // taken this node over, so a press on it is handled.
    const reactKey = (el) => Object.keys(el).some(k => k.startsWith('__react'));
    const fiberKey = (el) => Object.keys(el).some(k => k.startsWith('__reactFiber') || k.startsWith('__reactProps'));
    const live = (el) => {
        for (let p = el, i = 0; p && i < 4; p = p.parentElement, i++) {
            if (fiberKey(p)) return true;
        }
        return false;
    };
    let react = false;
    const all = document.body ? document.body.getElementsByTagName('*') : [];
    for (let i = 0, seen = 0; i < all.length && seen < 2000; i++) {
        const tag = all[i].tagName;
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'LINK' || tag === 'NOSCRIPT') continue;
        seen++;
        if (reactKey(all[i])) { react = true; break; }
    }
    const words = new Set(['apply', 'remove', 'applied']);
    const buttons = [];
    let idx = 0;
    for (const el of document.querySelectorAll('div, button, span, a')) {
        if (el.childElementCount > 0) continue;
        // textContent first: it is cheap. innerText (what is shown) only for the few that match.
        if (!words.has((el.textContent || '').trim().toLowerCase())) continue;
        const state = (el.innerText || '').trim().toLowerCase();
        if (!words.has(state)) continue;
        const r = el.getBoundingClientRect();
        if (!(r.width > 0 && r.height > 0)) continue;
        let p = el;
        let isCoupon = false;
        let isOther = false;
        let snippet = '';
        for (let i = 0; i < 6 && p.parentElement; i++) {
            p = p.parentElement;
            const pt = (p.innerText || '').replace(/\\s+/g, ' ').trim();
            if (pt.length > 450) break;
            if (/exchange offer|bank offer|no cost emi|credit card|debit card|\\bemi\\b/i.test(pt)) {
                isOther = true;
                break;
            }
            if (/\\bcoupons?\\b/i.test(pt)) {
                isCoupon = true;
                snippet = pt;
                break;
            }
        }
        if (isCoupon && !isOther) {
            const tag = String(idx++);
            el.setAttribute('data-bob-coupon', tag);
            // The tile's countdown ("Ends in 01d 22m 05s") ticks every second:
            // keep it out of the coupon's name, which must stay the same.
            const steady = snippet.replace(/ends in\\s*(?:\\d+\\s*[dhms]\\s*)+/ig, ' ')
                .replace(/^\\s*coupons?\\s*/i, '').replace(/\\s+/g, ' ').trim();
            const amount = (steady.match(/(?:₹\\s?[\\d,]+|\\d+(?:\\.\\d+)?\\s?%)\\s*(?:off|cashback)/i) || [''])[0];
            buttons.push({tag, state, snippet: steady.slice(0, 140), amount, live: live(el)});
        }
    }
    // Buy now / Add to cart: whether the page's main part can be pressed yet
    // (null when the page has neither, e.g. sold out).
    let buy = null;
    for (const el of document.querySelectorAll('div, button, span, a')) {
        if (el.childElementCount > 0) continue;
        const t = (el.textContent || '').trim().toLowerCase();
        if (t !== 'buy now' && t !== 'add to cart') continue;
        const r = el.getBoundingClientRect();
        if (!(r.width > 0 && r.height > 0)) continue;
        buy = live(el);
        if (buy) break;
    }
    return {ready: document.readyState, react, buy, buttons};
}"""


@dataclass
class CouponResult:
    """What the product-page coupon step did. Each entry names the coupon by
    its amount ("₹500 off", "10% off") or, failing that, by its tile's text."""

    applied: list[str] = field(default_factory=list)  # turned on by this step
    already: list[str] = field(default_factory=list)  # found already on
    failed: list[str] = field(default_factory=list)  # seen, but not on when the step stopped

    @property
    def label(self) -> str | None:
        """The run line's word on the coupon, or None when the page had none."""
        on = self.applied + [name for name in self.already if name not in self.applied]
        parts = []
        if on:
            parts.append(f"coupon {', '.join(on)} applied")
        if self.failed:
            parts.append(f"{'' if on else 'coupon '}{', '.join(self.failed)} NOT applied")
        return "; ".join(parts) or None


def _coupon_name(button: dict) -> str:
    amount = " ".join(str(button.get("amount") or "").split())
    return amount or str(button.get("snippet") or "coupon")[:40]


def _coupon_key(button: dict) -> str:
    """Which tile this is, steady across looks: its place among the coupon
    buttons plus its name, so two tiles with the same amount stay apart."""
    return f"{button.get('tag')}|{_coupon_name(button)}"


def _unique(names) -> list[str]:
    out: list[str] = []
    for name in names:
        if name not in out:
            out.append(name)
    return out


async def _scan_product_coupons(page: Page) -> dict | None:
    try:
        scan = await page.evaluate(_SCAN_PRODUCT_COUPONS_JS)
    except Exception:  # noqa: BLE001 — best effort, never stops an order or breaks stub pages
        return None
    if not isinstance(scan, dict) or not isinstance(scan.get("buttons"), list):
        return None
    scan["buttons"] = [b for b in scan["buttons"] if isinstance(b, dict) and "tag" in b]
    return scan


def _coupons_on(scan: dict) -> int:
    return sum(1 for b in scan["buttons"] if b.get("state") != "apply")


def _tile_on(scan: dict, key: str, on_before: int) -> bool:
    """Whether the pressed tile reads "Remove"/"Applied" now. A tile redrawn
    out of recognition is judged by the count of coupons that are on."""
    for button in scan["buttons"]:
        if _coupon_key(button) == key:
            return button.get("state") != "apply"
    return _coupons_on(scan) > on_before


def _settle_coupons(
    result: CouponResult, buttons: list[dict], seen: dict[str, str], applied_keys: list[str]
) -> CouponResult:
    """What is on and what is not, from the page's last look. A tile seen
    earlier but missing now counts as not on (unless this step turned it on),
    so a coupon that existed always shows in the run line."""
    state = {_coupon_key(b): b.get("state") for b in buttons}
    # One this step turned on that reads "Apply" again was replaced by another
    # coupon: it is no longer on, and it was not refused either.
    result.applied = _unique(seen[k] for k in applied_keys if state.get(k) != "apply")
    on_keys = {k for k, s in state.items() if s != "apply"}
    result.already = _unique(
        seen[k] for k in seen if k in on_keys and k not in applied_keys and seen[k] not in result.applied
    )
    result.failed = _unique(
        seen[k]
        for k in seen
        if k not in on_keys and k not in applied_keys
        and seen[k] not in result.applied and seen[k] not in result.already
    )
    return result


async def _press_coupon(page: Page, button: dict, on_before: int, hard_stop: float) -> str:
    """Press one coupon's Apply and wait for that tile to turn to "Remove".
    Never presses a button that no longer reads "Apply", and never runs past
    ``hard_stop`` (event-loop time). Returns one of the ``_PRESS_*`` outcomes."""
    loop = asyncio.get_running_loop()
    key = _coupon_key(button)
    sent: list[str] = []
    answers: list[tuple[float, int]] = []  # (when, HTTP status; 0 = the request failed)

    def _coupon_call(request) -> bool:  # noqa: ANN001 — Playwright Request
        try:
            return request.method == "POST" and urlsplit(request.url).path.endswith("/action/view")
        except Exception:  # noqa: BLE001
            return False

    def _on_request(request) -> None:  # noqa: ANN001
        if _coupon_call(request):
            sent.append(request.url)

    def _on_response(response) -> None:  # noqa: ANN001
        with contextlib.suppress(Exception):
            if _coupon_call(response.request):
                answers.append((loop.time(), int(response.status)))

    def _on_failed(request) -> None:  # noqa: ANN001
        if _coupon_call(request):
            answers.append((loop.time(), 0))

    def _left_ms(cap: int) -> int:
        return max(1, min(cap, int((hard_stop - loop.time()) * 1000)))

    listening: list[tuple[str, Callable]] = []
    for event, handler in (("request", _on_request), ("response", _on_response), ("requestfailed", _on_failed)):
        with contextlib.suppress(Exception):
            page.on(event, handler)
            listening.append((event, handler))
    try:
        loc = page.locator(f'[data-bob-coupon="{button["tag"]}"]').first
        try:
            with contextlib.suppress(Exception):
                await loc.scroll_into_view_if_needed(timeout=_left_ms(QUICK_TIMEOUT_MS))
            # Last look right before the press: a tile that has turned to
            # "Remove" meanwhile must not be pressed, that would take it off.
            if (await loc.inner_text(timeout=_left_ms(1_000))).strip().lower() != "apply":
                return _PRESS_NO_EFFECT
            await loc.click(timeout=_left_ms(QUICK_TIMEOUT_MS))
        except Exception as exc:  # noqa: BLE001 — best effort, never stops an order
            log.warning("could not press product coupon %s (%s)", _coupon_name(button), exc.__class__.__name__)
            return _PRESS_NO_EFFECT
        until = min(loop.time() + COUPON_CONFIRM_TIMEOUT_MS / 1000, hard_stop)
        extended = False
        while True:
            await page.wait_for_timeout(COUPON_POLL_MS)
            scan = await _scan_product_coupons(page)
            if scan is None:
                return _PRESS_NO_ANSWER if sent else _PRESS_NO_EFFECT
            if _tile_on(scan, key, on_before):
                return _PRESS_APPLIED
            now = loop.time()
            if answers:
                when, status = answers[-1]
                if not 200 <= status < 300:
                    # Busy, an error, or a dropped connection: not a no.
                    log.info("coupon %s: Flipkart answered %s — pressing again", _coupon_name(button), status or "nothing (request failed)")
                    with contextlib.suppress(Exception):
                        await page.wait_for_timeout(min(COUPON_ERROR_PAUSE_MS, _left_ms(COUPON_ERROR_PAUSE_MS)))
                    return _PRESS_NO_EFFECT
                if now - when >= COUPON_AFTER_ANSWER_MS / 1000:
                    return _PRESS_REFUSED
            if now >= until:
                if sent and not answers and not extended and until < hard_stop:
                    # The press reached Flipkart; its answer is just slow.
                    extended = True
                    until = min(until + COUPON_SLOW_ANSWER_MS / 1000, hard_stop)
                    continue
                if answers:
                    return _PRESS_REFUSED
                # Never press again over a request that may still land.
                return _PRESS_NO_ANSWER if sent else _PRESS_NO_EFFECT
    finally:
        for event, handler in listening:
            with contextlib.suppress(Exception):
                page.remove_listener(event, handler)


async def apply_product_coupons(
    page: Page,
    *,
    ready_timeout_ms: int = COUPON_READY_TIMEOUT_MS,
    slow_page_timeout_ms: int = COUPON_SLOW_PAGE_TIMEOUT_MS,
) -> CouponResult:
    """Turn on every Flipkart product-page coupon ("Coupons · ₹500 off · Apply")
    before Buy now / Add to cart, so the discount is in the order total.

    Waits for the coupon button to be live, presses it with a real pointer
    click, and counts it only when that tile reads "Remove" afterwards. A press
    that reached nobody, or that Flipkart answered with an error, is tried
    again (up to ``COUPON_CLICK_ATTEMPTS``); a "no", or a request still out, is
    not. Never presses "Remove", and never touches Exchange, Bank or EMI Apply
    buttons. Best effort: it never stops an order; a coupon left off shows in
    the run line as "NOT applied", and the price gate decides as before.
    """
    result = CouponResult()
    loop = asyncio.get_running_loop()
    start = loop.time()
    plain_since: float | None = None
    react_since: float | None = None
    awake_at: float | None = None
    empty_since: float | None = None
    presses: dict[str, int] = {}
    given_up: set[str] = set()
    applied_keys: list[str] = []
    seen: dict[str, str] = {}  # every coupon tile seen in this step: key -> name
    buttons: list[dict] = []
    while True:
        scan = await _scan_product_coupons(page)
        if scan is None:
            return _settle_coupons(result, buttons, seen, applied_keys) if seen else result
        now = loop.time()
        buttons = scan["buttons"]
        for button in buttons:
            seen.setdefault(_coupon_key(button), _coupon_name(button))
        if scan.get("react") and react_since is None:
            react_since = now
        if scan.get("buy") is True:
            page_live = True
        elif scan.get("react"):
            # No Buy now on the page, or one not live yet: the page counts as
            # live once React has had COUPON_BUY_WAIT_MS to take it over.
            page_live = scan.get("buy") is None or now - react_since >= COUPON_BUY_WAIT_MS / 1000
        elif scan.get("ready") == "complete":
            plain_since = now if plain_since is None else plain_since
            page_live = now - plain_since >= COUPON_PLAIN_PAGE_MS / 1000
        else:
            page_live = False
        # A page whose Buy now is still asleep is just slow: give it longer,
        # and once it wakes, at least COUPON_AFTER_WAKE_MS more.
        if scan.get("buy") is True and awake_at is None:
            awake_at = now
        if scan.get("buy") is False:
            deadline = start + slow_page_timeout_ms / 1000
        elif awake_at is not None:
            deadline = max(
                start + ready_timeout_ms / 1000,
                min(awake_at, start + slow_page_timeout_ms / 1000) + COUPON_AFTER_WAKE_MS / 1000,
            )
        else:
            deadline = start + ready_timeout_ms / 1000
        waiting = [b for b in buttons if b.get("state") == "apply"]

        if not buttons:
            if seen:
                pass  # a tile seen before is being redrawn: keep looking until the limit
            elif page_live:
                empty_since = now if empty_since is None else empty_since
                if now - empty_since >= COUPON_LATE_TILE_MS / 1000:
                    return result
        elif not waiting or any(_coupon_key(b) in applied_keys for b in waiting):
            # All on — or one this step turned on reads "Apply" again because
            # another coupon replaced it: keep what is on, never flip between them.
            settled = _settle_coupons(result, buttons, seen, applied_keys)
            log.info("product coupon(s): %s", settled.label)
            return settled
        else:
            empty_since = None
            candidates = [b for b in waiting if _coupon_key(b) not in given_up]
            if not candidates:
                settled = _settle_coupons(result, buttons, seen, applied_keys)
                log.warning("product coupon(s): %s", settled.label)
                return settled
            press = next(
                (b for b in candidates if b.get("live") or (page_live and not scan.get("react"))),
                None,
            )
            if press is not None and now < deadline:
                key, name = _coupon_key(press), _coupon_name(press)
                presses[key] = presses.get(key, 0) + 1
                hard_stop = deadline + (COUPON_CONFIRM_TIMEOUT_MS + COUPON_SLOW_ANSWER_MS) / 1000
                outcome = await _press_coupon(page, press, _coupons_on(scan), hard_stop)
                if outcome == _PRESS_APPLIED:
                    applied_keys.append(key)
                    log.info("applied product coupon %s (press %d)", name, presses[key])
                elif outcome == _PRESS_REFUSED:
                    given_up.add(key)
                    log.warning("Flipkart took the press but product coupon %s still reads 'Apply' — not for this account?", name)
                elif outcome == _PRESS_NO_ANSWER:
                    given_up.add(key)
                    log.warning("no answer from Flipkart for product coupon %s — not pressing again over it", name)
                elif presses[key] >= COUPON_CLICK_ATTEMPTS:
                    given_up.add(key)
                    log.warning("product coupon %s: %d presses, none went through", name, presses[key])
                continue

        if loop.time() >= deadline:
            settled = _settle_coupons(result, buttons, seen, applied_keys)
            if settled.failed:
                pressed = sum(presses.values())
                log.warning(
                    "product coupon(s) %s still not on after %.0fs — %s",
                    ", ".join(settled.failed),
                    loop.time() - start,
                    f"pressed {pressed} time(s), none went through" if pressed else "the page never let them be pressed",
                )
            return settled
        await page.wait_for_timeout(COUPON_POLL_MS)


async def buy_now(page: Page, product_link: str, *, already_open: bool = False) -> None:
    if not already_open:
        await page.goto(product_link, wait_until="domcontentloaded")
    # An account with no saved address lands on the first-address form instead
    # of the checkout: that is arriving too, not a press that did nothing.
    if not await click_until_navigated(
        page,
        S.BUY_NOW_BTN,
        "/viewcheckout",
        also=(FIRST_ADDRESS_PATH,),
        dismiss_xpath=S.BUY_NOW_SKIP_CONTINUE,
    ):
        log.error("buy_now: Buy now never took effect (page did not reach checkout)")


async def fill_address(page: Page, addr: AddressInfo) -> None:
    try:
        actions = await _xpath(page, S.ADDRESS_ACTION_BTN)
        await actions.nth(1).click(timeout=STEP_TIMEOUT_MS)  # legacy clicked index [1]
        await (await _xpath(page, S.ADD_NEW_ADDRESS)).click(timeout=STEP_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        log.error("address: open form failed: %s", exc)
    try:
        await (await _xpath(page, S.ADDR_NAME)).fill(addr.name)
        if addr.phone:
            await (await _xpath(page, S.ADDR_PHONE)).fill(addr.phone)
        await (await _xpath(page, S.ADDR_PIN)).fill(addr.pin)
        await (await _xpath(page, S.ADDR_LINE2)).fill(addr.area)
        await (await _xpath(page, S.ADDR_LINE1)).fill(addr.area)
        await (await _xpath(page, S.DELIVER_HERE)).click()
    except Exception as exc:  # noqa: BLE001
        log.error("address: fill failed: %s", exc)


#: Quantity modes: "upto" takes what Flipkart allows, "minimum" all or nothing.
QTY_MODE_UPTO = "upto"
QTY_MODE_MINIMUM = "minimum"
QTY_MODES = (QTY_MODE_MINIMUM, QTY_MODE_UPTO)


@dataclass
class QuantityOutcome:
    """What the checkout accepted. ``note`` is Flipkart's own reason when it
    is less than what was asked (a per-order cap), kept for the record and
    shown to the operator."""

    applied: int
    note: str | None = None


#: How long the box is given to show the new number after APPLY, and how
#: often it is read meanwhile. The refusal message lives for under a second,
#: so the body is read on the same ticks.
QTY_READBACK_WAIT_MS = 4_000
QTY_READBACK_INTERVAL_MS = 400
QTY_MENU_WAIT_MS = 800

_QTY_BOX_RE = re.compile(r"Qty:\s*(\d+)")
#: "You can only purchase 1 units of vivo T5 Lite … in a single order. If you
#: have a higher requirement, please create a new order." — seen live.
_QTY_LIMIT_RE = re.compile(r"You can only purchase\s+\d+\s+units?[^.]*\.", re.IGNORECASE)


async def _read_qty_box(page: Page) -> int | None:
    try:
        text = await page.locator(f"xpath={S.QTY_BOX}").first.inner_text(timeout=QUICK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — not there, or not yet
        return None
    match = _QTY_BOX_RE.search(text or "")
    return int(match.group(1)) if match else None


async def _read_qty_limit(page: Page) -> str | None:
    try:
        body = await page.locator(f"xpath={S.PAGE_BODY}").inner_text(timeout=QUICK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        return None
    match = _QTY_LIMIT_RE.search(body or "")
    return " ".join(match.group(0).split()) if match else None


async def set_quantity(page: Page, qty: int, *, index: int = 0) -> QuantityOutcome:
    """Ask the checkout for ``qty`` units and report what it accepted.

    Mapped live on 24 Oct 2026: the "Qty: 1" box is a dropdown whose "more"
    entry opens an "Enter Quantity" dialog (an input and APPLY); the short
    list is used only when the dialog is not offered. Flipkart caps some
    products — phones among them — at one unit per order and says so in a
    message gone within a second, so nothing is assumed: the box is read back
    after the change and the cap's wording is kept as the note when the number
    falls short. No control, or no read-back, means one unit — recording a
    quantity that was never applied would misstate what was bought.
    """
    if not qty or qty <= 1:
        return QuantityOutcome(1)
    # ``index``: which line of a multi-product checkout (0 for the first).
    boxes = page.locator(f"xpath={S.QTY_BOX}")
    box = boxes.first if index == 0 else boxes.nth(index)
    try:
        await box.wait_for(timeout=QUICK_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "could not set quantity to %s (no quantity control on the checkout); "
            "ordering 1 unit instead: %s",
            qty,
            exc,
        )
        return QuantityOutcome(1, "the checkout offered no quantity control")

    try:
        await box.click(timeout=QUICK_TIMEOUT_MS)
        await page.wait_for_timeout(QTY_MENU_WAIT_MS)
        more = page.locator(f"xpath={S.QTY_MORE}").last
        if await more.count():
            await more.click(timeout=QUICK_TIMEOUT_MS)
            field = page.locator(f"xpath={S.QTY_DIALOG_INPUT}").first
            await field.wait_for(timeout=QUICK_TIMEOUT_MS)
            await field.fill(str(qty))
            await page.locator(f"xpath={S.QTY_APPLY}").last.click(timeout=QUICK_TIMEOUT_MS)
        else:
            await page.locator(f"xpath={S.QTY_OPTION.format(value=qty)}").last.click(
                timeout=QUICK_TIMEOUT_MS
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not set quantity to %s; ordering 1 unit instead: %s", qty, exc)
        return QuantityOutcome(1, f"the quantity control did not respond ({exc.__class__.__name__})")

    note: str | None = None
    applied: int | None = None
    waited = 0
    while waited < QTY_READBACK_WAIT_MS:
        await page.wait_for_timeout(QTY_READBACK_INTERVAL_MS)
        waited += QTY_READBACK_INTERVAL_MS
        if note is None:
            note = await _read_qty_limit(page)
        applied = await _read_qty_box(page)
        if applied == qty:
            break

    if applied is None:
        # APPLY was pressed and the box cannot be read: the checkout may now
        # carry the asked-for units while the record would say one. Not a
        # guess to make with money (audit F-10, 25 Oct 2026): zero units means
        # "refuse this order" to the caller.
        log.error("quantity read-back failed after asking for %s; refusing the order", qty)
        return QuantityOutcome(0, note or f"the checkout did not confirm the quantity after {qty} were asked for")
    if applied != qty:
        log.warning("Flipkart kept %s unit(s) instead of %s: %s", applied, qty, note or "no reason shown")
        return QuantityOutcome(applied, note or f"Flipkart kept the quantity at {applied}")
    return QuantityOutcome(applied)


def quantity_refusal(qty_mode: str, asked: int, outcome: QuantityOutcome) -> str | None:
    """Why the quantity mode refuses this order, or None to go ahead.

    "upto" takes whatever the checkout accepted. "minimum" refuses an order
    that carries fewer units than asked (Flipkart's per-order cap), so a run
    never places part-orders it did not want.
    """
    if qty_mode != QTY_MODE_MINIMUM or outcome.applied >= max(1, asked or 1):
        return None
    because = f" ({outcome.note})" if outcome.note else ""
    return (
        f"Flipkart allowed only {outcome.applied} of the {asked} unit(s) per order{because} — "
        "minimum mode, not ordering, nothing was charged"
    )


def gst_refusal(gst_mandatory: bool, wanted: str | None, shown: str | None) -> str | None:
    """Why "GST mandatory" refuses this order, or None to go ahead.

    Without the switch an order may go ahead when the checkout names no GSTIN
    (the read-back checks it later). With it, the order needs a GST chosen and
    the checkout showing exactly that one; a different one is refused anyway,
    switch or not, before this is asked.
    """
    if not gst_mandatory:
        return None
    if not wanted:
        return "GST is mandatory for this run, but no GST was chosen for this order — not ordering, nothing was charged"
    if not shown:
        return (
            f"GST is mandatory for this run, but the checkout does not show GST {wanted} — "
            "not ordering, nothing was charged"
        )
    return None


async def set_qty(page: Page, qty: int) -> int:
    """The quantity actually applied — see :func:`set_quantity`."""
    return (await set_quantity(page, qty)).applied


#: How long the GST panel gets to open after "Use GST Invoice" is ticked, and
#: the checkout to name the GSTIN after "Confirm and Save".
GST_PANEL_WAIT_MS = 6_000


async def _until(predicate, wait_ms: int) -> bool:
    """Whether ``predicate()`` (a coroutine function) came true within ``wait_ms``."""
    deadline = time.monotonic() + wait_ms / 1000
    while True:
        if await predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.25)


async def fill_gst(page: Page, gst: GstInfo) -> None:
    """Bill the order under ``gst`` on the checkout (audit F-4).

    The checkout as mapped live on 27 Oct 2026 and 23 Nov 2026 (``selectors`` GST_*):
    - When no GST is selected yet, Flipkart shows a "Use GST Invoice" tick box.
    - When another GST is already pre-selected on the account, Flipkart replaces
      that tick box with a "GST Invoice" block carrying a "Change" button, which
      opens "Select GST Details" (with saved GSTINs, "Add new GST Details", and
      "Confirm and Use").
    A checkout already naming ``gst.gstin`` is left alone. Nothing here raises:
    the caller reads the GSTIN the checkout then names and refuses an order
    billed under another one (:func:`gstin_on_checkout`).
    """
    wanted = (gst.gstin or "").strip().upper()
    if not wanted:
        return
    try:
        if await gstin_on_checkout(page) == wanted:
            return

        # Open the GST drawer: either "Use GST Invoice" (no GST yet) or the
        # "Change" button beside "GST Invoice" (a different GST is selected).
        use_box = page.get_by_text(S.GST_USE_INVOICE_TEXT, exact=True)
        change_btn = page.locator(f"xpath={S.GST_CHANGE_BTN}")
        if await use_box.count() and await use_box.first.is_visible():
            await use_box.first.click(timeout=QUICK_TIMEOUT_MS)
        elif await change_btn.count():
            with contextlib.suppress(Exception):
                await change_btn.first.scroll_into_view_if_needed(timeout=QUICK_TIMEOUT_MS)
            await change_btn.first.click(timeout=QUICK_TIMEOUT_MS)
        else:
            await use_box.first.click(timeout=QUICK_TIMEOUT_MS)

        gstin_box = page.locator(S.GST_GSTIN_INPUT)
        listed = page.get_by_text(wanted, exact=False)
        add_new = page.get_by_text(S.GST_ADD_NEW_TEXT, exact=True)

        async def box_open() -> bool:
            return bool(await gstin_box.count()) and await gstin_box.first.is_visible()

        async def add_new_visible() -> bool:
            return bool(await add_new.count()) and await add_new.first.is_visible()

        async def panel_open() -> bool:
            return await box_open() or bool(await listed.count()) or await add_new_visible()

        if not await _until(panel_open, GST_PANEL_WAIT_MS):
            log.error("fill_gst: opening the GST drawer opened no GST panel")
            return

        if await listed.count() and not await box_open():
            await listed.first.click(timeout=QUICK_TIMEOUT_MS)
        else:
            if not await box_open() and await add_new_visible():
                await add_new.first.click(timeout=QUICK_TIMEOUT_MS)
                if not await _until(box_open, GST_PANEL_WAIT_MS):
                    log.error("fill_gst: clicking %r did not open the GST inputs", S.GST_ADD_NEW_TEXT)
                    return
            if await box_open():
                await gstin_box.first.fill(wanted, timeout=QUICK_TIMEOUT_MS)
                await page.locator(S.GST_BUSINESS_INPUT).first.fill(
                    (gst.business_name or "")[:60], timeout=QUICK_TIMEOUT_MS
                )
                await page.wait_for_timeout(500)

        for _attempt in range(2):
            for label in (S.GST_CONFIRM_TEXT, S.GST_CONFIRM_USE_TEXT):
                btn = page.get_by_text(label, exact=True)
                if await btn.count() and await btn.first.is_visible():
                    await btn.first.click(timeout=QUICK_TIMEOUT_MS)
                    await page.wait_for_timeout(600)
                    if await gstin_on_checkout(page) == wanted and not await box_open():
                        break
            if await gstin_on_checkout(page) == wanted and not await box_open():
                break

        async def named() -> bool:
            return await gstin_on_checkout(page) == wanted and not await box_open()

        if not await _until(named, GST_PANEL_WAIT_MS):
            # If saving a new GST returned to the "Select GST Details" drawer,
            # confirm using it once more.
            use_btn = page.get_by_text(S.GST_CONFIRM_USE_TEXT, exact=True)
            if await use_btn.count() and await use_btn.first.is_visible():
                if await listed.count():
                    with contextlib.suppress(Exception):
                        await listed.first.click(timeout=QUICK_TIMEOUT_MS)
                await use_btn.first.click(timeout=QUICK_TIMEOUT_MS)
                await _until(named, GST_PANEL_WAIT_MS)

        if not await named():
            log.warning("fill_gst: the checkout does not name %s after confirming GST", wanted)
    except Exception as exc:  # noqa: BLE001
        log.error("fill_gst failed: %s", exc)


#: A GSTIN as the checkout prints it beside "GST Invoice": state code, PAN,
#: entity digit, Z, check character — not glued to another code on either side.
_GSTIN_IN_TEXT = re.compile(r"(?<![0-9A-Z])\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z](?![0-9A-Z])")


async def gstin_on_checkout(page: Page) -> str | None:
    """The GSTIN the checkout page shows the order billed under, or None when
    the page names none (or cannot be read)."""
    try:
        text = await page.locator(f"xpath={S.PAGE_BODY}").first.inner_text(timeout=QUICK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — an unreadable page is "none shown"
        return None
    found = _GSTIN_IN_TEXT.findall(text or "")
    return found[0].upper() if found else None


async def _start_payment(page: Page) -> None:
    for xp in (S.PAY_CONTINUE_1, S.PAY_CONTINUE_2):
        try:
            await (await _xpath(page, xp)).click(timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            log.error("payment step %s failed: %s", xp, exc)


#: How long the card form gets to run its own checks after the last box,
#: looked at every half second: a check of the card with its bank can answer
#: a moment after the box is left.
CARD_FORM_SETTLE_MS = 2_000
#: The pause between typed keys, as a person types: some of Flipkart's boxes
#: format as they go (spaces in the number, " / " in the expiry).
CARD_KEY_DELAY_MS = 25
#: The card form itself: the nearest block holding both the number box and
#: the CVV box. Its complaints are read there, not off the whole page, which
#: also carries offers whose small print ("not valid on corporate cards")
#: would stop a good run.
CARD_FORM_BLOCK = f"{S.CARD_NUMBER}/ancestor::*[.{S.CARD_CVV}][1]"
#: What Flipkart's card form prints under a box it does not accept. The first
#: two are its own words (seen live, 26 Oct 2026); the rest are the usual
#: variants. Error wording only — never a box's label, or every run would stop.
_CARD_FORM_ERROR = re.compile(
    r"card number is (?:required|invalid|not valid)"
    r"|(?:please\s+)?enter (?:a\s+)?valid (?:month|year|expiry|card|cvv)[^.\n]{0,40}"
    r"|invalid (?:card number|expiry|cvv)"
    r"|cvv is (?:required|invalid)"
    r"|card (?:has |is )?expired"
    r"|cards? (?:is |are )?not (?:supported|accepted)[^.\n]{0,40}",
    re.IGNORECASE,
)


def _expiry_for_form(month: str, year: str) -> str:
    """The expiry as Flipkart's one "MM / YY" box takes it: "12/28".

    A four-digit year keeps its last two digits. The box holds two, so
    "12/2028" typed into it reads as December 2020 — an expired card. The card
    sheet keeps a year as written, and until 26 Oct 2026 a sheet's "2028" went
    into the box as it was. A two-digit year is kept as it is.
    """
    mm = month.zfill(2) if month else ""
    yy = year[-2:] if len(year) == 4 else year
    return f"{mm}/{yy}" if mm and yy else ""


async def _type_into(page: Page, xpath: str, value: str) -> None:
    """Clear a box and type the value into it key by key, as a person would.

    Typed rather than set in one go: a box that formats as it goes, or that
    checks the card's bank as the digits arrive, listens to keys.
    """
    box = page.locator(f"xpath={xpath}").first
    with contextlib.suppress(Exception):  # a click helps some boxes take focus; typing focuses anyway
        await box.click(timeout=QUICK_TIMEOUT_MS)
    await box.fill("", timeout=QUICK_TIMEOUT_MS)
    await box.press_sequentially(value, delay=CARD_KEY_DELAY_MS)


async def _card_form_errors(page: Page) -> str | None:
    """The first complaint Flipkart's card form shows, or None.

    Read inside the card form (``CARD_FORM_BLOCK``); the whole page only when
    that block cannot be read. Its digits are taken out: the complaint goes
    into the log and the run's message, and the words after it are read up to
    the end of the line, which could run on into the number shown with spaces
    ("4111 1111 …") — a shape the message's own filter for long digit runs
    misses. No complaint needs a digit to be understood.
    """
    text = None
    for xpath in (CARD_FORM_BLOCK, S.PAGE_BODY):
        try:
            block = page.locator(f"xpath={xpath}").first
            if await block.count():
                text = await block.inner_text(timeout=QUICK_TIMEOUT_MS)
                break
        except Exception:  # noqa: BLE001 — try the next, wider block
            continue
    if text is None:
        return None
    found = _CARD_FORM_ERROR.search(text)
    if not found:
        return None
    return " ".join(re.sub(r"\d", "", found.group(0)).split()) or "a complaint"


async def _card_form_complaint(page: Page, wait_ms: int = CARD_FORM_SETTLE_MS) -> str | None:
    """A complaint the card form shows within ``wait_ms``, or None after it."""
    waited = 0
    while True:
        complaint = await _card_form_errors(page)
        if complaint or waited >= wait_ms:
            return complaint
        await page.wait_for_timeout(500)
        waited += 500


async def _type_and_check(page: Page, xpath: str, label: str, value: str) -> tuple[str | None, bool]:
    """Type a value into one box and read it back: ``(why_not, mismatch)``.
    ``why_not`` is None when the box holds the digits typed (shown with
    spaces or " / " or not); ``mismatch`` says the box took the keys but holds
    something else."""
    try:
        await _type_into(page, xpath, value)
    except Exception as exc:  # noqa: BLE001 — the message would echo the value
        log.error("card entry: could not fill the %s field (%s)", label, exc.__class__.__name__)
        return f"could not fill the {label} field on Flipkart's card form ({exc.__class__.__name__})", False
    try:
        shown = await page.locator(f"xpath={xpath}").first.input_value()
    except Exception as exc:  # noqa: BLE001
        return f"could not read the {label} back from Flipkart's card form ({exc.__class__.__name__})", False
    if re.sub(r"\D", "", shown or "") != re.sub(r"\D", "", value):
        log.error("card entry: the %s box does not hold what was typed", label)
        return f"the {label} box on Flipkart's card form does not hold what was typed", True
    return None, False


async def _enter_card(page: Page, card: CardInfo) -> str | None:
    """Enter card details. Does NOT charge and presses nothing that could:
    Pay is pressed in one place only, ``_submit_card``, after the order total
    has passed the price gate and the order has been recorded.

    Returns None when every box holds what was typed and Flipkart's form
    raises no complaint about it, else the reason — and the caller then
    refuses to go on to Pay: a Pay pressed over a form that does not hold the
    card charges whatever the payment page had preselected, or nothing, and
    neither is the order (audit F-1, 25 Oct 2026). The first live card runs
    (26 Oct 2026) pressed Pay and the browser never left Flipkart's payment
    page, and what the form had made of the details was not recorded. Hence
    the two-digit year, the typing, the read-back of every box and the form's
    own complaints, all before Pay.

    Nothing typed is ever logged. Playwright's error text repeats the value a
    fill was given, so a failure names the field and the error's type, never
    the error itself (audit F-2).
    """
    # Selecting the card method shows the new-card form straight away (seen
    # live, 26 Oct 2026). An account with saved cards may show those instead,
    # with a link to add a new one: that link is clicked only when the number
    # box is hidden, and found by its wording. The old flow clicked a span by
    # an obfuscated class every time, a class that could sit on any text.
    try:
        number_shown = await page.locator(f"xpath={S.CARD_NUMBER}").first.is_visible()
    except Exception:  # noqa: BLE001 — unreadable is "not shown"
        number_shown = False
    if not number_shown:
        with contextlib.suppress(Exception):  # absent: typing below fails closed
            await page.locator(f"xpath={S.PAY_ADD_NEW_CARD}").first.click(timeout=QUICK_TIMEOUT_MS)

    fields = (
        (S.CARD_NUMBER, "card number", card.number),
        (S.CARD_EXPIRY, "expiry", _expiry_for_form(card.month, card.year)),
        (S.CARD_CVV, "CVV", card.cvv),
    )
    # Every box is read back as soon as it is typed: it must hold the digits
    # typed before anything is pressed. A box that dropped or cut a key —
    # "12/2028" into a box with room for "12 / 20" — holds another card.
    for xpath, label, value in fields:
        if not value:
            return f"the {label} is missing from the card sheet"
        problem, mismatch = await _type_and_check(page, xpath, label, value)
        if mismatch and xpath == S.CARD_EXPIRY and "/" in value:
            # A box that adds its own " / " and keeps a typed "/" as well
            # holds "12 / /2". It formats the digits alone itself.
            problem, mismatch = await _type_and_check(page, xpath, label, value.replace("/", ""))
        if problem:
            return problem

    # Leave the last box so the form runs its own checks, then read them for
    # a moment: a "Card number is required" or "Enter valid month/year" on the
    # form means Pay would do nothing, or something other than this order.
    with contextlib.suppress(Exception):
        await page.keyboard.press("Tab")
    complaint = await _card_form_complaint(page)
    if complaint:
        log.error("card entry: Flipkart's card form says %r after the details were typed", complaint)
        return f"Flipkart's card form says '{complaint}' after the details were typed"

    # Nothing is pressed here. The old flow clicked a "card submit" button
    # found by a class name from an older payment page; Pay is the only submit
    # now, and a stale class matching it would have paid before the price gate
    # and before the order was recorded.
    return None


async def select_cod(page: Page) -> bool:
    """Choose Cash on Delivery. Returns False when COD isn't offered.

    No card is entered in this mode. The price-target gate still runs
    afterwards, which matters because COD can carry a collection fee that
    pushes the payable amount above the card price.
    """
    try:
        await (await _xpath(page, S.PAY_COD_OPTION)).click(timeout=STEP_TIMEOUT_MS)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("select_cod failed (COD may not be offered here): %s", exc)
        return False


#: How many times to re-read the payable total while waiting for it to stop
#: moving, and the gap between reads. Choosing Cash on Delivery RE-PRICES the
#: order, and the summary lands a beat after the CTA does.
#:
#: Bounded by read COUNT rather than by wall-clock on purpose: a slow page whose
#: own reads eat the clock would otherwise be declared "never settled" after one
#: or two looks, failing orders that were merely slow.
#:
#: The figure must hold for STABLE_READS consecutive reads (≈4.8s at this
#: interval). Two agreeing reads were enough only while each read happened to
#: wait out a 4s timeout; once that wait was removed, a COD handling fee that
#: landed 1.5s after the method was chosen slipped past the gate unseen.
AMOUNT_SETTLE_READS = 12
AMOUNT_SETTLE_STABLE_READS = 5
AMOUNT_SETTLE_INTERVAL_MS = 1_200


async def amount_candidates(page: Page) -> dict[str, float]:
    """Every figure on the confirm screen that could be the charged amount."""
    found: dict[str, float] = {}
    for name, xpath in (("total", S.ORDER_TOTAL), ("pay button", S.PAY_CTA_AMOUNT)):
        locator = page.locator(f"xpath={xpath}")
        try:
            # Count first: it answers at once, while reading an element that is
            # not there waits out the whole timeout. A COD page has no "Pay ₹"
            # button at all, and waiting for it cost 4s on every settle read.
            if not await locator.count():
                continue
            text = await locator.first.inner_text(timeout=QUICK_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — a COD button carries no amount
            continue
        amount = parse_amount(text)
        if amount > 0:
            found[name] = amount
    return found


async def read_final_amount(page: Page) -> float | None:
    """Read the final payable total on the confirm screen (the gate value).

    Returns ``None`` when the total cannot be read — deliberately NOT 0.0.
    Returning zero made an unreadable page look free, which the gate then
    waved through; ``None`` makes the caller refuse to confirm instead.

    Two further precautions, both from live failures on 2026-10-24:

    * It waits for the figure to STOP CHANGING. A read taken while the page was
      still applying Cash on Delivery returned ₹17,628 for an order that
      charges ₹17,707 — the ₹79 handling fee landed a moment later. A gate that
      reads low is the one failure mode that costs the operator money.
    * It caps on the HIGHEST figure present, not the first one found. The
      summary total and the amount on the pay button are separate elements;
      gating on the larger can only ever refuse too much, never charge too much.
    """
    settled: dict[str, float] | None = None
    previous: dict[str, float] = {}
    streak = 0  # consecutive reads agreeing with the one before

    for attempt in range(AMOUNT_SETTLE_READS):
        current = await amount_candidates(page)
        if current and current == previous:
            streak += 1
        else:
            streak = 1 if current else 0
            if attempt:
                log.info(
                    "the order total is still moving (%s) — re-reading",
                    _describe_amounts(current) or "nothing readable yet",
                )
        if streak >= AMOUNT_SETTLE_STABLE_READS:
            settled = current
            break
        previous = current
        await page.wait_for_timeout(AMOUNT_SETTLE_INTERVAL_MS)

    if settled is None:
        log.error(
            "read_final_amount: the total never settled after %d reads (last: %s)",
            AMOUNT_SETTLE_READS,
            _describe_amounts(previous) or "nothing readable",
        )
        return None

    gate_value = max(settled.values())
    if len(set(settled.values())) > 1:
        log.warning(
            "confirm screen disagrees on the amount (%s) — gating on the highest, ₹%.2f",
            _describe_amounts(settled),
            gate_value,
        )
    return gate_value


def _describe_amounts(amounts: dict[str, float]) -> str:
    return ", ".join(f"{name}=₹{value:,.2f}" for name, value in sorted(amounts.items()))


#: Flipkart order ids look like OD followed by a long run of digits.
_ORDER_ID_RE = re.compile(r"\bOD\d{10,}\b")


async def _row_text(row, xpath: str) -> str | None:
    """Text of one field inside an order row, or None if it isn't there."""
    try:
        return (await row.locator(f"xpath={xpath}").first.inner_text()).strip()
    except Exception:  # noqa: BLE001
        return None


async def _field(page: Page, xpath: str) -> str | None:
    """Text of one field, or None when it is not on the page."""
    try:
        return (await page.locator(f"xpath={xpath}").first.inner_text()).strip()
    except Exception:  # noqa: BLE001
        return None


# --- Delivering to the chosen address (mapped live 2026-10-24) ----------------
#: How long the address picker / map search / form get to render.
ADDRESS_STEP_TIMEOUT_MS = 10_000
#: How long the saved-address list may take to finish loading (56 addresses
#: took about 14 s live on 26 Oct 2026; "Add New" appears with the last rows).
ADDRESS_LIST_TIMEOUT_MS = 40_000
#: How long after Save address the checkout gets to show the new address.
ADDRESS_SAVE_TIMEOUT_MS = 20_000
#: How many map suggestions are tried before giving up on a query.
ADDRESS_SUGGESTIONS_TRIED = 3

_PINCODE_RE = re.compile(r"\b(\d{6})\b")
#: Names that are businesses get Flipkart's "Work" label; people get "Home".
_BUSINESS_NAME_RE = re.compile(
    r"\b(pvt|private|limited|ltd|llp|enterprises?|traders?|industries|company|"
    r"co|inc|corporation|stores?|solutions|technologies|services)\b\.?",
    re.IGNORECASE,
)


def normalize_address_text(text: str | None) -> str:
    """Lower-case letters and digits only: "Tower 5, 1005" == "Tower5-1005"."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def address_matches(text: str | None, address: AddressInfo) -> bool:
    """Does a saved-address row or the checkout's "Deliver to" block describe
    ``address``?

    The NAME must be there — the same flat under a different name is a
    different delivery (and a different invoice). With it, the HOUSE LINE must
    be there too whenever the Address Book has one: the same name and the same
    pincode are not the same address. Live on 25–26 Oct 2026 the account held
    "AARTHBODHA PRIVATE LIMITED, Tower 5, 1005, Madiyanva, … Allu Nagar
    Diguria, Lucknow 226013" — a wrongly pinned twin of the real "Tower 5,
    1005, Garden Bay" with the right name and the right pincode — and a
    pincode-only match would have shipped to it. Only an Address Book entry
    with no separate house line (the legacy one-line form) still settles on
    the pincode or its one line.
    """
    hay = normalize_address_text(text)
    if not hay:
        return False
    name = normalize_address_text(address.name)
    if not name or name not in hay:
        return False
    # A pincode shown on the row must be the address's own: the same name and
    # flat in another city is another delivery. A row showing none is judged
    # on its words alone (the picker's rows do not always print it).
    pin = (address.pin or "").strip()
    shown = set(_PINCODE_RE.findall(text or ""))
    if pin and shown and pin not in shown:
        return False
    line1 = normalize_address_text(address.line1)
    if line1:
        return _contains_whole(hay, line1)
    if pin and pin in shown:
        return True
    area = normalize_address_text(address.area)
    return bool(area) and area in hay


def _contains_whole(hay: str, needle: str) -> bool:
    """``needle`` occurs in ``hay`` without its digits running into the
    neighbours': "tower51005" is not in "tower510050" or "5tower51005", though
    it is in "aarthbodhatower51005gardenbay". Letters have no boundary to keep
    — the normalised text has no separators left."""
    start = hay.find(needle)
    while start != -1:
        end = start + len(needle)
        before_ok = not needle[0].isdigit() or start == 0 or not hay[start - 1].isdigit()
        after_ok = not needle[-1].isdigit() or end == len(hay) or not hay[end].isdigit()
        if before_ok and after_ok:
            return True
        start = hay.find(needle, start + 1)
    return False


def address_overlap(text: str | None, address: AddressInfo) -> int:
    """How much of the Address Book entry's wording a saved-address row
    repeats: the count of its house-line, locality, city and state words (three
    characters or more) found in the row. Decides between several rows that
    all match — the one naming the most of the entry is the entry."""
    hay = normalize_address_text(text)
    wanted = {
        token
        for token in re.findall(r"[a-z0-9]+", f"{address.line1} {address.area} {address.city} {address.state}".lower())
        if len(token) >= 3
    }
    return sum(1 for token in wanted if token in hay)


def address_search_queries(address: AddressInfo) -> list[str]:
    """What to type into the map search, most specific first.

    A house line rarely geocodes on its own, so each query is wider than the
    last; the pin check afterwards is what keeps a wide match honest.
    """
    queries: list[str] = []
    if address.line1 and address.city:
        queries.append(f"{address.line1}, {address.city} {address.pin}".strip())
    if address.city:
        queries.append(f"{address.city} {address.pin}".strip())
    if address.pin:
        queries.append(address.pin)
    if not queries and address.area:
        queries.append(address.area)
    return list(dict.fromkeys(q for q in queries if q))


#: Visible, clickable texts below the map search box: Flipkart's suggestions.
#: They carry no identifying class, so they are found by position. MAP_SUGGESTIONS
_MAP_SUGGESTIONS_JS = """() => {  /* MAP_SUGGESTIONS */
  const box = document.querySelector('input[placeholder="Search by area, name, street."]');
  if (!box) return [];
  const r = box.getBoundingClientRect();
  const txt = e => (e.innerText || '').replace(/\\s+/g, ' ').trim();
  const out = [];
  for (const e of document.querySelectorAll('body *')) {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(e.tagName) || e.children.length) continue;
    const b = e.getBoundingClientRect();
    if (!b.width || !b.height || b.top < r.bottom || b.top > r.bottom + 420 || b.left > r.right) continue;
    if (getComputedStyle(e).cursor !== 'pointer') continue;
    const t = txt(e);
    if (t.length < 3 || /current location/i.test(t)) continue;
    out.push(t);
  }
  return [...new Set(out)];
}"""

#: Press one suggestion, found by its text AT CLICK TIME and through the row
#: that owns it: the list re-renders as the search settles, so an element
#: located a moment earlier may already be gone. CLICK_SUGGESTION
_CLICK_SUGGESTION_JS = """(wanted) => {  /* CLICK_SUGGESTION */
  const txt = e => (e.innerText || '').replace(/\\s+/g, ' ').trim();
  const box = document.querySelector('input[placeholder="Search by area, name, street."]');
  const top = box ? box.getBoundingClientRect().bottom : 0;
  for (const e of document.querySelectorAll('body *')) {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(e.tagName)) continue;
    const b = e.getBoundingClientRect();
    if (!b.width || !b.height || b.top < top) continue;
    if (txt(e) !== wanted) continue;
    let row = e;
    for (let i = 0; i < 4 && row; i++) {
      if (getComputedStyle(row).cursor === 'pointer') break;
      row = row.parentElement;
    }
    (row || e).click();
    return true;
  }
  return false;
}"""


def rank_suggestions(suggestions: list[str], address: AddressInfo) -> list[str]:
    """Most address-like suggestion first: the one sharing the most words with
    the house line and city. A locality named in the Address Book ("Garden
    Bay") then beats a wider match that merely shares the pincode."""
    wanted = {
        token
        for token in re.findall(r"[a-z0-9]+", f"{address.line1} {address.city}".lower())
        if len(token) > 2
    }
    unique = list(dict.fromkeys(suggestions))

    def score(item: str) -> tuple[int, int]:
        words = set(re.findall(r"[a-z0-9]+", item.lower()))
        return (len(wanted & words), -unique.index(item))

    return sorted(unique, key=score, reverse=True)


#: The "Deliver To" block on the pin page: the locality Flipkart resolved the
#: pin to, ending in its pincode. PIN_BLOCK
_PIN_BLOCK_JS = """() => {  /* PIN_BLOCK */
  const txt = e => (e.innerText || '').replace(/\\s+/g, ' ').trim();
  const head = [...document.querySelectorAll('body *')].find(e => !e.children.length && txt(e) === 'Deliver To');
  if (!head) return '';
  const top = head.getBoundingClientRect().top;
  const parts = [];
  for (const e of document.querySelectorAll('body *')) {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(e.tagName) || e.children.length) continue;
    const b = e.getBoundingClientRect();
    if (!b.width || !b.height || b.top < top || b.top > top + 260 || b.left < head.getBoundingClientRect().left - 60) continue;
    const t = txt(e);
    if (t) parts.push(t);
  }
  return parts.join(' ');
}"""


async def _click_text(page: Page, xpath: str, *, timeout_ms: int = ADDRESS_STEP_TIMEOUT_MS) -> bool:
    """Wait for an RN-Web text control and activate it through the DOM."""
    locator = page.locator(f"xpath={xpath}").first
    try:
        await locator.wait_for(state="visible", timeout=timeout_ms)
        await locator.evaluate("el => el.click()")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("could not press %s: %s", xpath[-60:], exc)
        return False


class AddressPickerClosed(RuntimeError):
    """The saved-address picker never opened, so nothing can be said about the
    saved addresses — not even that the address is absent. Its message is the
    flow's own reason, in the operator's words."""


async def select_saved_address(page: Page, address: AddressInfo) -> bool:
    """Pick ``address`` from the account's saved addresses. False if absent.

    Leaves the picker open on False, so the caller can add the address. Raises
    :class:`AddressPickerClosed` when the picker could not be opened at all —
    a closed picker is not an absent address, and the add-address page is not
    reachable from it either.
    """
    if address_matches(await current_delivery_address(page), address):
        log.info("delivery address already set to %s", address.name)
        return True

    # The picker's first rows show within a couple of seconds; its heading and
    # its "Add New" control only once the whole list has loaded (about 14 s
    # for 56 addresses, seen live on 26 Oct 2026). So "open" means more rows
    # than the closed checkout showed, OR the heading, OR "Add New" — the row
    # selector also matches the checkout's own leaves, so a count alone says
    # nothing, and pressing Change again on an open picker would close it.
    baseline = await _row_count(page)
    opened = False
    for attempt in range(3):
        await click_rnw(page, S.CHECKOUT_CHANGE_ADDRESS, timeout_ms=QUICK_TIMEOUT_MS, force_js=attempt > 0)
        if await _picker_is_open(page, wait_ms=6_000, baseline=baseline):
            opened = True
            break
        log.info("the address picker did not open on press %d", attempt + 1)
    if not opened:
        log.error("could not open the address picker")
        raise AddressPickerClosed("could not open Flipkart's saved-address picker: the Change control did not respond")

    # Poll the list as it fills in rather than read it once, and do not call
    # the address absent until the list is complete: "Add New" renders with
    # the last rows, and the count has stopped growing.
    step_mark("address: picker opened")
    waited_ms = 0
    last_count = -1
    stable_ticks = 0
    first_look = True
    while waited_ms < ADDRESS_LIST_TIMEOUT_MS:
        # Look at once, then every half second: the rows arrive over a few
        # seconds, and a fixed 2 s sleep before every look cost most of this step.
        if not first_look:
            await page.wait_for_timeout(ADDRESS_POLL_MS)
            waited_ms += ADDRESS_POLL_MS
        first_look = False
        rows = page.locator(f"xpath={S.ADDRESS_PICKER_ROW}")
        texts = await _all_texts(rows)
        count = len(texts)
        matches: list[tuple[int, str]] = []
        seen_rows: set[str] = set()
        for index, text in enumerate(texts):
            # Two pieces of one entry read as the same row: count it once.
            if text and text not in seen_rows and address_matches(text, address):
                seen_rows.add(text)
                matches.append((index, text))
        if matches:
            if len(matches) > 1:
                log.warning(
                    "%d saved addresses match %s; taking the one that repeats most of the Address Book entry",
                    len(matches),
                    address.name,
                )
            # Among several matching rows, the one naming the most of the entry
            # (its locality, city, state) is the entry; ties go to the first.
            index, text = max(matches, key=lambda match: (address_overlap(match[1], address), -match[0]))
            log.info("matched saved address: %s", " ".join(text.split())[:90])
            try:
                await rows.nth(index).evaluate("el => el.click()")
            except Exception as exc:  # noqa: BLE001
                log.warning("could not click the address row: %s", exc)
            else:
                step_mark("address: row found")
                if await _switched_to(page, address):
                    step_mark("address: selected")
                    return True
                log.warning("clicked the matching address but the checkout did not switch to it")
        stable_ticks = stable_ticks + 1 if count == last_count else 0
        last_count = count
        try:
            complete = await page.locator(f"xpath={S.ADDRESS_ADD_NEW}").count() > 0
        except Exception:  # noqa: BLE001
            complete = False
        if complete and stable_ticks >= ADDRESS_STABLE_TICKS:
            break
    log.info("%s is not among the account's %d saved addresses", address.name, max(last_count, 0))
    return False


#: How often the open picker is looked at, and how many unchanged looks in a
#: row (with "Add New" showing) mean the list is complete: 4 x 0.5 s = 2 s,
#: the same settle time the old 2 s ticks gave.
ADDRESS_POLL_MS = 500
ADDRESS_STABLE_TICKS = 4
#: After a row is clicked: how long the checkout gets to show it, looked at often.
ADDRESS_SWITCH_WAIT_MS = 4_000
ADDRESS_SWITCH_POLL_MS = 300


#: For each matched element, the text of its whole entry in the address list.
#: Seen live on 15 Nov 2026: the row selector now matches small pieces of each
#: row (the NAME line "Harsh Gupta", and others beside it), not the row; the
#: street and city sit a few levels up, and one level further is the whole
#: list. Matching on the name alone never confirmed the address, so every
#: order added it to the account again (16 and 20 saved addresses).
#: Each list entry holds exactly two matches (the name line and the first
#: address line); the list holds two per entry. So a row is the largest box
#: around the match that holds at most ROW_PIECES matches: the whole entry,
#: never two entries. (The matches also hit pieces of the checkout outside the
#: list, so "the box holding every match" is the whole page, not the list.)
ROW_PIECES = 2
_ROW_TEXT_JS = """(els, pieces) => {
    const leaves = new Set(els);
    const count = box => { let n = 0; for (const l of leaves) if (box.contains(l)) n++; return n; };
    return els.map(e => {
        let box = e;
        while (box.parentElement && count(box.parentElement) <= pieces) box = box.parentElement;
        return (box.innerText || '').trim();
    });
}"""


async def _all_texts(rows) -> list[str]:
    """Every saved-address row's whole text, in one round trip."""
    try:
        texts = await rows.evaluate_all(_ROW_TEXT_JS, ROW_PIECES)
        return [str(t or "") for t in texts]
    except Exception:  # noqa: BLE001 — not available: read row by row, as before
        pass
    try:
        count = await rows.count()
    except Exception:  # noqa: BLE001
        return []
    texts: list[str] = []
    for index in range(count):
        try:
            texts.append((await rows.nth(index).inner_text(timeout=1_000)).strip())
        except Exception:  # noqa: BLE001
            texts.append("")
    return texts


async def _switched_to(page: Page, address: AddressInfo) -> bool:
    """Whether the checkout now ships to ``address``, checked every 0.3 s."""
    waited = 0
    while waited <= ADDRESS_SWITCH_WAIT_MS:
        # The checkout already shows "Deliver to:" here, so this read is immediate.
        if address_matches(await current_delivery_address(page), address):
            return True
        await page.wait_for_timeout(ADDRESS_SWITCH_POLL_MS)
        waited += ADDRESS_SWITCH_POLL_MS
    return False


async def _row_count(page: Page) -> int:
    """How many elements the picker-row selector matches right now — on the
    closed checkout, its own leaves; with the picker open, those plus a row
    per saved address."""
    try:
        return await page.locator(f"xpath={S.ADDRESS_PICKER_ROW}").count()
    except Exception:  # noqa: BLE001
        return 0


async def _picker_is_open(page: Page, *, wait_ms: int, baseline: int = 0) -> bool:
    """Whether the saved-address picker is on screen: its heading, its "Add
    New" control, or more rows than the closed checkout showed before Change
    was pressed (``baseline``). A press that did nothing — or one that opened
    the picker and shut it again — leaves the count where it was, and is then
    retried by the caller."""
    deadline = wait_ms
    while deadline > 0:
        try:
            if await page.locator(f"xpath={S.ADDRESS_PICKER_HEADING}").count() > 0:
                return True
            if await page.locator(f"xpath={S.ADDRESS_ADD_NEW}").count() > 0:
                return True
        except Exception:  # noqa: BLE001
            pass
        if await _row_count(page) > baseline:
            return True
        await page.wait_for_timeout(500)
        deadline -= 500
    return False


async def add_delivery_address(page: Page, address: AddressInfo) -> tuple[bool, str]:
    """Add ``address`` to the account from the open picker, then deliver to it.

    Returns ``(ok, why_not)``. Fails CLOSED at every step before Save: a pin
    whose pincode is not the address's is never saved, since an address with
    the wrong pin ships to the wrong place while reading correctly.
    """
    # "Add New" renders only once the whole saved-address list has loaded —
    # the list timeout, not the step timeout, is the right wait here.
    if not await _click_text(page, S.ADDRESS_ADD_NEW, timeout_ms=ADDRESS_LIST_TIMEOUT_MS):
        return False, "could not open Flipkart's add-address page"
    if not await _click_text(page, S.ADDRESS_AWAY_FROM_LOCATION):
        return False, "could not reach the map search ('Away from my location' not offered)"

    box = page.locator(f"xpath={S.ADDRESS_MAP_SEARCH}").first
    try:
        await box.wait_for(state="visible", timeout=ADDRESS_STEP_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        return False, "Flipkart's map search did not appear"

    pinned: str | None = None
    for query in address_search_queries(address):
        try:
            await box.click(timeout=ADDRESS_STEP_TIMEOUT_MS)
            await box.fill("")
            await page.keyboard.type(query, delay=40)
        except Exception as exc:  # noqa: BLE001 — the search box went away (the pin view replaced it)
            log.warning("could not type %r into the map search: %s", query, exc)
            continue
        await page.wait_for_timeout(3_500)
        try:
            suggestions = await page.evaluate(_MAP_SUGGESTIONS_JS)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the map suggestions: %s", exc)
            suggestions = []
        if not suggestions:
            log.info("map search %r offered no locations", query)
            continue
        for suggestion in rank_suggestions(suggestions, address)[:ADDRESS_SUGGESTIONS_TRIED]:
            try:
                pressed = await page.evaluate(_CLICK_SUGGESTION_JS, suggestion)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not press map suggestion %r: %s", suggestion, exc)
                pressed = False
            if not pressed:
                log.info("map suggestion %r had gone by the time it was pressed", suggestion)
                continue
            await page.wait_for_timeout(4_000)
            try:
                block = await page.evaluate(_PIN_BLOCK_JS)
            except Exception:  # noqa: BLE001
                block = ""
            found = _PINCODE_RE.search(block or "")
            if found and found.group(1) == (address.pin or "").strip():
                log.info("map pin resolved to %s", " ".join((block or "").split())[:100])
                pinned = suggestion
                break
            log.info(
                "map location %r resolved to pincode %s, not %s — not using it",
                suggestion,
                found.group(1) if found else "unknown",
                address.pin,
            )
        if pinned:
            break
    if not pinned:
        return False, (
            f"no map location for this address resolved to pincode {address.pin}; "
            "not saved (the address needs a locality Flipkart's map can find)"
        )

    if not await _click_text(page, S.ADDRESS_ADD_DETAILS):
        return False, "could not open the address details form"

    texts = page.locator(f"xpath={S.ADDRESS_FORM_TEXT_INPUTS}")
    tels = page.locator(f"xpath={S.ADDRESS_FORM_TEL_INPUTS}")
    for _ in range(5):
        try:
            if await texts.count() >= 2:
                break
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(1_000)
    else:
        return False, "the address form did not show its fields"

    house = ", ".join(p for p in (address.line1, address.floor) if p) or address.area
    try:
        await texts.nth(0).fill(house)
        await texts.nth(1).fill(address.name)
        phone = re.sub(r"\D", "", address.phone or "")[-10:]
        if len(phone) == 10:
            await tels.nth(0).fill(phone)
    except Exception as exc:  # noqa: BLE001
        return False, f"could not fill the address form ({exc})"

    kind = S.ADDRESS_TYPE_WORK if _BUSINESS_NAME_RE.search(address.name or "") else S.ADDRESS_TYPE_HOME
    await _click_text(page, kind, timeout_ms=QUICK_TIMEOUT_MS)

    # Flipkart's revamped /address-map Save address button is an RN-Web
    # responder control that ignores synthetic JS el.click() (verified live
    # 23 Nov 2026: real Playwright mouse click triggers address-validate -> 200
    # and returns to /viewcheckout). Fall back to _click_text for stubs.
    saved_click = False
    try:
        save_btn = page.locator(f"xpath={S.ADDRESS_SAVE}").first
        await save_btn.wait_for(state="visible", timeout=ADDRESS_STEP_TIMEOUT_MS)
        await save_btn.scroll_into_view_if_needed()
        await save_btn.click(timeout=ADDRESS_STEP_TIMEOUT_MS)
        saved_click = True
    except Exception:  # noqa: BLE001
        saved_click = await _click_text(page, S.ADDRESS_SAVE)
    if not saved_click:
        return False, "could not press Save address"
    log.info("saved a new address on the account: %s, %s, %s", address.name, house, address.pin)

    try:
        await page.wait_for_url("**/viewcheckout**", timeout=ADDRESS_SAVE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        hint = ""
        try:
            errs = await page.evaluate(
                """() => [...document.querySelectorAll('*')]
                     .filter(e => e.children.length === 0 && e.innerText && e.innerText.trim().length > 2
                             && getComputedStyle(e).color.startsWith('rgb(227'))
                     .map(e => e.innerText.trim())"""
            )
            if errs:
                hint = f": {errs[0][:120]}"
        except Exception:  # noqa: BLE001
            pass
        return False, f"the checkout did not come back after saving{hint} (browser is on {(page.url or '')[:80]})"
    for _ in range(5):
        if address_matches(await current_delivery_address(page), address):
            return True, ""
        await page.wait_for_timeout(2_000)
    return False, "the address was saved but the checkout is not delivering to it"


#: Where Buy now lands an account with no saved address at all (seen live on
#: 15 Nov 2026): a plain form, not the checkout with its "Change" link.
FIRST_ADDRESS_PATH = "/changeShippingAddress/add/form"
FIRST_ADDRESS_SAVE_TIMEOUT_MS = 20_000


def split_street(line: str) -> tuple[str, str]:
    """The Address Book's one street line as the form's two boxes:
    (house / building, road / area). "251, Ashok Vihar Colony, Ashok Vihar,
    80 Feet Road" -> ("251, Ashok Vihar Colony", "Ashok Vihar, 80 Feet Road").
    A line with nothing after its first two parts repeats its last part, as
    both boxes are required."""
    parts = [part.strip() for part in (line or "").split(",") if part.strip()]
    if not parts:
        return "", ""
    head = parts[:2] if len(parts) > 2 else parts[:1]
    tail = parts[len(head):] or parts[-1:]
    return ", ".join(head), ", ".join(tail)


#: How long to wait, after Buy now, for the page to show which of the two it
#: is. Live on 15 Nov 2026 Buy now first reached /viewcheckout and Flipkart
#: then redirected a no-address account to the form a moment later; deciding
#: on the first URL sent it down the picker path, which cannot work there.
FIRST_ADDRESS_DECIDE_MS = 6_000
FIRST_ADDRESS_POLL_MS = 250


async def on_first_address_form(page: Page, *, wait_ms: int = FIRST_ADDRESS_DECIDE_MS) -> bool:
    """Whether Flipkart is asking this account for its first address: True on
    the form, False once the checkout has drawn its "Deliver to:" line."""
    waited = 0
    while True:
        if FIRST_ADDRESS_PATH in (page.url or ""):
            return True
        try:
            if await page.locator(f"xpath={S.DELIVER_TO_LINE}").count() > 0:
                return False
        except Exception:  # noqa: BLE001 — a page mid-navigation: look again
            pass
        if waited >= wait_ms:
            return False
        await page.wait_for_timeout(FIRST_ADDRESS_POLL_MS)
        waited += FIRST_ADDRESS_POLL_MS


async def fill_first_address_fields(page: Page, address: AddressInfo) -> None:
    """Fill the first-address form from the Address Book entry (no submit)."""
    house, area = split_street(address.line1 or address.area)
    if address.floor:
        house = f"{address.floor}, {house}" if house else address.floor

    async def put(name: str, value: str) -> None:
        if not value:
            return
        box = page.locator(f"[name={name}]").first
        if await box.count() == 0:
            return
        # Read-only boxes (State, seen live on 15 Nov 2026) are Flipkart's to
        # fill from the pincode: typing there only waits out a timeout.
        if await box.evaluate("e => e.readOnly || e.disabled"):
            return
        if (await box.evaluate("e => e.tagName")) == "SELECT":
            try:
                await box.select_option(label=value, timeout=QUICK_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 — a state spelt differently: leave it to Flipkart's pincode lookup
                log.warning("first address: %s %r is not among the choices", name, value)
            return
        try:
            await box.fill(value, timeout=QUICK_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 — one box never stops the whole form
            log.warning("first address: could not fill %s: %s", name, exc.__class__.__name__)

    await put("name", address.name)
    phone = re.sub(r"\D", "", address.phone or "")[-10:]
    if len(phone) == 10:
        await put("phone", phone)
    await put("pincode", address.pin)
    # The pincode fills city and state on its own a moment later; ours go in
    # only where the box is still empty, so Flipkart's spelling wins.
    for _ in range(20):
        try:
            state_box = page.locator("[name=state]").first
            if await state_box.count() == 0 or await state_box.input_value():
                break
        except Exception:  # noqa: BLE001
            break
        await page.wait_for_timeout(250)
    for name, value in (("city", address.city), ("state", address.state)):
        box = page.locator(f"[name={name}]").first
        try:
            empty = await box.count() > 0 and not (await box.input_value())
        except Exception:  # noqa: BLE001
            empty = False
        if empty:
            await put(name, value)
    await put("addressLine1", house)
    await put("addressLine2", area)
    try:
        await page.locator("input[name=locationTypeTag]").first.check(timeout=QUICK_TIMEOUT_MS, force=True)
    except Exception:  # noqa: BLE001 — the type is optional on Flipkart's side
        pass


#: Boxes the form requires, checked after filling: the page redraws itself once
#: it has loaded, and a fill made before that is wiped (seen live on 15 Nov
#: 2026: every box came back empty and Save said "provide the necessary details").
FIRST_ADDRESS_REQUIRED = ("name", "pincode", "addressLine1", "addressLine2")


async def _empty_required(page: Page) -> list[str]:
    empty: list[str] = []
    for name in FIRST_ADDRESS_REQUIRED:
        try:
            box = page.locator(f"[name={name}]").first
            if await box.count() and not (await box.input_value()).strip():
                empty.append(name)
        except Exception:  # noqa: BLE001
            empty.append(name)
    return empty


async def save_first_address(page: Page, address: AddressInfo) -> tuple[bool, str]:
    """Fill and save the first address, then make sure the checkout ships there."""
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:  # noqa: BLE001 — a busy page: the re-check below covers it
        pass
    for attempt in range(3):
        await fill_first_address_fields(page, address)
        await page.wait_for_timeout(700)
        missing = await _empty_required(page)
        if not missing:
            break
        log.info("first address: %s cleared by the page; filling again (%d)", ", ".join(missing), attempt + 1)
    else:
        return False, f"could not fill the first-address form ({', '.join(missing)} stayed empty)"
    try:
        await page.locator("input[type=submit], button[type=submit]").first.click(timeout=STEP_TIMEOUT_MS)
        await page.wait_for_url("**/viewcheckout**", timeout=FIRST_ADDRESS_SAVE_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        why = await _first_address_complaint(page)
        return False, f"could not save the first delivery address on this account ({why or exc.__class__.__name__})"
    log.info("saved the account's first address: %s, %s", address.name, address.pin)
    if address_matches(await current_delivery_address(page), address):
        return True, ""
    return False, "the first address was saved but the checkout is not delivering to it"


async def _first_address_complaint(page: Page) -> str | None:
    """Flipkart's own red text under the form, if any."""
    try:
        body = await page.inner_text("body", timeout=QUICK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        return None
    found = re.search(r"(please (?:enter|provide)[^.\n]{0,80}|invalid[^.\n]{0,60}|not serviceable[^.\n]{0,60})", body or "", re.I)
    return found.group(0).strip() if found else None


async def ensure_delivery_address(page: Page, address: AddressInfo) -> tuple[bool, str]:
    """Make the checkout deliver to ``address``: select it, or add it first."""
    if await on_first_address_form(page):
        # An account with no saved address: Buy now opens the form, not the
        # checkout, and there is no picker to open (it waited 2 minutes for one).
        step_mark("address: first-address form")
        return await save_first_address(page, address)
    try:
        if await select_saved_address(page, address):
            return True, ""
    except AddressPickerClosed as exc:
        return False, str(exc)
    return await add_delivery_address(page, address)


# --- Submitting the order, and proving it exists ------------------------------
#: How long the Cash on Delivery confirmation popup gets to appear.
COD_POPUP_TIMEOUT_MS = 10_000
#: How long to wait for Flipkart's confirmation page after submitting. A COD
#: order lands there in seconds; a card payment can pass through the bank
#: first. The wait only runs long when something has gone wrong — NOT waiting
#: at all is how the old flow threw away every Cash on Delivery order.
ORDER_CONFIRMATION_TIMEOUT_MS = 90_000
#: Once on the confirmation page: how many looks for the order id, and the gap.
ORDER_ID_READS = 10
ORDER_ID_INTERVAL_MS = 1_500
#: Where the last page of an unconfirmed order is saved as a screenshot.
DIAGNOSTICS_DIR = Path("diagnostics")

#: Wording on a confirmation page meaning the order did NOT simply go through.
#: Patterns, not fixed phrases: a fixed list missed "Your payment has failed"
#: (which does not contain "payment failed") and would have called it placed.
_NOT_CONFIRMED = re.compile(
    r"\b(?:payment|transaction|order)\s+(?:has\s+|was\s+|is\s+)?(?:been\s+)?"
    r"(?:failed|failure|unsuccessful|declined|pending|cancell?ed|not\s+placed)\b"
    r"|\bcould\s*n[o']?t\s+(?:be\s+)?placed\b",
    re.IGNORECASE,
)

#: Flipkart's one order-creating request: POST .../fkpay/api/v3/payments/pay.
#: Watching for it is what makes "was the order sent?" evidence rather than an
#: inference from which buttons were pressed.
_ORDER_CALL = re.compile(r"/fkpay/api/v\d+/payments/pay(?:\?|$)")


@dataclass
class Submission:
    """What happened at the one irreversible step.

    ``sent`` is the line that matters. Before it nothing can have been ordered,
    so stopping is a clean failure. After it only Flipkart's confirmation can
    say whether an order exists, so stopping is *unconfirmed* — never *failed*.
    """

    sent: bool
    note: str = ""


COD_POPUP_POLL_MS = 300
_COD_ADVANCE = re.compile(r"pay only (₹\s?[\d,]+) now|(₹\s?[\d,]+) as advance|advance payment of (₹\s?[\d,]+)", re.I)


async def _cod_advance_asked(page: Page) -> str | None:
    """The advance Flipkart demands before a COD order ("₹25"), or None."""
    try:
        body = await page.inner_text("body", timeout=1_000)
    except Exception:  # noqa: BLE001
        return None
    if "pay only" not in (body or "").lower() and "advance" not in (body or "").lower():
        return None
    found = _COD_ADVANCE.search(body or "")
    if not found:
        return None
    return next(group for group in found.groups() if group).replace(" ", "")


async def _submit_cod(page: Page) -> Submission:
    """Place Order, then the popup's Confirm order — each pressed exactly once.

    Only the second press sends anything (see ``S.COD_PLACE_ORDER_BTN``), so
    every way of stopping before it is a clean failure.
    """
    try:
        await page.locator(f"xpath={S.COD_PLACE_ORDER_BTN}").click(timeout=STEP_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        return Submission(sent=False, note=f"could not press Place Order ({exc})")

    confirm = page.locator(f"xpath={S.COD_CONFIRM_ORDER_BTN}")
    # Watch for either popup: the usual "Confirm order", or Flipkart's demand
    # for an online advance before a COD order ("Pay only ₹25 now", seen live
    # on 15 Nov 2026 for some accounts). A COD run never pays online, so the
    # advance popup ends the order at once, with its own reason, and its
    # Continue is never pressed.
    waited = 0
    matches = 0
    while waited <= COD_POPUP_TIMEOUT_MS:
        try:
            if await confirm.first.is_visible():
                matches = await confirm.count()
                break
        except Exception:  # noqa: BLE001
            pass
        advance = await _cod_advance_asked(page)
        if advance:
            log.error("Flipkart asks for an online advance (%s) before a COD order; not paying it", advance)
            return Submission(
                sent=False,
                note=(
                    f"Flipkart asks this account to pay {advance} online in advance for Cash on Delivery — "
                    "a COD run never pays online, so nothing was ordered"
                ),
            )
        await page.wait_for_timeout(COD_POPUP_POLL_MS)
        waited += COD_POPUP_POLL_MS
    if not matches:
        return Submission(sent=False, note="the Cash on Delivery confirmation popup never appeared")
    if matches != 1:
        # Guessing between candidates is not acceptable on the one control that
        # orders; refuse while nothing has been sent.
        return Submission(sent=False, note=f"expected one 'Confirm order' button, found {matches}")

    try:
        await confirm.click(timeout=STEP_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        # Playwright can raise AFTER delivering a click (the popup detaches as
        # the page moves on), so an error here does not prove nothing was sent.
        log.warning("pressing 'Confirm order' reported an error; treating it as sent: %s", exc)
        return Submission(sent=True, note=f"pressing 'Confirm order' reported an error ({exc})")
    log.info("Cash on Delivery order submitted ('Confirm order' pressed)")
    return Submission(sent=True)


async def _submit_card(page: Page) -> Submission:
    """Press "Pay ₹…" once. The card can be charged from this click on.

    The card form's complaints are read once more first. The order total
    was read and the order recorded since the card was typed, so a check of
    the card that answers late shows its complaint by now; it stops the run
    here, while nothing has been sent.
    """
    complaint = await _card_form_errors(page)
    if complaint:
        log.error("not pressing Pay: Flipkart's card form says %r", complaint)
        return Submission(sent=False, note=f"Flipkart's card form says '{complaint}'")
    pay = page.locator(f"xpath={S.CARD_PAY_BTN}")
    try:
        matches = await pay.count()
    except Exception as exc:  # noqa: BLE001
        return Submission(sent=False, note=f"could not look for the Pay button ({exc})")
    if matches == 0:
        return Submission(sent=False, note="there is no 'Pay ₹…' button on the payment page")
    if matches != 1:
        # Guessing between candidates is not acceptable on the one control
        # that charges a card; refuse while nothing has been sent (as the Cash
        # on Delivery step does with its Confirm button).
        return Submission(sent=False, note=f"expected one 'Pay ₹…' button, found {matches}")

    try:
        await pay.first.click(timeout=STEP_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        log.warning("pressing Pay reported an error; treating it as sent: %s", exc)
        return Submission(sent=True, note=f"pressing Pay reported an error ({exc})")
    log.info("card payment submitted ('Pay' pressed)")
    return Submission(sent=True)


def reference_from_url(url: str) -> str | None:
    """A confirmation page's ``reference_id``, when it is an order reference."""
    try:
        values = parse_qs(urlsplit(url or "").query).get("reference_id", [])
    except ValueError:
        return None
    for value in values:
        if _ORDER_ID_RE.fullmatch(value.strip()):
            return value.strip()
    return None


def full_order_id(
    reference: str | None, page_text: str, link_ids: list[str] | None = None
) -> str | None:
    """The order id a confirmation page stands for.

    The reference is the order id minus its last two digits — Flipkart's own
    tracking code rebuilds the id as ``reference + "00"``. Recording the bare
    reference would file the order under an id nothing else uses, so the next
    sync would import the real order as a second row. A full id the page LINKS
    to or shows, extending the reference, wins; failing that, the same rebuild
    Flipkart does.
    """
    ids = list(dict.fromkeys([*(link_ids or []), *_ORDER_ID_RE.findall(page_text or "")]))
    if reference:
        extended = [i for i in ids if i != reference and i.startswith(reference)]
        return extended[0] if extended else f"{reference}00"
    if len(ids) > 1:
        log.warning("the confirmation page lists several order ids %s; recording the first", ids)
    return ids[0] if ids else None


def _is_confirmation_page(url: str) -> bool:
    """Exact path match: a substring test would accept any address that merely
    mentions the route, such as a redirect carrying it as a parameter."""
    try:
        path = urlsplit(url or "").path.rstrip("/").lower()
    except ValueError:
        return False
    return path in {p.lower() for p in S.ORDER_CONFIRMATION_PATHS}


#: Wording that positively says an order was created. "Thanks for shopping
#: with us!" is the heading of Flipkart's real success page (seen live
#: 2026-10-24 on two orders); that page shows NO order id in its text.
_CONFIRMED = re.compile(
    r"\border\s+(?:has\s+been\s+|is\s+|was\s+)?(?:placed|confirmed|successful)"
    r"|\bthanks?\s+(?:you\s+)?for\s+(?:your\s+)?(?:order|shopping)",
    re.IGNORECASE,
)

#: Links on the confirmation page that carry an order id — "Track & manage
#: order" points at the order's own page. The strongest proof there is: the
#: page is literally linking to the order. ORDER_LINKS
_ORDER_LINKS_JS = """() => {  /* ORDER_LINKS */
  return [...document.querySelectorAll('a[href*="order_id="]')].map(a => a.href);
}"""


def order_ids_in_links(hrefs: list[str] | None) -> list[str]:
    """Order ids carried by link addresses, in page order, without repeats."""
    found: list[str] = []
    for href in hrefs or []:
        try:
            values = parse_qs(urlsplit(href).query).get("order_id", [])
        except ValueError:
            continue
        for value in values:
            value = value.strip()
            if _ORDER_ID_RE.fullmatch(value) and value not in found:
                found.append(value)
    return found


def positive_proof(reference: str | None, text: str, link_ids: list[str] | None = None) -> str | None:
    """Why the page proves THIS order exists, or None if it does not.

    Accepted, strongest first: a link to an order whose id extends the
    address bar's reference (or any order link when there is no reference);
    an order id in the text that extends the reference; or the page confirming
    the order in words while the reference (or, with none, some order id) is
    there. A page that does none of these proves nothing, however right its
    address looks.
    """
    links = link_ids or []
    if links and (not reference or any(i.startswith(reference) for i in links)):
        return "the page links to the order"
    ids = _ORDER_ID_RE.findall(text or "")
    if reference and any(i != reference and i.startswith(reference) for i in ids):
        return "the page shows the order id"
    if _CONFIRMED.search(text or ""):
        if reference:
            return "the page confirms the order"
        if ids or links:
            return "the page confirms the order and names an id"
    return None


async def await_order_confirmation(
    page: Page, *, timeout_ms: int = ORDER_CONFIRMATION_TIMEOUT_MS
) -> tuple[str | None, str]:
    """Wait for Flipkart's confirmation page. Returns ``(order_id, why_not)``.

    That page is the only proof of an order accepted. The old flow read the
    newest row of the account's order list instead, which is how a run that
    placed nothing reported the previous day's cancelled backpack as its order.
    """
    try:
        await page.wait_for_url(_is_confirmation_page, timeout=timeout_ms, wait_until="commit")
    except Exception:  # noqa: BLE001
        return None, (
            f"Flipkart's order confirmation page did not appear within "
            f"{timeout_ms // 1000}s (the browser was on {_shown_url(page.url or '')})"
        )

    reference = reference_from_url(page.url or "")
    seen: list[str] = []
    proof: str | None = None
    link_ids: list[str] = []
    for _ in range(ORDER_ID_READS):
        text = await _field(page, S.PAGE_BODY) or ""
        try:
            link_ids = order_ids_in_links(await page.evaluate(_ORDER_LINKS_JS))
        except Exception:  # noqa: BLE001 — no links is simply no links
            link_ids = []
        if text:
            seen.append(text)
            if _NOT_CONFIRMED.search(text):
                break
            proof = positive_proof(reference, text, link_ids)
            if proof:
                break
        await page.wait_for_timeout(ORDER_ID_INTERVAL_MS)

    # Every read counts: failure wording seen on any of them is not undone by a
    # later read that happens to lack it.
    refusal = next((m.group(0) for t in seen if (m := _NOT_CONFIRMED.search(t))), None)
    if refusal:
        return None, f"Flipkart's confirmation page says {refusal!r}"
    if not seen:
        return None, "Flipkart's confirmation page could not be read"

    # The address can move on while the page is being read (a redirect, a
    # bounce to login). The proof is only good for the page it was read on.
    landed = page.url or ""
    if not _is_confirmation_page(landed) or reference_from_url(landed) != reference:
        return None, f"the browser left the confirmation page while it was being read (now on {_shown_url(landed)})"

    text = seen[-1]
    if not proof:
        # "Placed" is a finding, never a default. Being on the right address
        # without the page naming the order is not enough.
        log.warning("confirmation page did not name the order; it reads: %s", " ".join(text.split())[:240])
        return None, "Flipkart's confirmation page did not name the order"

    order_id = full_order_id(reference, text, link_ids)
    if not order_id:
        return None, "Flipkart showed its confirmation page, but no order id could be read from it"
    # Logged so the first real orders show what success looks like on the page.
    log.info("confirmation page (%s) reads: %s", proof, " ".join(text.split())[:240])
    return order_id, ""


#: How long after Pay the browser gets to arrive at the bank's own page (3-D
#: Secure); Flipkart's confirmation page arriving first ends the wait early,
#: so the full wait is only spent when something has gone wrong.
BANK_PAGE_WAIT_MS = 45_000
#: How long the bank's page gets to show its password box once the browser is
#: there: the address changes before the form has drawn, and the first page
#: away from Flipkart can be a payment gateway, or the bank's own hidden
#: check, on the way to the page that asks.
BANK_FIELD_WAIT_MS = 30_000
_PASSWORD_BOX = "xpath=//input[@type='password']"


def _foreign_host(url: str) -> str | None:
    """The host of a page that is not Flipkart's, else None."""
    host = (urlsplit(url or "").hostname or "").lower()
    if host and host != "flipkart.com" and not host.endswith(".flipkart.com"):
        return host
    return None


def _shown_url(url: str) -> str:
    """An address as a note or a log may show it: Flipkart's as it is (cut to
    90 characters, as before); any other site's without its query or
    fragment, which on a bank's page can carry what a form sent — the card's
    password among it, when a form is sent the plain way (23 Nov 2026)."""
    url = url or ""
    if not _foreign_host(url):
        return url[:90]
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:160]


def _inner_frames(where: object) -> list:
    """The frames inside a page, its own main frame left out."""
    main = getattr(where, "main_frame", None)
    return [frame for frame in list(getattr(where, "frames", None) or []) if frame is not main]


def _foreign_places(
    page: Page, popups: list | None = None, ignore: frozenset[str] = frozenset()
) -> list[tuple[str, object]]:
    """Everywhere a bank's page can be after Pay, as ``(host, place)``: the
    page itself once it has left Flipkart, a frame inside it showing another
    site (3-D Secure often loads in one, with Flipkart's address still in the
    bar), and any window Pay opened, with its frames.

    ``ignore`` holds the sites already framed in the payment page before Pay
    (fraud checks, captchas, ads): those are never the bank's page, and taking
    one for it would look for a password where none will ever be asked."""
    places: list[tuple[str, object]] = []
    for window in [page, *(popups or [])]:
        try:
            if window is not page and window.is_closed():
                continue
        except Exception:  # noqa: BLE001 — a window that cannot say is looked at
            pass
        host = _foreign_host(getattr(window, "url", "") or "")
        if host and host not in ignore:
            places.append((host, window))
        for frame in _inner_frames(window):
            frame_host = _foreign_host(getattr(frame, "url", "") or "")
            if frame_host and frame_host not in ignore:
                places.append((frame_host, frame))
    return places


async def _decline_saving_the_card(page: Page) -> bool:
    """Answer Flipkart's "Secure your card as per RBI guidelines" with "Maybe
    later" when it stands between Pay and the bank (``selectors``
    CARD_SAVE_PROMPT). True when it was answered. Never raises: Pay has been
    pressed, and nothing after it may."""
    try:
        prompt = page.locator(f"xpath={S.CARD_SAVE_PROMPT}")
        if not await prompt.count() or not await prompt.first.is_visible():
            return False
        later = page.locator(f"xpath={S.CARD_SAVE_LATER}")
        if not await later.count():
            return False
        await later.first.click(timeout=QUICK_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001 — after Pay nothing may raise
        log.warning("could not answer Flipkart's save-the-card prompt: %s", type(exc).__name__)
        return False
    log.info("Flipkart asked to save the card on the account (RBI guidelines); answered 'Maybe later'")
    return True


async def _bank_page(
    page: Page, popups: list | None = None, ignore: frozenset[str] = frozenset()
) -> tuple[str | None, object | None]:
    """The bank's authentication page after Pay: ``(host, place)``, the first
    of ``_foreign_places``. ``(None, None)`` when Flipkart kept the browser or
    its confirmation page arrived straight away.

    Flipkart may first ask whether to save the card on the account; that
    dialog is answered "Maybe later" once (:func:`_decline_saving_the_card`),
    or the browser would stand in front of it and never reach the bank — what
    happened to the card run of 27 Oct 2026."""
    waited = 0
    declined = False
    while waited <= BANK_PAGE_WAIT_MS:
        if _is_confirmation_page(page.url or ""):
            return None, None
        places = _foreign_places(page, popups, ignore)
        if places:
            return places[0]
        if not declined:
            declined = await _decline_saving_the_card(page)
        await page.wait_for_timeout(500)
        waited += 500
    return None, None


#: A bank authentication page refusing the browser rather than showing its
#: form (seen live 27 Oct 2026: HDFC's 3-D Secure returned "Request Blocked").
_BANK_BLOCKED = re.compile(
    r"request blocked|blocked by (?:our|your) security|access to this endpoint is blocked", re.IGNORECASE
)
#: A bare web-server error page where the bank's page was. HDFC's 3-D Secure
#: ended on "404 Not Found / nginx" on 24 Nov 2026 (job 689), after its
#: password form had been sent the plain way, without the page's own Submit
#: step. Read only on a page of a few lines, so a bank page that merely
#: mentions a number is never taken for one.
_BANK_ERROR_PAGE = re.compile(r"^\s*([45]\d\d)\s*[-:]?\s*([A-Za-z][A-Za-z' -]{2,40}?)\s*(?:\n|$)")
#: The longest page text that can still be a bare error page.
_BANK_ERROR_PAGE_MAX_CHARS = 300


async def _bank_authentication_blocked(
    page: Page, popups: list | None = None, ignore: frozenset[str] = frozenset()
) -> str:
    """A note when the bank's authentication page shows a "blocked by security"
    page, or a bare server error page, instead of its form. The order may
    still have reached Flipkart, so this only explains where the bank step
    stuck — it never says the order failed. Never raises: it runs after Pay."""
    for host, place in _foreign_places(page, popups, ignore):
        try:
            text = await place.locator("xpath=//body").first.inner_text(timeout=QUICK_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — a page that cannot be read is passed over
            continue
        if text and _BANK_BLOCKED.search(text):
            return (
                f"the bank's authentication page ({host}) showed 'Request Blocked' — it refused this "
                "automated browser; the bank step may need a person to finish it in a visible window"
            )
        error = (
            _BANK_ERROR_PAGE.match(text)
            if text and len(text.strip()) <= _BANK_ERROR_PAGE_MAX_CHARS
            else None
        )
        if error:
            return (
                f"the bank's page ({host}) ended on a bare error page "
                f"('{error.group(1)} {error.group(2).strip()}') instead of going back to Flipkart"
            )
    return ""


#: How long a press of the bank's Submit gets to show it reached the bank: a
#: page load or a request leaving for the bank, its page moving on, or the
#: password box going away. A sign ends the wait at once; the full wait is
#: spent only to report that none came. Nothing is pressed again after a
#: press that went through, sign or not: a second sending of the password can
#: count as a wrong try, and banks lock the password after three.
BANK_SUBMIT_SIGN_MS = 10_000
#: After a press that raised (the control could not be pressed at all), how
#: long to look for a sign before the next way of submitting may be tried.
BANK_SUBMIT_RAISED_SIGN_MS = 2_000
#: After typing, how long to look for the page sending its form by itself (a
#: page that does so once its box is full) before anything is pressed. The
#: browser can report that page load a moment after the last key.
BANK_TYPED_LOOK_MS = 750

#: The password form's own submit controls, found from the password box: a
#: <button> with no type (in a form, that is a submit) or type="submit", and
#: an <input> of type submit or image.
_BANK_FORM_SUBMIT = (
    "xpath=ancestor::form[1]//*[(self::button and (not(@type) or @type='submit'))"
    " or (self::input and (@type='submit' or @type='image'))]"
)


#: Words a control the bank step presses must never say.
_NEVER_WORDS = ("cancel", "resend", "back", "change", "close", "forgot")


def _bank_button_by_words() -> str:
    """A control on the bank's page whose words say it sends the password —
    Submit, Continue, Verify, Proceed, Confirm, Authenticate — and never
    Cancel, Resend, Back, Change, Close or Forgot. Its words are its text, or
    its value, aria-label, alt or title. Buttons the page draws itself count
    (role="button", an onclick, a "btn" class), but only the innermost: a
    wrapper around a button is not a second button."""
    lower = (
        "translate(concat(normalize-space(.), ' ', @value, ' ', @aria-label, ' ', @alt, ' ', @title), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
    )
    says = " or ".join(
        f"contains({lower}, '{word}')"
        for word in ("submit", "continue", "verify", "proceed", "confirm", "authenticate")
    )
    never = " or ".join(f"contains({lower}, '{word}')" for word in _NEVER_WORDS)
    control = (
        "self::button or self::a or @role='button' or @onclick"
        " or (self::input and (@type='submit' or @type='button' or @type='image'))"
        " or contains(translate(@class, 'BTN', 'btn'), 'btn')"
    )
    return f"xpath=//*[({control}) and ({says}) and not({never}) and not(.//*[{control}])]"


_BANK_SUBMIT_BY_WORDS = _bank_button_by_words()

#: Whether Enter in the password box would send its form the plain way: the
#: box sits in a form with no submit control at all, so the browser posts the
#: form by itself and none of the page's own Submit step runs. With a submit
#: control, Enter presses that control, as a person's Enter does.
_ENTER_SENDS_PLAIN_FORM_JS = """(el) => {
  const form = el.form;
  if (!form) return false;
  const submits = Array.from(form.elements).some((e) => e.type === 'submit')
    || form.querySelector('input[type=image]') !== null;
  return !submits;
}"""

#: What the bank's page holds, for the record kept when an order does not
#: confirm: its forms (where they send, never what), its boxes (type and name,
#: never a value) and its buttons (their words). Taken before anything is
#: typed. 24 Nov 2026: the only record of HDFC's page was a picture of the
#: "404 Not Found" it ended on, which could not say why.
_BANK_OUTLINE_JS = """() => {
  const shown = (el) => {
    const box = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const bare = (url) => { try { const u = new URL(url, location.href); return u.host + u.pathname; } catch (e) { return ''; } };
  const words = (el) => String(el.innerText || el.getAttribute('value') || el.getAttribute('aria-label')
    || el.getAttribute('alt') || el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
  return {
    address: bare(location.href),
    title: (document.title || '').slice(0, 80),
    text: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').trim().slice(0, 300),
    forms: Array.from(document.forms).slice(0, 5).map((f) => ({
      id: f.id || f.getAttribute('name') || '',
      sends_to: bare(f.getAttribute('action') || location.href),
      method: (f.getAttribute('method') || 'get').toLowerCase(),
      submit_controls: Array.from(f.elements).filter((e) => e.type === 'submit').length
        + f.querySelectorAll('input[type=image]').length,
    })),
    boxes: Array.from(document.querySelectorAll('input, select, textarea')).slice(0, 25).map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      name: el.getAttribute('name') || el.id || '',
      max: el.maxLength > 0 ? el.maxLength : null,
      shown: shown(el),
      in_form: !!el.form,
    })),
    controls: Array.from(document.querySelectorAll(
      'button, a, [role=button], [onclick], input[type=submit], input[type=button], input[type=image]'
    )).slice(0, 30).map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      words: words(el),
      shown: shown(el),
      disabled: !!el.disabled,
      in_form: !!el.form,
    })),
  };
}"""


#: The longest any one look at the bank's page may take. While that page is
#: loading its next page, Playwright's reads of it (count, is_visible, all,
#: evaluate) wait for the next page with no end of their own — seen in
#: Chromium, 24 Nov 2026 — and a run must never hang on one.
BANK_READ_SECONDS = 3.0


async def _within(awaitable, seconds: float = BANK_READ_SECONDS):
    """``awaitable``'s result, or TimeoutError after ``seconds``."""
    return await asyncio.wait_for(awaitable, seconds)


def _bare_url(url: str) -> str:
    """An address without its query or fragment, for the bank step's record."""
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:160]


class _BankTraffic:
    """What the browser sent after Pay, and what came back: every page load,
    and anything but a plain picture-or-script fetch to a site that is not
    Flipkart. Addresses are kept without their query, and nothing a request
    carries is read, so the record never holds what a form sent. Fed by the
    request listeners on the page and on any window Pay opened.

    ``count`` is every such request; the press of the bank's Submit marks it
    before and reads it after, as one sign that the form reached the bank."""

    LIMIT = 80

    def __init__(self, ignore: frozenset[str] = frozenset()) -> None:
        self.ignore = ignore
        self.started = time.monotonic()
        self.count = 0
        #: Page loads among them: a page that sends its form by itself as the
        #: password is typed (some do, once the box is full) shows as one.
        self.documents = 0
        self.lines: list[str] = []

    def _wanted(self, request) -> bool:
        kind = (getattr(request, "resource_type", "") or "").lower()
        if kind == "document":
            return True
        host = _foreign_host(getattr(request, "url", "") or "")
        return bool(host) and host not in self.ignore and (request.method != "GET" or kind in ("xhr", "fetch"))

    def _note(self, line: str) -> None:
        if len(self.lines) < self.LIMIT:
            self.lines.append(f"{time.monotonic() - self.started:6.1f}s after Pay  {line}")

    def on_request(self, request) -> None:
        with contextlib.suppress(Exception):
            if self._wanted(request):
                self.count += 1
                kind = getattr(request, "resource_type", "") or "?"
                if kind == "document":
                    self.documents += 1
                self._note(f"sent     {request.method} {kind} {_bare_url(request.url)}")

    def on_response(self, response) -> None:
        with contextlib.suppress(Exception):
            if self._wanted(response.request):
                self._note(f"answered {response.status} {_bare_url(response.url)}")

    def on_failed(self, request) -> None:
        with contextlib.suppress(Exception):
            if self._wanted(request):
                failure = getattr(request, "failure", None)
                why = (failure if isinstance(failure, str) else "") or "no answer"
                self._note(f"failed   {why[:60]} {_bare_url(request.url)}")

    def listeners(self) -> tuple[tuple[str, Callable], ...]:
        return (("request", self.on_request), ("response", self.on_response), ("requestfailed", self.on_failed))


@dataclass
class BankStep:
    """What happened on the bank's page. ``problem`` says why the password
    could not be typed or sent (None when it was); ``how`` says how it was
    sent and what showed it reached the bank; ``sent`` is whether anything
    did show that. ``outline`` is the page as it was before typing."""

    problem: str | None = None
    how: str = ""
    sent: bool = False
    outline: dict | None = None
    tried: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


async def _bank_outline(place: object) -> dict | None:
    """The bank page's outline (``_BANK_OUTLINE_JS``), or None. Never raises."""
    try:
        outline = await _within(place.evaluate(_BANK_OUTLINE_JS))
    except Exception:  # noqa: BLE001 — the record can do without it
        return None
    return outline if isinstance(outline, dict) else None


async def _enter_bank_password(
    page: Page,
    password: str,
    popups: list | None = None,
    ignore: frozenset[str] = frozenset(),
    traffic: _BankTraffic | None = None,
) -> BankStep:
    """Type the card's password into the bank's one password box and submit.

    The box is looked for wherever the bank's page is (``_foreign_places``),
    for up to ``BANK_FIELD_WAIT_MS``. More than one box anywhere is refused,
    not guessed between. The step's ``problem`` is None on success, else why
    not — never the password, and never the driver's error text, which would
    repeat it.
    """
    waited = 0
    while True:
        places = _foreign_places(page, popups, ignore)
        for _host, place in places:
            boxes = place.locator(_PASSWORD_BOX)
            try:
                count = await _within(boxes.count())
            except Exception:  # noqa: BLE001 — a frame that went away, or is loading, while being looked at
                continue
            if count > 1:
                return BankStep(
                    problem=f"expected one password field on the bank's page, found {count}",
                    outline=await _bank_outline(place),
                )
            if count == 1:
                outline = await _bank_outline(place)
                step = await _type_and_submit(place, boxes.first, password, traffic)
                step.outline = outline
                return step
        if _is_confirmation_page(page.url or ""):
            return BankStep(problem="Flipkart confirmed before the bank's page asked for a password")
        if waited >= BANK_FIELD_WAIT_MS:
            return BankStep(
                problem="the bank's page showed no password field",
                outline=await _bank_outline(places[0][1]) if places else None,
            )
        await page.wait_for_timeout(500)
        waited += 500


async def _type_bank_password(place: object, box, password: str) -> str | None:
    """Type the password and submit the bank's form (:func:`_type_and_submit`):
    None on success, else why not."""
    return (await _type_and_submit(place, box, password)).problem


async def _type_and_submit(
    place: object, box, password: str, traffic: _BankTraffic | None = None
) -> BankStep:
    """Type into the bank's password box, then submit its form
    (:func:`_submit_bank_form`)."""
    step = BankStep()
    pages_before = traffic.documents if traffic is not None else 0
    url_before = getattr(place, "url", "") or ""
    try:
        with contextlib.suppress(Exception):
            await box.click(timeout=QUICK_TIMEOUT_MS)
        await box.fill("", timeout=QUICK_TIMEOUT_MS)
        await box.press_sequentially(password, delay=CARD_KEY_DELAY_MS)
    except Exception as exc:  # noqa: BLE001
        step.problem = f"could not fill the bank's password field ({exc.__class__.__name__})"
        return step
    moved = await _bank_sign(place, box, traffic, pages_before, url_before, BANK_TYPED_LOOK_MS, pages_only=True)
    if moved:
        # The page sent its form by itself as the box filled: pressing
        # anything now would send the password a second time.
        step.sent = True
        step.how = f"the bank's page sent it by itself as it was typed ({moved}; nothing pressed)"
        return step
    await _submit_bank_form(place, box, step, traffic)
    return step


async def _shown_controls(locator) -> list:
    """The matches of ``locator`` that are shown on the page. Never raises."""
    try:
        found = await _within(locator.all())
    except Exception:  # noqa: BLE001 — a page that cannot be searched shows nothing
        return []
    shown = []
    for control in found:
        with contextlib.suppress(Exception):
            if await _within(control.is_visible()):
                shown.append(control)
    return shown


#: Words a bank's Submit button can say on its own.
_SUBMIT_WORDS = frozenset({"submit", "continue", "verify", "proceed", "confirm", "authenticate"})
_CONTROL_WORDS_JS = """(el) => String(el.innerText || el.value || el.getAttribute('aria-label')
  || el.getAttribute('alt') || el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim().toLowerCase()"""


async def _not_saying_never_words(controls: list) -> list:
    """``controls`` less any whose words say Cancel, Resend, Back, Change,
    Close or Forgot. A control that cannot be read is kept (it may be an
    icon); a page that cannot be read at all keeps them all. Never raises."""
    kept = []
    for control in controls:
        words = ""
        with contextlib.suppress(Exception):
            words = str(await _within(control.evaluate(_CONTROL_WORDS_JS)) or "")
        if not any(word in words for word in _NEVER_WORDS):
            kept.append(control)
    return kept


async def _saying_only_a_submit_word(controls: list) -> list:
    """The controls whose whole words are one submit word ("Submit",
    "CONTINUE"). Never raises; a control that cannot be read is left out."""
    exact = []
    for control in controls:
        with contextlib.suppress(Exception):
            if str(await _within(control.evaluate(_CONTROL_WORDS_JS)) or "").strip(" .!") in _SUBMIT_WORDS:
                exact.append(control)
    return exact


async def _bank_sign(
    place: object,
    box,
    traffic: _BankTraffic | None,
    mark: int,
    url_before: str,
    wait_ms: int,
    *,
    pages_only: bool = False,
) -> str:
    """What showed the bank's form went off after a press, or "" when nothing
    did within ``wait_ms``: a page load or a request leaving for the bank, the
    bank's page moving to another address, or the password box going away.
    ``mark`` is ``traffic.count`` before the press — or, with ``pages_only``,
    ``traffic.documents``: after typing, only a page load is a sign, as a
    page may call home on its own while a box is typed in."""
    owner = getattr(place, "page", None) or place
    waited = 0
    while True:
        seen = (traffic.documents if pages_only else traffic.count) if traffic is not None else mark
        if seen > mark:
            return "the browser sent it to the bank"
        if (getattr(place, "url", "") or "") != url_before:
            return "the bank's page moved on"
        try:
            if not await _within(box.count(), 1.0) or not await _within(box.is_visible(), 1.0):
                return "the password box went away"
        except TimeoutError:
            return "the bank's page is loading its next page"
        except Exception:  # noqa: BLE001 — the frame itself went away: the page moved on
            return "the bank's page moved on"
        if waited >= wait_ms:
            return ""
        try:
            await owner.wait_for_timeout(250)
        except Exception:  # noqa: BLE001 — a window that closed has moved on
            return "the bank's window closed"
        waited += 250


async def _submit_bank_form(place: object, box, step: BankStep, traffic: _BankTraffic | None) -> None:
    """Submit the bank's password form the way a person would, once.

    The ways, in turn — the next only when the one before could not be
    pressed at all and nothing reached the bank:

    1. the form's one Submit button, when it shows exactly one (never the
       first of several, which can be the bank's Cancel);
    2. the one button on the bank's page whose words say Submit / Continue /
       Verify / Proceed / Confirm — the page's own button, which runs the
       page's own Submit step even when it is not a submit button of the form;
    3. Enter in the password box — but never when the box's form has no
       submit control at all: Enter then makes the browser send the form by
       itself, skipping the page's own step.

    Every press is made without waiting for the page that follows: the
    bank's server can take longer to answer than any wait here, which is
    slow, not refused. What follows a press is looked for instead
    (:func:`_bank_sign`).

    Seen live: 23 Nov 2026 (job 629) the form's one submit button "timed out"
    on click; 24 Nov (job 689) HDFC's page showed no submit button of the
    form, Enter sent the form by itself, HDFC's server did not answer for
    30 s and its page ended on "404 Not Found".
    """
    url_before = getattr(place, "url", "") or ""
    ways: list[tuple[str, object | None]] = []
    # A form whose one submit control is Cancel (the real button drawn
    # outside it) must not have Cancel pressed for Submit.
    in_form = await _not_saying_never_words(await _shown_controls(box.locator(_BANK_FORM_SUBMIT)))
    if len(in_form) == 1:
        ways.append(("the form's Submit button", in_form[0]))
    worded = await _shown_controls(place.locator(_BANK_SUBMIT_BY_WORDS))
    if len(worded) > 1:
        # "Submit" beside a link such as "verify with an OTP instead": the
        # one control saying a submit word and nothing else is the button.
        worded = await _saying_only_a_submit_word(worded) or worded
    if len(worded) == 1:
        ways.append(("the page's Submit button", worded[0]))
    elif len(worded) > 1:
        step.skipped.append(f"{len(worded)} buttons say Submit/Continue/Verify — none was guessed at")
    sends_plain = False
    with contextlib.suppress(Exception):
        sends_plain = bool(await _within(box.evaluate(_ENTER_SENDS_PLAIN_FORM_JS)))
    if sends_plain:
        step.skipped.append(
            "Enter was not pressed: the password's form has no submit control, so Enter would send "
            "the form without the bank page's own Submit step"
        )
    else:
        ways.append(("Enter in the password box", None))

    for label, control in ways:
        mark = traffic.count if traffic is not None else 0
        try:
            if control is None:
                await box.press("Enter", timeout=QUICK_TIMEOUT_MS, no_wait_after=True)
            else:
                await control.click(timeout=QUICK_TIMEOUT_MS, no_wait_after=True)
        except Exception as exc:  # noqa: BLE001
            step.tried.append(f"{label} ({exc.__class__.__name__})")
            sign = await _bank_sign(place, box, traffic, mark, url_before, BANK_SUBMIT_RAISED_SIGN_MS)
            if not sign:
                log.warning(
                    "could not press %s on the bank's page (%s); nothing reached the bank",
                    label,
                    exc.__class__.__name__,
                )
                continue
        else:
            sign = await _bank_sign(place, box, traffic, mark, url_before, BANK_SUBMIT_SIGN_MS)
        step.sent = bool(sign)
        step.how = (
            f"sent with {label} ({sign})"
            if sign
            else (
                f"{label} was pressed, but nothing was seen reaching the bank within "
                f"{BANK_SUBMIT_SIGN_MS // 1000}s (not pressed again)"
            )
        )
        return
    found = "; ".join([*(f"tried {t}" for t in step.tried), *step.skipped])
    step.problem = "could not submit the bank's password form — " + (
        found or "no Submit button was found on the bank's page"
    )


def _save_bank_record(host: str | None, step: BankStep | None, traffic: _BankTraffic | None) -> Path | None:
    """Keep what the bank step saw, next to the screenshots, when an order
    does not confirm: the bank's page before typing (its outline), what was
    pressed, and what the browser sent and got back after Pay. Nothing in it
    is a value typed or sent. Never raises; None when nothing was saved."""
    try:
        lines = [
            f"bank step after Pay, {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
            f"bank's page: {host or 'none reached'}",
        ]
        if step is not None:
            lines.append(f"result: {step.problem or step.how}")
            lines += [f"tried: {t}" for t in step.tried] + [f"not done: {s}" for s in step.skipped]
            if step.outline:
                outline = json.dumps(step.outline, ensure_ascii=False, indent=1)
                lines += ["", "the bank's page before typing (no values):", _LONG_DIGIT_RUN.sub("[digits]", outline)[:8000]]
        if traffic is not None and traffic.lines:
            lines += ["", "what the browser sent and got back after Pay (addresses without their query):", *traffic.lines]
        DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
        _prune_diagnostics()
        path = DIAGNOSTICS_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-bank-step.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — a record is never worth failing a run over
        log.warning("could not save the bank step's record (%s)", exc.__class__.__name__)
        return None
    log.info("saved the bank step's record to %s", path)
    return path


#: Eight or more digits in a row (spaces and dashes between allowed): taken
#: out of the bank page's outline, which can show a masked card or a phone.
_LONG_DIGIT_RUN = re.compile(r"\d(?:[\d \-]{6,})\d")


async def _submit_and_confirm(
    page: Page,
    *,
    payment_mode: str,
    outcome: dict,
    reserve: Callable[[dict], object] | None = None,
    card: CardInfo | None = None,
) -> dict:
    """Send the order, then establish from Flipkart whether it exists.

    The order request itself is watched the whole time, not just while the
    buttons are pressed: Confirm order sends it asynchronously, a moment after
    the click has returned. The buttons say what we pressed; the watcher says
    what actually left the browser.

    The record is written BEFORE the send. Everything after the send can be
    cut short — Ctrl+C in the CLI, an API restart cancelling the job, a crash —
    and none of those handlers run, so a record written afterwards might never
    be written at all. An order that exists with no record is invisible to the
    unit limits, to the alerts and to the next run, which then buys it again.
    """
    reservation = None
    if reserve is not None:
        try:
            reservation = reserve({**outcome, "status": OrderStatus.unconfirmed})
        except Exception as exc:  # noqa: BLE001
            # The row being written carries the card number; a database error's
            # text can carry the statement's parameters. The error's type is
            # all that is logged or shown (audit F-7, 25 Oct 2026).
            log.error(
                "could not record the order before sending it (%s) — not sending", exc.__class__.__name__
            )
            return {
                **outcome,
                "status": OrderStatus.failed,
                "error": (
                    f"could not record the order before sending it ({exc.__class__.__name__}) — nothing "
                    "was sent to Flipkart, so nothing was ordered"
                ),
            }
    outcome = {**outcome, "reservation": reservation}

    order_calls: list[str] = []
    #: What Flipkart answered the order request with (HTTP status codes).
    order_answers: list[int] = []
    #: Sites other than Flipkart that the page or a frame in it went to.
    visited: list[str] = []
    #: Windows the page opened (a bank's page can open in one).
    popups: list = []

    def watch(request) -> None:
        if request.method == "POST" and _ORDER_CALL.search(request.url or ""):
            order_calls.append(request.url)

    def watch_answer(response) -> None:
        with contextlib.suppress(Exception):
            if response.request.method == "POST" and _ORDER_CALL.search(response.url or ""):
                order_answers.append(int(response.status))

    def watch_navigation(frame) -> None:
        host = _foreign_host(getattr(frame, "url", "") or "")
        if host and host not in visited and host not in framed_before and len(visited) < 6:
            visited.append(host)

    def watch_popup(popup) -> None:
        popups.append(popup)
        if traffic is not None:
            for event, handler in traffic.listeners():
                with contextlib.suppress(Exception):
                    popup.on(event, handler)

    #: Sites already framed in the payment page before Pay: never the bank's.
    framed_before = frozenset(host for host, _ in _foreign_places(page))
    #: What a card run's browser sends after Pay: the sign that the bank's form
    #: went off, and the record kept when the order does not confirm.
    traffic = _BankTraffic(framed_before) if payment_mode != PAYMENT_COD else None
    listeners = (
        ("request", watch),
        ("response", watch_answer),
        ("framenavigated", watch_navigation),
        ("popup", watch_popup),
        *(traffic.listeners() if traffic is not None else ()),
    )
    for event, handler in listeners:
        page.on(event, handler)
    try:
        submission = await (
            _submit_cod(page) if payment_mode == PAYMENT_COD else _submit_card(page)
        )
        if order_calls and not submission.sent:
            # The checkout sent the order without the step we expected (say, no
            # popup for this account). It is sent, whatever the buttons did.
            log.warning("an order request left the browser although the flow had not sent one")
            submission = Submission(
                sent=True, note=f"{submission.note}, but Flipkart's order request was sent anyway"
            )
        if not submission.sent:
            log.error("order not submitted: %s — nothing was sent to Flipkart", submission.note)
            return {
                **outcome,
                "status": OrderStatus.failed,
                "error": f"{submission.note} — nothing was sent to Flipkart, so nothing was ordered",
            }

        # From here an order may exist, so nothing below may raise or say
        # "failed": a real order recorded as failed hands its units back, and
        # the next attempt buys them a second time.
        bank_host: str | None = None
        bank_note = ""
        bank_step: BankStep | None = None
        confirmation_timeout = ORDER_CONFIRMATION_TIMEOUT_MS
        try:
            if payment_mode != PAYMENT_COD:
                # A card payment is routed to the bank's own page (3-D Secure).
                # HDFC's corporate cards ask for a password there, which the
                # card sheet carries (the owner, 26 Oct 2026): typed and
                # submitted here. A bank that asks for a one-time code instead
                # needs a person at a visible browser window; either way the
                # wait is then the bank's, not Flipkart's, and is extended by
                # the configured allowance (audit F-5).
                bank_host, _bank_place = await _bank_page(page, popups, framed_before)
                if bank_host:
                    confirmation_timeout += max(0, int(get_settings().bank_otp_fill_wait)) * 1000
                    if card is not None and card.password:
                        bank_step = await _enter_bank_password(
                            page, card.password, popups, framed_before, traffic
                        )
                        problem = bank_step.problem
                        bank_note = (
                            f"the card's password was entered on the bank's page ({bank_host}) and {bank_step.how}"
                            if problem is None
                            else f"the card's password could not be entered on the bank's page ({bank_host}): {problem}"
                        )
                        log.log(
                            logging.ERROR
                            if problem is not None
                            else logging.INFO
                            if bank_step.sent
                            else logging.WARNING,
                            "%s",
                            bank_note,
                        )
                    else:
                        bank_note = (
                            f"no password was typed on the bank's page ({bank_host}), so its "
                            "authentication was not completed — this card step needs a person to "
                            'finish it in a visible browser (the "finish the bank\'s step by hand" option)'
                        )
                        log.warning("%s", bank_note)
            order_id, why_not = await await_order_confirmation(page, timeout_ms=confirmation_timeout)
        except Exception as exc:  # noqa: BLE001
            order_id, why_not = None, f"checking for Flipkart's confirmation failed ({exc})"
        if order_id is None and bank_note:
            why_not = f"{bank_note}; {why_not}"

        if order_id is None:
            # Whether the order request was seen leaving the browser is the
            # operator's best guide to whether this one exists. A watcher can
            # miss a request, so it informs the note but never turns the
            # status into "failed".
            seen = (
                "Flipkart's order request WAS sent"
                + (f" (answered {', '.join(str(code) for code in order_answers)})" if order_answers else "")
                if order_calls
                else "no order request was seen leaving the browser"
            )
            # Where the browser went after Pay is the other half of "what did
            # Pay do": the first live card runs (26 Oct 2026) ended here with
            # no word on it, and the screenshot alone could not say.
            went = ""
            if payment_mode != PAYMENT_COD:
                try:
                    hosts = list(
                        dict.fromkeys([*visited, *(host for host, _ in _foreign_places(page, popups, framed_before))])
                    )
                    went = (
                        f"pages away from Flipkart after Pay: {', '.join(hosts)}"
                        if hosts
                        else "the browser never left Flipkart after Pay and no bank page opened"
                    )
                except Exception:  # noqa: BLE001 — after the send nothing may raise; the note can do without it
                    went = ""
            blocked = ""
            if payment_mode != PAYMENT_COD:
                try:
                    blocked = await _bank_authentication_blocked(page, popups, framed_before)
                except Exception:  # noqa: BLE001 — after the send nothing may raise
                    blocked = ""
            note = "; ".join(part for part in (submission.note, blocked, why_not, seen, went) if part)
            log.error(
                "order UNCONFIRMED — it was submitted, so it MAY exist; check the "
                "account's orders before ordering again: %s",
                note,
            )
            if payment_mode != PAYMENT_COD:
                _save_bank_record(bank_host, bank_step, traffic)
            await _snapshot(page, "unconfirmed")
            return {
                **outcome,
                "status": OrderStatus.unconfirmed,
                "order_id": None,
                # What the run saw, for the operator's message (the error
                # below adds what to do about it).
                "why_unconfirmed": note,
                "error": (
                    f"{note}. The order was submitted to Flipkart and MAY exist — "
                    "check this account's orders before ordering again."
                ),
            }

        log.info("order placed: %s for ₹%.2f", order_id, outcome["amount"])
        return {**outcome, "status": OrderStatus.placed, "order_id": order_id}
    finally:
        for event, handler in listeners:
            with contextlib.suppress(Exception):
                page.remove_listener(event, handler)
        for popup in popups if traffic is not None else ():
            for event, handler in traffic.listeners():
                with contextlib.suppress(Exception):
                    popup.remove_listener(event, handler)


#: How long a diagnostic screenshot is kept. They show a checkout — a name, an
#: address, a phone, a GSTIN — so they are not kept for ever.
DIAGNOSTICS_KEEP_DAYS = 14


#: Boxes a diagnostic screenshot must never show: the card form's and any
#: password box (the bank's page, or a sign-in).
_SECRET_BOXES = (S.CARD_NUMBER, S.CARD_EXPIRY, S.CARD_CVV, "//input[@type='password']")


async def _snapshot(page: Page, label: str) -> None:
    """Best-effort screenshot of the page an order was left on. Never raises.

    On a card run the page left behind is often the payment page, and a
    picture of it would hold the card number and expiry in clear (audit F-3,
    25 Oct 2026). Those boxes are painted over in the picture itself, and the
    page is left as it was. They used to be emptied first, and emptying them
    made Flipkart's form print "Card number is required" and "Enter valid
    month/year": the first live card runs' screenshots (26 Oct 2026) then
    showed complaints the run itself had caused. Only when a painted-over
    picture cannot be taken are the boxes emptied, and a plain picture taken
    instead — and only once every one of them reads back empty. A box that
    keeps its value, or cannot be read, means no picture at all: a picture
    with the card in clear is never taken.

    Older screenshots are pruned at the same time, so the folder holds a
    fortnight, not a history.
    """
    _prune_diagnostics()
    path = DIAGNOSTICS_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{label}.png"
    try:
        masks = [page.locator(f"xpath={xpath}") for xpath in _SECRET_BOXES]
        await page.screenshot(path=str(path), full_page=True, mask=masks, mask_color="#000000")
        log.info("saved a screenshot of the last page to %s", path)
        return
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "could not save a screenshot with the card boxes painted over (%s); emptying them instead",
            exc.__class__.__name__,
        )
    if not await _secret_boxes_emptied(page):
        # Fail closed: the plain picture would show whatever a box still holds.
        log.warning("picture skipped: card boxes could not be cleared")
        return
    try:
        await page.screenshot(path=str(path), full_page=True)
        log.info("saved a screenshot of the last page to %s", path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save a screenshot: %s", exc.__class__.__name__)


async def _secret_boxes_emptied(page: Page) -> bool:
    """Empty every card and password box on the page, then read each one back.

    True only when every match of every ``_SECRET_BOXES`` xpath reads empty,
    as on a page with no such box at all. A box that keeps its value, or that
    cannot be counted or read, makes it False. Every match, not the first
    alone: the fill used to empty the first box of each kind and the plain
    picture was then taken without a look (review of 27 Oct 2026), while a
    page can hold a saved card's CVV box beside the new card's, or two
    password boxes.

    A page that is not Flipkart's own is left untouched and makes it False
    too: a card run ends on the bank's 3-D Secure page, and the tool never
    types there — not even "" to empty a box — so its promise that it types
    nothing on the bank's page holds, and a person finishing the step by hand
    keeps the password they are part-way through typing. The masked shot in
    :func:`_snapshot`, which types nothing, is tried first and is what the
    bank page normally yields; only its fallback reaches here.
    """
    if _foreign_host(getattr(page, "url", "") or ""):
        return False
    try:
        for xpath in _SECRET_BOXES:
            for box in await page.locator(f"xpath={xpath}").all():
                with contextlib.suppress(Exception):  # a box that will not empty is caught by the read below
                    await box.fill("", timeout=1_000)
        # Read after every box has been emptied, the list taken afresh: a page
        # that redraws its form as one box empties can bring a value back.
        for xpath in _SECRET_BOXES:
            for box in await page.locator(f"xpath={xpath}").all():
                if await box.input_value(timeout=1_000):
                    return False
    except Exception:  # noqa: BLE001 — a box that cannot be read is not known to be empty
        return False
    return True


def _prune_diagnostics(keep_days: int = DIAGNOSTICS_KEEP_DAYS) -> int:
    """Delete diagnostic screenshots and bank-step records older than
    ``keep_days``; returns how many."""
    removed = 0
    try:
        if not DIAGNOSTICS_DIR.is_dir():
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86_400
        for file in [*DIAGNOSTICS_DIR.glob("*.png"), *DIAGNOSTICS_DIR.glob("*-bank-step.txt")]:
            try:
                if file.stat().st_mtime < cutoff:
                    file.unlink()
                    removed += 1
            except OSError:
                continue
    except Exception:  # noqa: BLE001 — housekeeping never fails a run
        return removed
    return removed


# --- delivery-date and cart-amount caps --------------------------------------
#: "Delivery by Oct 28, Wed" / "Delivery by Mon Oct 28"
_DELIVERY_DATE_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3,9})|([A-Za-z]{3,9})\s+(\d{1,2})"
)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], start=1
    )
}


def parse_delivery_days(text: str | None, *, today: date | None = None) -> int | None:
    """Days from today until the promised delivery date, or None if unreadable.

    Flipkart writes the promise without a year ("Delivery by Oct 28, Wed"), so a
    date that has already passed is read as next year rather than as a negative
    number of days.
    """
    if not text:
        return None
    match = _DELIVERY_DATE_RE.search(text)
    if not match:
        return None

    if match.group(1):
        day, month_name = int(match.group(1)), match.group(2)
    else:
        month_name, day = match.group(3), int(match.group(4))
    month = _MONTHS.get((month_name or "")[:3].lower())
    if not month:
        return None

    today = today or date.today()
    try:
        promised = date(today.year, month, day)
    except ValueError:
        return None
    if promised < today:  # the promise cannot be in the past; it is next year
        try:
            promised = date(today.year + 1, month, day)
        except ValueError:
            return None
    return (promised - today).days


async def read_delivery_days(page: Page) -> int | None:
    """How many days out the delivery promise is, from the checkout page."""
    text = await _field(page, S.CHECKOUT_DELIVERY_BY)
    return parse_delivery_days(text)


async def read_cart_amount(page: Page) -> float | None:
    """The cart figure, which is NOT the payable total (see ORDER_TOTAL)."""
    text = await _field(page, S.CHECKOUT_CART_AMOUNT)
    if not text:
        return None
    amount = parse_amount(text)
    return amount if amount > 0 else None


async def checkout_is_location_gated(page: Page, *, timeout_ms: int = 3_000) -> bool:
    """True when Flipkart is withholding checkout until a map pin is set.

    Confirmed live 2026-10-23: a gated checkout renders no Continue / Place
    Order / Pay control anywhere — only "Set Location". Without this check the
    run walks through every later step, times each one out in turn, and reports
    a vague failure after two minutes instead of the one thing that is wrong.
    """
    gate = page.locator(f"xpath={S.CHECKOUT_LOCATION_GATE}")
    try:
        # Wait for whichever renders: the way on (Continue) or the gate. Waiting
        # for the gate alone cost its whole timeout on every healthy checkout,
        # where it is — correctly — absent.
        await page.locator(
            f"xpath={S.CHECKOUT_CONTINUE_CTA} | {S.CHECKOUT_LOCATION_GATE}"
        ).first.wait_for(timeout=timeout_ms)
        if not await gate.count():
            return False
        text = await gate.first.inner_text(timeout=QUICK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — absent is the normal, healthy case
        return False
    return bool((text or "").strip())


async def continue_to_payment(page: Page, *, timeout_ms: int = 20_000) -> bool:
    """Click Continue on /viewcheckout to reach the payment page.

    The legacy flow had no such step: it expected payment controls on the
    checkout page itself. They live on a separate /payments url, which is why
    every payment selector timed out.
    """
    if S.PAYMENTS_PATH in (page.url or ""):
        return True  # the cart route can land straight on /payments

    if not await on_checkout(page):
        log.error(
            "not on the checkout page (still at %s) — Buy now did not take effect",
            (page.url or "")[:80],
        )
        return False

    if not await click_until_navigated(page, S.CHECKOUT_CONTINUE_CTA, S.PAYMENTS_PATH):
        log.error("Continue never took effect (page did not reach the payment step)")
        return False
    return True


async def add_gift_cards(page: Page, cards: list[GiftCardInfo]) -> str | None:
    """Enter each gift card on the payment page's "Have a Flipkart Gift
    Card?" form. Returns an error string, or None when every card was added."""
    if not cards:
        return "no gift card supplied for a gift card payment"
    if len(cards) > S.GIFT_CARDS_PER_PAYMENT:
        return f"at most {S.GIFT_CARDS_PER_PAYMENT} gift cards can be added to one payment"
    try:
        await page.locator(f"xpath={S.GIFT_CARD_OPEN}").first.click(timeout=STEP_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        return "the payment page offered no gift card option"
    for number, card in enumerate(cards, start=1):
        try:
            field = page.locator(f"xpath={S.GIFT_CARD_NUMBER}").first
            await field.wait_for(timeout=STEP_TIMEOUT_MS)
            await field.fill(card.number)
            await page.locator(f"xpath={S.GIFT_CARD_PIN}").first.fill(card.pin)
            await page.locator(f"xpath={S.GIFT_CARD_ADD}").first.click(timeout=STEP_TIMEOUT_MS)
            await page.wait_for_timeout(2_500)
        except Exception as exc:  # noqa: BLE001
            return f"gift card {number} could not be added ({exc.__class__.__name__})"
        # Flipkart leaves the form filled when it refuses a card; it clears it
        # when the card is taken.
        try:
            left = await page.locator(f"xpath={S.GIFT_CARD_NUMBER}").first.input_value(timeout=QUICK_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — the form closed: taken
            left = ""
        if left and left.strip() == card.number:
            return f"Flipkart did not accept gift card {number} (ending {card.number[-4:]})"
    log.info("added %d gift card(s) to the payment", len(cards))
    return None


def gift_cards_left_to_pay(payment_mode: str, final_amount: float | None) -> str | None:
    """A "Gift Voucher only" order must owe nothing once its gift cards are on:
    Pay would otherwise charge the rest to whatever method the page holds.
    Returns the reason to stop, or None."""
    if payment_mode != PAYMENT_GV:
        return None
    if final_amount is None or final_amount > 0:
        left = "an unreadable amount" if final_amount is None else f"₹{final_amount:,.2f}"
        return f"the gift cards left {left} to pay on a Gift Voucher only order — nothing was charged"
    return None


async def choose_payment_option_by_label(page: Page, mode: str) -> str | None:
    """COD with a card advance, RTGS, IMPS: pick the option by its label in the
    payment-option list. Returns an error string when the list has no such
    option, or offers it greyed out — never a click on other text."""
    for label in S.PAY_OPTION_LABELS.get(mode, ()):
        quoted = '"' + label.replace('"', "") + '"'
        options = page.locator(f"xpath={S.PAY_OPTION % quoted}")
        try:
            if await options.count() != 1:
                continue  # absent, or not one unambiguous option
            option = options.first
            classes = await option.get_attribute("class") or ""
            if S.PAY_OPTION_DISABLED_CLASS in classes:
                return f"{label} is shown but unavailable on this order's payment page — nothing was charged"
            await option.click(timeout=STEP_TIMEOUT_MS)
            log.info("payment option selected: %s", label)
            return None
        except Exception:  # noqa: BLE001
            continue
    names = {"cod_card": "COD + Card (advance payment)", "rtgs": "RTGS", "imps": "IMPS"}
    return f"Flipkart offers no {names.get(mode, mode)} on this order's payment page"


async def choose_payment_method(page: Page, mode: str, *, timeout_ms: int = 15_000) -> str | None:
    """Select a payment method. Returns an error string, or None on success.

    Checks availability first: Flipkart renders an unavailable method greyed
    out rather than hiding it, so clicking blindly selects nothing and the run
    pays by whatever was already highlighted.
    """
    if mode in (PAYMENT_COD_CARD, PAYMENT_RTGS, PAYMENT_IMPS):
        return await choose_payment_option_by_label(page, mode)
    if mode == PAYMENT_GV:
        return None  # the gift cards are the payment
    if mode == PAYMENT_COD:
        try:
            # The option renders whether or not COD is offered, so wait for IT,
            # then look for the unavailable marker. Waiting for the marker alone
            # cost 3s on every order where COD was — correctly — available.
            await page.locator(f"xpath={S.PAY_OPTION_COD}").first.wait_for(timeout=timeout_ms)
            dead = page.locator(f"xpath={S.PAY_OPTION_COD_DISABLED}")
            if await dead.count() and await dead.first.inner_text(timeout=QUICK_TIMEOUT_MS):
                return "Cash on Delivery is not available for this product"
        except Exception:  # noqa: BLE001 — not marked unavailable, carry on
            pass
        target_xpath = S.PAY_OPTION_COD
    else:
        target_xpath = S.PAY_OPTION_CARD

    try:
        await page.locator(f"xpath={target_xpath}").first.click(timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        return f"could not select the payment method: {exc}"

    # Say which method was chosen: the operator needs to see that a COD run
    # really went COD, not quietly fall through to whatever was preselected.
    log.info(
        "payment method selected: %s",
        "Cash on Delivery" if mode == PAYMENT_COD else "Credit / Debit / ATM Card",
    )
    return None


#: What ``use_balance_toggle`` found on the payment page.
TOGGLE_APPLIED = "applied"
TOGGLE_ALREADY = "already on"
TOGGLE_ABSENT = "not offered"


async def use_balance_toggle(page: Page, row_xpath: str, *, timeout_ms: int = QUICK_TIMEOUT_MS) -> str:
    """Turn on a payment-page balance toggle (SuperCoins, gift card): the
    innermost row naming it that holds a checkbox or switch. Returns
    TOGGLE_APPLIED, TOGGLE_ALREADY or TOGGLE_ABSENT — never raises, because a
    balance the page does not offer must not stop an order on its own."""
    row = page.locator(f"xpath={row_xpath}").first
    try:
        await row.wait_for(timeout=timeout_ms)
    except Exception:  # noqa: BLE001 — absent: no balance to use
        return TOGGLE_ABSENT
    try:
        box = row.locator("xpath=.//input[@type='checkbox'] | .//*[@role='checkbox' or @role='switch']").first
        checked = await box.evaluate(
            "el => el.type === 'checkbox' ? el.checked : el.getAttribute('aria-checked') === 'true'"
        )
        if checked:
            return TOGGLE_ALREADY
        # The DOM click: a pointer click on these React-Native-Web rows is often a no-op.
        await box.evaluate("el => el.click()")
        await page.wait_for_timeout(1_500)  # the total redraws with the balance taken off
        checked = await box.evaluate(
            "el => el.type === 'checkbox' ? el.checked : el.getAttribute('aria-checked') === 'true'"
        )
        return TOGGLE_APPLIED if checked else TOGGLE_ABSENT
    except Exception as exc:  # noqa: BLE001
        log.warning("balance toggle could not be used: %s", exc.__class__.__name__)
        return TOGGLE_ABSENT


async def select_size(page: Page, size: str, *, timeout_ms: int = QUICK_TIMEOUT_MS) -> bool:
    """Pick an apparel size. Returns False when the chip is not there."""
    try:
        xpath = S.SIZE_CHIP % f'"{size}"'
        await page.locator(f"xpath={xpath}").first.click(timeout=timeout_ms)
        await page.wait_for_timeout(1500)  # the variant reload changes the pid
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("could not select size %r: %s", size, exc)
        return False


#: How long the cart page gets to draw its list after loading: its rows
#: arrive a moment after the page itself.
CART_SETTLE_MS = 2_500
#: How long "Add to cart" gets to turn into "Go to cart" — Flipkart's own sign
#: that the product is in the cart — before the press is tried again.
ADD_TO_CART_PROOF_MS = 6_000
#: Product-page wording that says why a product cannot go in the cart.
_CART_REFUSALS = re.compile(
    r"(not deliverable[^.\n]{0,60}|currently unavailable[^.\n]{0,40}|out of stock[^.\n]{0,40}"
    r"|sold out[^.\n]{0,30}|please select (?:a )?size[^.\n]{0,20}|select size[^.\n]{0,20})",
    re.IGNORECASE,
)
#: "Flipkart (2)" on the cart's own tab: how many items the cart holds.
_CART_COUNT_RE = re.compile(r"\((\d+)\)")


async def empty_cart(page: Page, *, max_items: int = 20) -> int:
    """Remove everything from the cart — and only from the cart. Returns how
    many items were removed.

    This is NOT housekeeping — it is a safety requirement. "Place Order" buys
    the WHOLE cart, so an item left from a previous session would be bought
    alongside the intended product and charged to the same card. Observed live:
    a cart holding one unrelated phone totalled ₹18,511 against a ₹212 top.

    Only a cart line's "Remove" is pressed (``S.CART_REMOVE_ITEM``): the
    "Saved For Later" list on the same page carries "Remove" links too, and on
    an empty cart the first "Remove" anywhere is a saved item's (seen live on
    26 Oct 2026: 25 saved items under an empty cart). A press that opens
    Flipkart's confirmation box is confirmed in that box only.
    """
    removed = 0
    await page.goto(S.CART_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(CART_SETTLE_MS)
    in_cart = page.locator(f"xpath={S.CART_REMOVE_ITEM}")
    for _ in range(max_items):
        try:
            if not await in_cart.count():
                break
            # The DOM click: a pointer click on these React-Native-Web links
            # is often a silent no-op (the same trap as Buy now).
            await in_cart.first.evaluate("el => el.click()")
            await page.wait_for_timeout(1_500)
            confirm = page.locator(f"xpath={S.CART_REMOVE_CONFIRM}")
            if await confirm.count():
                await confirm.first.evaluate("el => el.click()")
                await page.wait_for_timeout(2_000)
            removed += 1
        except Exception:  # noqa: BLE001 — nothing left that can be removed
            break
    if removed:
        log.info("emptied the cart (%d item(s)) before ordering", removed)
    return removed


async def _shows_up(page: Page, xpath: str, wait_ms: int) -> bool:
    """Whether ``xpath`` matches something within ``wait_ms``."""
    waited = 0
    while True:
        try:
            if await page.locator(f"xpath={xpath}").count():
                return True
        except Exception:  # noqa: BLE001 — a page mid-navigation answers nothing
            pass
        if waited >= wait_ms:
            return False
        await page.wait_for_timeout(500)
        waited += 500


async def _cart_refusal(page: Page) -> str | None:
    """What the product page says about why the product cannot be bought, if anything."""
    body = await _field(page, S.PAGE_BODY) or ""
    match = _CART_REFUSALS.search(" ".join(body.split()))
    return match.group(0).strip() if match else None


async def _cart_says_empty(page: Page) -> bool:
    """Whether the cart page shows its "Your cart is empty!" heading."""
    try:
        heading = page.locator(f"xpath={S.CART_EMPTY_TEXT}")
        if not await heading.count():
            return False
        text = await heading.first.inner_text(timeout=QUICK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — no heading read is no proof of an empty cart
        return False
    return "cart is empty" in (text or "").lower()


async def add_to_cart_and_open(page: Page) -> tuple[bool, str]:
    """Add the open product to the cart, open the cart, and make sure the
    product is what it holds. Returns ``(ok, why_not)``.

    Proof at every step, because this path once reached "Place Order" on an
    empty cart (live, 25 Oct 2026, on the account that is offered only Add to
    cart): the press must turn "Add to cart" into "Go to cart"; a pointer
    press that does not is tried again as a DOM click; and the cart page must
    not say it is empty, and must hold one item — "Place Order" buys the
    whole cart, so anything else in it is refused, never bought.
    """
    added = False
    for attempt in range(3):
        if attempt and S.CART_PATH in (page.url or ""):
            added = True  # the press went straight to the cart
            break
        button = page.locator(f"xpath={S.ADD_TO_CART_BTN}").first
        try:
            if attempt == 0:
                await button.click(timeout=STEP_TIMEOUT_MS)
            else:
                await button.evaluate("el => el.click()")
        except Exception as exc:  # noqa: BLE001
            log.warning("pressing Add to cart failed (try %d): %s", attempt + 1, exc)
            continue
        if await _shows_up(page, S.GO_TO_CART_BTN, ADD_TO_CART_PROOF_MS) or S.CART_PATH in (page.url or ""):
            added = True
            break
        log.info("Add to cart did not take on try %d; pressing it again", attempt + 1)
    if not added:
        reason = await _cart_refusal(page)
        why = f"the page says: {reason}" if reason else "Add to cart did not respond"
        log.error("the product did not go in the cart: %s", why)
        return False, f"Flipkart did not put the product in the cart ({why})"

    if S.CART_PATH not in (page.url or ""):
        try:
            await page.locator(f"xpath={S.GO_TO_CART_BTN}").first.click(timeout=STEP_TIMEOUT_MS)
            await page.wait_for_url(f"**{S.CART_PATH}**", timeout=PAGE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 — open the cart directly if the link moved
            await page.goto(S.CART_URL, wait_until="domcontentloaded")
    await page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    await page.wait_for_timeout(CART_SETTLE_MS)

    if await _cart_says_empty(page):
        log.error("the cart page says it is empty although Add to cart took")
        return False, "the cart was empty when the order was to be placed — Flipkart did not keep the product in it"
    # Looked for first: a label that is not there would otherwise cost the
    # whole default wait before saying so.
    count_text = None
    try:
        tab = page.locator(f"xpath={S.CART_ITEM_COUNT}")
        if await tab.count():
            count_text = await tab.first.inner_text(timeout=QUICK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — no count shown: the price cap still guards the total
        count_text = None
    count = _CART_COUNT_RE.search(count_text or "")
    if count and int(count.group(1)) > 1:
        log.error("the cart holds %s items, not just this product; refusing", count.group(1))
        return False, (
            f"the cart holds {count.group(1)} items, not just this product — Place Order would buy all of "
            "them, so nothing was ordered. Empty the account's cart (not its Saved For Later list) and run again"
        )
    return True, ""


async def add_links_to_cart(page: Page, links: list[str]) -> tuple[bool, str]:
    """Put one of each product link in an EMPTY cart, open it, and prove it
    holds exactly ``len(links)`` lines. Returns ``(ok, why_not)``.

    The "+ Add product URL" order: one order buys several products together.
    The caller empties the cart first — "Place Order" buys the whole cart.
    """
    for number, link in enumerate(links, start=1):
        await page.goto(link, wait_until="domcontentloaded")
        added = False
        for attempt in range(3):
            button = page.locator(f"xpath={S.ADD_TO_CART_BTN}").first
            try:
                if attempt == 0:
                    await button.click(timeout=STEP_TIMEOUT_MS)
                else:
                    await button.evaluate("el => el.click()")
            except Exception as exc:  # noqa: BLE001
                log.warning("Add to cart failed for product %d (try %d): %s", number, attempt + 1, exc)
                continue
            if await _shows_up(page, S.GO_TO_CART_BTN, ADD_TO_CART_PROOF_MS) or S.CART_PATH in (page.url or ""):
                added = True
                break
        if not added:
            reason = await _cart_refusal(page)
            why = f"the page says: {reason}" if reason else "Add to cart did not respond"
            return False, f"product {number} of {len(links)} did not go in the cart ({why})"

    await page.goto(S.CART_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(CART_SETTLE_MS)
    if await _cart_says_empty(page):
        return False, "the cart was empty when the order was to be placed"
    count_text = None
    try:
        tab = page.locator(f"xpath={S.CART_ITEM_COUNT}")
        if await tab.count():
            count_text = await tab.first.inner_text(timeout=QUICK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        count_text = None
    count = _CART_COUNT_RE.search(count_text or "")
    if count and int(count.group(1)) != len(links):
        return False, (
            f"the cart holds {count.group(1)} items, not the {len(links)} products of this order — "
            "nothing was ordered"
        )
    return True, ""


def split_per_link(qty: int, links: int) -> int:
    """Units of each product in one multi-link order (the form refuses a
    quantity per order that does not divide by the number of links)."""
    if links <= 1:
        return qty
    return max(1, qty // links)


async def product_has_buy_now(page: Page, *, timeout_ms: int = QUICK_TIMEOUT_MS) -> bool:
    """Whether this product offers Buy now at all (apparel often does not)."""
    try:
        text = await page.locator(f"xpath={S.BUY_NOW_BTN}").first.inner_text(
            timeout=timeout_ms
        )
    except Exception:  # noqa: BLE001 — absent on apparel and some other items
        return False
    return bool((text or "").strip())


async def click_rnw(
    page: Page, xpath: str, *, timeout_ms: int = STEP_TIMEOUT_MS, force_js: bool = False
) -> bool:
    """Click an element, with a DOM-click fallback for React-Native-Web.

    Flipkart's product and checkout controls are RN-Web <div>s. A real mouse
    click lands on the text leaf and RN-Web's responder system does not always
    pick it up — observed live: Playwright reported a successful click on
    "Buy now" and the page simply never changed. Dispatching the DOM click the
    way a script would does fire the handler, so that is the fallback.
    """
    locator = page.locator(f"xpath={xpath}").first

    # ONE activation only. Firing both a native and a JS click toggles anything
    # that opens on click: the address picker opened and shut again in the same
    # breath, leaving only the checkout's own rows to search.
    if not force_js:
        try:
            await locator.click(timeout=timeout_ms)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("native click failed on %s: %s", xpath[:50], exc)

    try:
        await locator.evaluate("el => el.click()")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("could not click %s: %s", xpath[:50], exc)
        return False


async def click_until_navigated(
    page: Page,
    xpath: str,
    url_fragment: str,
    *,
    attempts: int = 4,
    settle_ms: int = 2_000,
    also: tuple[str, ...] = (),
    dismiss_xpath: str | None = None,
) -> bool:
    """Click an RN-Web control and wait for the navigation it should cause.

    React-Native-Web attaches its handlers AFTER the initial DOM load, so a
    click fired too early lands on an element that has no handler yet: the
    click "succeeds", nothing happens, and every later step then runs against
    the wrong page. Observed live — Buy now reported a successful click and the
    URL never changed. Waiting once is not enough either, because hydration
    time varies with the page, so this retries until the URL actually moves.

    When ``dismiss_xpath`` is supplied (for example, the "SKIP & CONTINUE"
    button on Flipkart's "Frequently Bought Together" warranty sheet), an
    interstitial that appears instead of navigating is dismissed automatically.
    """
    fragments = (url_fragment, *also)
    for attempt in range(1, attempts + 1):
        # Give the page a moment to hydrate before (re)trying.
        await page.wait_for_timeout(settle_ms)
        # If an interstitial from the previous click is already open, dismiss
        # it instead of clicking the obscured trigger behind it.
        dismissed = False
        if dismiss_xpath:
            with contextlib.suppress(Exception):
                dl = page.locator(f"xpath={dismiss_xpath}").first
                if await dl.count() > 0 and await dl.is_visible():
                    log.info("dismissing interstitial (%s)", dismiss_xpath[:40])
                    await click_rnw(page, dismiss_xpath, timeout_ms=QUICK_TIMEOUT_MS, force_js=(attempt > 1))
                    dismissed = True
        if not dismissed:
            # A native click is often a silent no-op before RN-Web has hydrated,
            # so later attempts escalate to the DOM click rather than doing both
            # at once (which would double-activate a toggle).
            await click_rnw(
                page, xpath, timeout_ms=QUICK_TIMEOUT_MS, force_js=(attempt > 1)
            )
            if dismiss_xpath:
                with contextlib.suppress(Exception):
                    dl = page.locator(f"xpath={dismiss_xpath}").first
                    await dl.wait_for(state="visible", timeout=1_500)
                    log.info("dismissing interstitial after click (%s)", dismiss_xpath[:40])
                    await click_rnw(page, dismiss_xpath, timeout_ms=QUICK_TIMEOUT_MS)
        try:
            if also:
                await page.wait_for_url(lambda url: any(f in url for f in fragments), timeout=STEP_TIMEOUT_MS)
            else:
                await page.wait_for_url(f"**{url_fragment}**", timeout=STEP_TIMEOUT_MS)
            return True
        except Exception:  # noqa: BLE001 — not there yet
            if any(f in (page.url or "") for f in fragments):
                return True
            log.info(
                "%s did not navigate yet (attempt %d/%d)", xpath[:40], attempt, attempts
            )
    return False


async def on_checkout(page: Page) -> bool:
    """Whether we actually reached the checkout page."""
    url = page.url or ""
    return "/viewcheckout" in url or "/checkout" in url


#: How long the checkout gets to draw its "Deliver to:" line before the
#: address is read. Read too early, the line is not there yet and a correct
#: address looks wrong, which opened the picker for nothing (≈25 s an order,
#: measured 15 Nov 2026).
DELIVER_TO_WAIT_MS = 5_000
DELIVER_TO_POLL_MS = 250


async def current_delivery_address(page: Page, *, wait_ms: int = DELIVER_TO_WAIT_MS) -> str:
    """The address the checkout will actually ship to, as shown on the page."""
    marker = "Deliver to:"
    waited = 0
    flat = ""
    while True:
        try:
            body = await page.inner_text("body")
        except Exception:  # noqa: BLE001
            body = ""
        flat = " ".join((body or "").split())
        if marker in flat or waited >= wait_ms:
            break
        await page.wait_for_timeout(DELIVER_TO_POLL_MS)
        waited += DELIVER_TO_POLL_MS
    if marker not in flat:
        return ""
    after = flat.split(marker, 1)[1]
    if after.lstrip().startswith("Change"):
        after = after.lstrip()[len("Change"):]
    return after.strip()[:160]


async def select_address(page: Page, needle: str) -> bool:
    """Pick the saved address matching ``needle`` (e.g. a city or pincode).

    Flipkart pre-selects one address per account and it is often the wrong one
    — an account here held 24 addresses with Hardoi selected while the order
    was meant for Lucknow. Shipping to the wrong place SUCCEEDS silently, so
    this verifies afterwards rather than trusting the click.
    """
    wanted = (needle or "").strip().lower()
    if not wanted:
        return True

    before = await current_delivery_address(page)
    if wanted in before.lower():
        log.info("delivery address already matches %r", needle)
        return True

    if not await click_rnw(page, S.CHECKOUT_CHANGE_ADDRESS, timeout_ms=QUICK_TIMEOUT_MS):
        log.error("could not open the address picker")
        return False

    # The picker fills in asynchronously — an account with 24 saved addresses
    # showed 7 rows at 2.5s and 56 once settled. A fixed sleep therefore reads
    # a half-rendered list and concludes the address is not there, so poll
    # until the wanted row appears instead.
    deadline = 20
    waited = 0
    while waited < deadline:
        await page.wait_for_timeout(2_000)
        waited += 2

        rows = page.locator(f"xpath={S.ADDRESS_PICKER_ROW}")
        try:
            count = await rows.count()
        except Exception:  # noqa: BLE001
            count = 0

        for index in range(count):
            row = rows.nth(index)
            try:
                text = (await row.inner_text(timeout=1_000)).strip()
            except Exception:  # noqa: BLE001
                continue
            if wanted not in text.lower():
                continue

            log.info("matched address row: %s", text[:80])
            try:
                await row.evaluate("el => el.click()")
            except Exception as exc:  # noqa: BLE001
                log.warning("could not click the address row: %s", exc)
                continue
            await page.wait_for_timeout(3_000)

            after = await current_delivery_address(page)
            if wanted in after.lower():
                log.info("delivery address set to: %s", after[:90])
                return True
            log.warning("address clicked but the page still shows: %s", after[:90])

        log.info("address picker: %d rows after %ds, no match yet", count, waited)

    log.error(
        "no saved address matched %r — the order would ship to %r",
        needle,
        before[:80],
    )
    return False


# --- Choosing the seller (mapped live 26 Oct 2026, signed out, read only) -------
#: The chosen row's BUY NOW was pressed and the checkout is open.
SELLER_ROUTE_SELLERS_PAGE = "sellers page"
#: The product has the chosen seller alone: it is bought from its product page
#: exactly as an order without a choice is.
SELLER_ROUTE_PRODUCT_PAGE = "product page"
#: A chosen seller whose row offers only ADD TO CART is refused. Pressing a
#: sellers-page ADD TO CART was never seen live (the page was mapped read
#: only): whether it turns into "Go to cart" — the proof the cart path waits
#: for — is unknown, and an apparel size changes the product id the sellers
#: page is opened for. The product page's own cart path stays for a product
#: with one seller, where nothing about it changes.
SELLER_NEEDS_BUY_NOW = "choosing a seller works only for products with Buy now for now"


@dataclass
class SellerChoice:
    """How an order with a chosen seller goes on — or why it does not."""

    #: SELLER_ROUTE_SELLERS_PAGE or SELLER_ROUTE_PRODUCT_PAGE; blank when refused.
    route: str = ""
    #: The seller as the page spells it ("SAPHEDretail" for a typed
    #: "saphedretail"), the spelling the read-back will write later too.
    name: str | None = None
    #: Why the order cannot go on, in the operator's words. Nothing was charged;
    #: every refusal but a BUY NOW that did not open the checkout is made
    #: before anything is pressed.
    why_not: str | None = None


async def choose_seller(page: Page, *, product_link: str, seller: str) -> SellerChoice:
    """Make this order buy from ``seller`` rather than the product page's default.

    Read on the open product page first. With no "See other sellers" link the
    product has one seller, whose "Seller: X" must be the chosen one; it is
    then bought as an order without a choice is. Otherwise the sellers page is
    opened and the chosen seller's row found by its name, and that row's BUY
    NOW is pressed — only when it matches exactly once, inside a row holding
    no other listed seller's name, and the row's own text reads as that one
    seller's row. A seller the page does not list is refused before anything
    is pressed, naming the sellers it does list; so is a list that cannot be
    read.
    """
    untouched = "nothing was clicked, nothing was charged"
    on_page = await read_product_sellers(page)
    if not on_page.others_href:
        if on_page.default is None:
            return SellerChoice(
                why_not=f"the product page names no seller, so it cannot be checked against the chosen seller "
                f"{seller!r} — {untouched}"
            )
        if not same_seller(on_page.default, seller):
            return SellerChoice(
                why_not=f"this product is sold by {on_page.default!r} alone (its page lists no other sellers), "
                f"not by the chosen {seller!r} — {untouched}"
            )
        log.info("%s is the product's only seller; buying from its product page", on_page.default)
        return SellerChoice(route=SELLER_ROUTE_PRODUCT_PAGE, name=on_page.default)

    pid = choose_pid(link=product_link, landed=page.url, others_href=on_page.others_href)
    if not pid:
        return SellerChoice(
            why_not=f"the product page's link to its other sellers carries no product id, so its sellers page "
            f"cannot be opened — {untouched}"
        )
    try:
        offers = await read_sellers_page(page, pid)
    except Exception as exc:  # noqa: BLE001 — a page that will not open is a reason, not a crash
        return SellerChoice(
            why_not=f"could not open this product's sellers page ({exc.__class__.__name__}) — {untouched}"
        )
    if not offers:
        # The list is what tells the chosen row from the rest: without the
        # other sellers' names, the deepest element around the name holding
        # a BUY NOW could be the whole list, and its BUY NOW another seller's.
        return SellerChoice(why_not=f"could not read the list of sellers on the sellers page — {untouched}")
    listed = [offer.name for offer in offers]
    matching = [offer for offer in offers if same_seller(offer.name, seller)]
    if not matching:
        return SellerChoice(
            why_not=f"{seller!r} is not among the sellers Flipkart lists for this product ({', '.join(listed)}) — "
            f"{untouched}"
        )
    # Two readings must agree before anything is pressed: the page's text
    # (the list) and its elements (the name, once).
    found_names = await count_matches(page, seller_name_xpath(seller))
    if found_names == 0:
        return SellerChoice(
            why_not=f"the sellers page lists {matching[0].name!r}, but its row could not be found on the page — "
            f"{untouched}"
        )
    if found_names > 1 or len(matching) > 1:
        return SellerChoice(
            why_not=f"{seller!r} is listed more than once on the sellers page; not guessing between the rows — "
            f"{untouched}"
        )

    name = matching[0].name
    if matching[0].buy_now is False:
        return SellerChoice(
            why_not=f"{SELLER_NEEDS_BUY_NOW}: {name}'s row on the sellers page offers no BUY NOW — {untouched}"
        )
    buy_now_xpath = seller_buy_now_xpath(seller, listed)
    buttons = await count_matches(page, buy_now_xpath)
    if buttons == 0:
        if await count_matches(page, seller_add_to_cart_xpath(seller, listed)):
            return SellerChoice(
                why_not=f"{SELLER_NEEDS_BUY_NOW}: {name}'s row on the sellers page offers only Add to cart — "
                f"{untouched}"
            )
        return SellerChoice(why_not=f"no BUY NOW was found in {name}'s row on the sellers page — {untouched}")
    if buttons != 1:
        # Guessing between candidates is not acceptable on a control that
        # chooses who is paid; refuse while nothing has been pressed.
        return SellerChoice(
            why_not=f"expected one BUY NOW in {name}'s row on the sellers page, found {buttons} — {untouched}"
        )
    # And the row itself must read as that one seller's row, whatever the
    # list said: a seller the list missed would otherwise go unseen in it.
    problem = row_problem(await element_text(page, seller_row_xpath(seller, listed)), name)
    if problem:
        return SellerChoice(
            why_not=f"{name}'s row on the sellers page does not read as one seller's row ({problem}) — {untouched}"
        )

    log.info("buying from %s: pressing BUY NOW in its row on the sellers page", name)
    if not await click_until_navigated(page, buy_now_xpath, "/viewcheckout"):
        # BUY NOW only opens a checkout; nothing is ordered before the payment
        # step, so stopping here charges nothing. Say accurately where it went:
        # a seller's BUY NOW sometimes opens Flipkart's older step-by-step
        # checkout (/checkout/init: Login, a Delivery Address radio list with
        # "DELIVER HERE", an Order Summary, then payment) instead of the
        # one-page /viewcheckout. The flow does not place through that layout
        # yet (seen live 27 Oct 2026), so it is refused rather than half-walked.
        url = page.url or ""
        if "/checkout" in url:
            return SellerChoice(
                why_not=(
                    f"{name}'s BUY NOW opened Flipkart's step-by-step checkout, which this tool does not "
                    "place orders through yet — order from the default seller (leave Seller blank), or "
                    "place this one by hand. Nothing was charged."
                )
            )
        return SellerChoice(
            why_not=f"BUY NOW in {name}'s row did not respond (the browser is still on "
            f"{url[:80]}) — nothing was charged"
        )
    return SellerChoice(route=SELLER_ROUTE_SELLERS_PAGE, name=name)


async def sellers_on_checkout(page: Page) -> list[str]:
    """The sellers the checkout names ("Seller: X", "Sold by X" anywhere in a
    line), or none when it names none or cannot be read."""
    try:
        text = await page.locator(f"xpath={S.PAGE_BODY}").first.inner_text(timeout=QUICK_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 — an unreadable page is "none named"
        return []
    return sellers_named(text, sold_by_anywhere=True)


# --- Orchestrator ------------------------------------------------------------
async def run_order(
    page: Page,
    *,
    product_link: str,
    address: AddressInfo,
    gst: GstInfo,
    card: CardInfo | None = None,
    qty: int = 1,
    target: float | None = None,
    payment_mode: str = PAYMENT_CARD,
    cart_amount_limit: float | None = None,
    max_delivery_days: int | None = None,
    manage_address: bool = False,
    size: str | None = None,
    #: Pick the saved address matching this text (city, pincode or name).
    #: Without it the order ships wherever Flipkart happens to have selected.
    address_match: str | None = None,
    #: Called with the order's details just before it is sent, so a record of
    #: it exists even if nothing after the send ever runs (a crash, Ctrl+C, a
    #: cancelled job). Whatever it returns comes back as ``result["reservation"]``
    #: so the caller can complete that same record. If it raises, nothing is
    #: sent: an order with no record is worse than no order.
    reserve: Callable[[dict], object] | None = None,
    #: "upto": an order may carry fewer units than asked when Flipkart caps the
    #: quantity per order (the behaviour before this option). "minimum": it
    #: must carry all of them, or it is refused before any payment step.
    qty_mode: str = "upto",
    #: The order must be billed under a GST the checkout itself confirms: no
    #: GST chosen, or a checkout that names none, refuses it before payment.
    gst_mandatory: bool = False,
    #: Make Flipkart deliver to ``address``: pick it from the account's saved
    #: addresses, or add it when it is not there. Refuses to order rather than
    #: ship somewhere else.
    ensure_address: bool = False,
    #: Buy from this seller (as the product's sellers page names it, in any
    #: case) rather than whichever the product page shows by default. A seller
    #: the page does not list is refused before anything is pressed. None
    #: leaves the choice to Flipkart.
    seller: str | None = None,
    #: Take the account's SuperCoins / gift card balance off the total on the
    #: payment page. The price cap is then checked against what is left to pay.
    use_supercoins: bool = False,
    use_wallet: bool = False,
    #: The gift cards for a "gv"/"gv_card" payment, in memory only.
    gift_cards: list[GiftCardInfo] | None = None,
    #: More product links bought in the SAME order ("+ Add product URL"):
    #: the cart path, with the quantity per order split evenly across links.
    extra_links: list[str] | None = None,
) -> dict:
    """Place one order with the price-target gate enforced. Returns a result
    dict that always carries ``status`` (an ``OrderStatus``) and ``amount``.

    ``payment_mode`` is ``PAYMENT_CARD`` (needs ``card``) or ``PAYMENT_COD``
    (needs no card at all). Either way the price-target gate runs last, so a
    COD collection fee that pushes the total over the target still blocks it.

    With ``seller`` the order is placed from that seller's row on the
    product's sellers page (``choose_seller``), and a checkout naming another
    seller is refused before any payment step. The result's ``seller`` is the
    chosen seller (written on the order row when it is reserved) and
    ``seller_verified`` says whether the checkout named it.

    The status says what is known about money, not just about the page:
    ``failed`` and ``over_target`` mean nothing was sent to Flipkart;
    ``placed`` means Flipkart's confirmation page named the order;
    ``unconfirmed`` means the order was submitted but never confirmed, so it
    may exist. Once the order has been submitted this never raises.
    """
    await page.goto(product_link, wait_until="domcontentloaded")
    step_mark("open product")
    # Nothing is bought or paid yet: a captcha nobody answers fails the order
    # here and its card goes back to the pool (app.automation.challenges).
    await challenges.guard(page, site=challenges.FLIPKART, step="product page")

    # Apparel offers no buy control at all until a size is chosen. A size that
    # cannot be picked stops the run here, before any buy control: going on
    # would buy whichever variant the page shows — a product page that offers
    # Buy now anyway, or a chosen seller's sellers page opened for that
    # variant's product id — and nobody asked for that one.
    if size and not await select_size(page, size):
        log.error("not ordering: size %r could not be picked on the product page", size)
        return {
            "status": OrderStatus.failed,
            "amount": None,
            "target": target,
            "payment_mode": payment_mode,
            "error": f"could not pick size {size!r} on the product page — nothing was clicked, nothing was charged",
        }

    # Record WHAT is being bought from the product page itself, now, rather
    # than from the account's order list afterwards.
    product = await read_product_preview(page, product_link)
    step_mark("product page")
    # Apply any Flipkart on-page product coupon ("Coupons · ₹500 off · Apply")
    # before heading to checkout so the discount is included in the order total.
    # The run line says what happened ("coupon ₹500 off applied 1.5s",
    # "coupon ₹500 off NOT applied", "no coupon") with that step's own time.
    coupons = await apply_product_coupons(page)
    step_mark(coupons.label or "no coupon")

    # A chosen seller is settled before any buy control is touched: a seller
    # the product's sellers page does not list is refused with nothing pressed,
    # and the cart is not emptied for a run that will not go on.
    wanted_seller = " ".join((seller or "").split()) or None
    seller_choice: SellerChoice | None = None
    if wanted_seller:
        seller_choice = await choose_seller(page, product_link=product_link, seller=wanted_seller)
        if seller_choice.why_not:
            log.error("not ordering from %s: %s", wanted_seller, seller_choice.why_not)
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": seller_choice.why_not,
            }

    links = [product_link, *(extra_links or [])]
    if len(links) > 1:
        log.info("one order of %d products: the cart path", len(links))
        await empty_cart(page)
        in_cart, why_not = await add_links_to_cart(page, links)
        if not in_cart:
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": f"{why_not} — nothing was charged",
            }
        try:
            if not await click_rnw(page, S.PLACE_ORDER_BTN):
                raise RuntimeError("Place Order did not respond")
            try:
                await page.wait_for_url("**/viewcheckout**", timeout=PAGE_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 — it may go straight to payments
                await page.wait_for_url("**/payments**", timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": f"could not place the cart order: {exc}",
            }
    elif seller_choice is not None and seller_choice.route == SELLER_ROUTE_SELLERS_PAGE:
        log.info("on the checkout from %s's row on the sellers page", seller_choice.name)
    elif await product_has_buy_now(page):
        await buy_now(page, product_link, already_open=True)
    else:
        # No Buy now — some products (clothing especially) only offer Add to
        # cart. That path checks out the WHOLE cart, so the cart is emptied
        # first; otherwise a leftover item gets bought on the same card.
        log.info("no Buy now on this product; using the add-to-cart path")
        await empty_cart(page)
        await page.goto(product_link, wait_until="domcontentloaded")
        # Picked again on the reopened page. Gone by now (sold out meanwhile),
        # Add to cart would put whichever variant the page shows in the cart.
        if size and not await select_size(page, size):
            log.error("not ordering: size %r could not be picked again after emptying the cart", size)
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": f"could not pick size {size!r} on the product page — the cart was emptied, nothing was charged",
            }
        in_cart, why_not = await add_to_cart_and_open(page)
        if not in_cart:
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": f"{why_not} — nothing was charged",
            }
        try:
            if not await click_rnw(page, S.PLACE_ORDER_BTN):
                raise RuntimeError("Place Order did not respond")
            try:
                await page.wait_for_url("**/viewcheckout**", timeout=PAGE_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 — it may go straight to payments
                await page.wait_for_url("**/payments**", timeout=PAGE_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": f"could not place the cart order: {exc}",
            }

    # Choose the destination BEFORE anything else: the delivery address
    # changes price, delivery date and whether COD is offered at all, so it
    # must be settled before any of those are read.
    if address_match:
        if not await select_address(page, address_match):
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": (
                    f"no saved address matched {address_match!r} — refusing rather "
                    "than shipping to the wrong address. Nothing was charged."
                ),
            }

    step_mark("to checkout")
    await challenges.guard(page, site=challenges.FLIPKART, step="checkout")
    if ensure_address:
        delivered, why_not = await ensure_delivery_address(page, address)
        if not delivered:
            log.error("delivery address not set: %s", why_not)
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": (
                    f"{why_not} — refusing rather than shipping to a different "
                    "address. Nothing was charged."
                ),
            }

    step_mark("delivery address")
    if await checkout_is_location_gated(page):
        log.error(
            "checkout is blocked: the delivery address has no map pin set, so "
            "Flipkart renders no payment step at all"
        )
        return {
            "status": OrderStatus.failed,
            "amount": None,
            "target": target,
            "payment_mode": payment_mode,
            "error": (
                "Flipkart is blocking checkout until this delivery address has an "
                "exact location set on the map. Open the address on Flipkart, use "
                "'Set Location' to drop the pin, save it, then run again. Nothing "
                "was charged."
            ),
        }

    # Flipkart already has a delivery address selected and only offers a
    # "Change" link, so the add-an-address form never renders. Attempting it
    # anyway cost ~40s of timeouts per order and achieved nothing, so it is off
    # unless the caller explicitly asks to manage the address.
    if manage_address:
        await fill_address(page, address)

    # Use what was actually applied: recording a quantity we failed to set
    # would misstate what was bought and what it cost. Flipkart's reason for
    # keeping it lower (a per-order cap) travels with the result.
    if len(links) > 1:
        # Each product's own line on the checkout, the same units for each.
        each = split_per_link(qty, len(links))
        lines = [await set_quantity(page, each, index=line) for line in range(len(links))]
        quantity = QuantityOutcome(
            sum(line.applied for line in lines) if all(line.applied > 0 for line in lines) else 0,
            "; ".join(line.note for line in lines if line.note) or None,
        )
    else:
        quantity = await set_quantity(page, qty)
    applied_qty = quantity.applied
    step_mark("quantity")
    if applied_qty <= 0:
        # The quantity step could not say what the checkout holds (audit F-10):
        # ordering "one" over a checkout that may carry more misstates what was
        # bought and what it cost, so the order stops here.
        log.error("order aborted — %s (not charged)", quantity.note)
        return {
            "status": OrderStatus.failed,
            "amount": None,
            "target": target,
            "payment_mode": payment_mode,
            "error": f"{quantity.note} — nothing was charged",
        }
    refusal = quantity_refusal(qty_mode, qty, quantity)
    if refusal is not None:
        # Minimum mode: fewer units than asked is not this run's order. The
        # cart is left as it is; nothing has been paid.
        log.warning("order refused — %s", refusal)
        return {
            "status": OrderStatus.failed,
            "amount": None,
            "target": target,
            "payment_mode": payment_mode,
            "error": refusal,
        }
    await fill_gst(page, gst)
    # The checkout names the GSTIN the order will be billed under. When it is
    # not the one the run chose, the order would be invoiced to another entity
    # and read correctly on no screen until the read-back — so it is refused
    # before any payment step (audit F-4, 25 Oct 2026). A checkout naming no
    # GSTIN is let through with a warning: the read-back's GST check catches
    # what the page did not say.
    wanted_gstin = (gst.gstin or "").strip().upper()
    shown_gstin = await gstin_on_checkout(page) if wanted_gstin else None
    if wanted_gstin and shown_gstin and shown_gstin != wanted_gstin:
        log.error("the checkout bills under GST %s, the run chose %s — not ordering", shown_gstin, wanted_gstin)
        return {
            "status": OrderStatus.failed,
            "amount": None,
            "target": target,
            "payment_mode": payment_mode,
            "error": (
                f"the checkout bills under GST {shown_gstin}, the run chose {wanted_gstin} — "
                "not ordering, nothing was charged"
            ),
        }
    if wanted_gstin and not shown_gstin:
        log.warning("the checkout names no GSTIN; whether the order is billed under %s is known only at the read-back", wanted_gstin)
    gst_verified = bool(wanted_gstin and shown_gstin == wanted_gstin)
    refusal = gst_refusal(gst_mandatory, wanted_gstin, shown_gstin)
    if refusal is not None:
        log.error("order refused — %s", refusal)
        return {
            "status": OrderStatus.failed,
            "amount": None,
            "target": target,
            "payment_mode": payment_mode,
            "error": refusal,
        }

    # The same for a chosen seller. A checkout naming another seller would
    # buy from the very seller the run was steered away from, and read
    # correctly on no screen until the read-back, so it is refused before any
    # payment step. A checkout naming none is let through, recorded as not
    # verified: the read-back fills the order's seller from its own page.
    chosen_seller = (seller_choice.name or wanted_seller) if seller_choice is not None else None
    seller_verified = False
    if chosen_seller:
        named = await sellers_on_checkout(page)
        others = [name for name in named if not is_named_seller(name, chosen_seller)]
        if others:
            log.error("the checkout names the seller %s, the run chose %s — not ordering", ", ".join(others), chosen_seller)
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": (
                    f"the checkout names the seller {', '.join(others)}, not the chosen {chosen_seller} — "
                    "not ordering, nothing was charged"
                ),
            }
        seller_verified = bool(named)
        if not named:
            log.warning("the checkout names no seller; whether the order is from %s is known only at the read-back", chosen_seller)

    # Delivery and cart caps are checked BEFORE the payment steps: there is no
    # reason to enter a card on an order we already know we will refuse. Both
    # fail CLOSED — an unreadable value is not permission to proceed, which is
    # the mistake the price gate made when it treated "could not read" as 0.00.
    if max_delivery_days is not None:
        days = await read_delivery_days(page)
        if days is None:
            log.error("delivery promise unreadable; refusing (cap was %s days)", max_delivery_days)
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": "could not read the delivery date — nothing was charged",
            }
        if days > max_delivery_days:
            log.info("order skipped — delivery in %d days exceeds %d", days, max_delivery_days)
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "delivery_days": days,
                "error": f"delivery is {days} days out, limit is {max_delivery_days}",
            }

    if cart_amount_limit is not None:
        cart = await read_cart_amount(page)
        if cart is None:
            log.error("cart amount unreadable; refusing (cap was %s)", cart_amount_limit)
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": "could not read the cart amount — nothing was charged",
            }
        cart_each = per_unit(cart, applied_qty)
        if cart_each > cart_amount_limit:
            log.info("order skipped — cart ₹%.2f a unit exceeds ₹%.2f", cart_each, cart_amount_limit)
            return {
                "status": OrderStatus.over_target,
                "amount": cart,
                "target": cart_amount_limit,
                "payment_mode": payment_mode,
                "error": (
                    f"cart {rupees(cart)} exceeds the cart limit {rupees(cart_amount_limit)}"
                    if applied_qty <= 1
                    else f"cart {rupees(cart_each)} a unit ({rupees(cart)} for {applied_qty}) exceeds "
                    f"the cart limit {rupees(cart_amount_limit)} a unit"
                ),
            }

    step_mark("GST & checks")
    if not await continue_to_payment(page):
        return {
            "status": OrderStatus.failed,
            "amount": None,
            "target": target,
            "payment_mode": payment_mode,
            "error": "could not reach the payment page — nothing was charged",
        }

    step_mark("to payment page")
    await challenges.guard(page, site=challenges.FLIPKART, step="payment page")
    balances_used: dict[str, str] = {}
    # What the order costs before SuperCoins or the wallet pay part of it. The
    # price cap is checked against this, not against what is left to pay: a
    # balance spent is money spent (audit of 19 Nov 2026). Read only when a
    # balance may be used; without one the two figures are the same.
    gross_amount: float | None = None
    if use_supercoins or use_wallet:
        gross_amount = await read_final_amount(page)
    supercoins_amount: float | None = None
    if use_supercoins:
        balances_used["supercoins"] = await use_balance_toggle(page, S.SUPERCOINS_ROW)
        log.info("SuperCoins: %s", balances_used["supercoins"])
        if balances_used["supercoins"] == TOGGLE_APPLIED and gross_amount is not None:
            # What the coins took off, in rupees, before the wallet takes its share.
            after_coins = await read_final_amount(page)
            if after_coins is not None and after_coins < gross_amount:
                supercoins_amount = round(gross_amount - after_coins, 2)
    if use_wallet:
        balances_used["wallet"] = await use_balance_toggle(page, S.GIFT_CARD_ROW)
        log.info("gift card / wallet balance: %s", balances_used["wallet"])
    if payment_mode in GIFT_CARD_MODES:
        gift_error = await add_gift_cards(page, gift_cards or [])
        if gift_error:
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": f"{gift_error} — nothing was charged",
            }
    method_error = await choose_payment_method(page, payment_mode)
    if method_error:
        return {
            "status": OrderStatus.failed,
            "amount": None,
            "target": target,
            "payment_mode": payment_mode,
            "error": f"{method_error} — nothing was charged",
        }

    # Cash on Delivery needs no card: whether Flipkart offers it was settled by
    # choose_payment_method above (a refusal there is the "COD unavailable"
    # failure, and never a fall-back to a card the account did not ask to use).
    if payment_mode in CARD_MODES:
        if card is None:
            return {
                "status": OrderStatus.failed,
                "amount": 0.0,
                "target": target,
                "payment_mode": payment_mode,
                "error": "no card supplied for a card payment",
            }
        card_error = await _enter_card(page, card)
        if card_error:
            # Never on to Pay over a form that is not known to hold the card:
            # Pay would charge whatever the payment page had preselected, or
            # nothing — neither is this order (audit F-1, 25 Oct 2026).
            log.error("card could not be entered: %s — nothing was charged", card_error)
            return {
                "status": OrderStatus.failed,
                "amount": None,
                "target": target,
                "payment_mode": payment_mode,
                "error": f"card could not be entered ({card_error}) — nothing was charged",
            }

    step_mark("payment method")
    final_amount = await read_final_amount(page)
    if balances_used:
        log.info("balances on this order: %s", balances_used)
    used_supercoins = balances_used.get("supercoins") in (TOGGLE_APPLIED, TOGGLE_ALREADY)
    step_mark("price check")

    # Unreadable total => refuse. We cannot promise "at or below target" for an
    # amount we never saw, and confirming anyway is how an uncapped charge
    # happens. This is a failure, not an over-target outcome.
    if final_amount is None:
        log.error("order aborted — could not read the order total (not charged)")
        return {
            "status": OrderStatus.failed,
            "amount": None,
            "target": target,
            "payment_mode": payment_mode,
            "error": "could not read the order total — nothing was charged",
        }

    left_error = gift_cards_left_to_pay(payment_mode, final_amount)
    if left_error:
        log.error("order aborted — %s", left_error)
        return {
            "status": OrderStatus.failed,
            "amount": final_amount,
            "target": target,
            "payment_mode": payment_mode,
            "error": left_error,
        }

    balance_taken = any(state == TOGGLE_APPLIED for state in balances_used.values())
    if balance_taken and gross_amount is None:
        # A balance came off the total and what the order cost before it is
        # unknown: the cap cannot be checked on the real cost, so refuse.
        log.error("order aborted — the total before SuperCoins / wallet could not be read (not charged)")
        return {
            "status": OrderStatus.failed,
            "amount": final_amount,
            "target": target,
            "payment_mode": payment_mode,
            "error": "could not read the order total before SuperCoins / wallet — nothing was charged",
        }
    # The real cost: the total before any balance paid part of it, never less
    # than what is left to pay.
    cost = max(final_amount, gross_amount) if balance_taken else final_amount
    unit_cost = per_unit(cost, applied_qty)
    if not within_target(unit_cost, target):
        log.info(
            "order skipped — ₹%.2f a unit exceeds target ₹%s (not charged)",
            unit_cost,
            f"{target:.2f}" if target is not None else "—",
        )
        return {
            "status": OrderStatus.over_target,
            "amount": final_amount,
            "unit_amount": unit_cost,
            "gross_amount": cost,
            "target": target,
            "payment_mode": payment_mode,
        }

    outcome = {
        # The gate's figure: what was authorised against the target, and for
        # COD the sum the courier collects.
        "amount": final_amount,
        "unit_amount": unit_cost,
        "gross_amount": cost,
        "target": target,
        "payment_mode": payment_mode,
        "qty": applied_qty,
        "qty_note": quantity.note,
        "pin": address.pin,
        "model": product.name if len(links) == 1 else f"{product.name} (+{len(links) - 1} more)",
        "color": product.color,
        # The product page's own classification, for unit limits when the run
        # was not told what it is buying (the CLI never says).
        "brand": product.brand,
        "category": product.category,
        # Whether the checkout itself named the GST the run chose (the
        # read-back's GST check is the other half).
        "gst_verified": gst_verified,
        # The seller the run chose (None: Flipkart's default), on the order
        # row from its reservation on; and whether the checkout named it.
        "seller": chosen_seller,
        "seller_verified": seller_verified,
        # The payment page's SuperCoins toggle was on for this order.
        "used_supercoins": used_supercoins,
        "supercoins_amount": supercoins_amount,
    }
    return await _submit_and_confirm(
        page, payment_mode=payment_mode, outcome=outcome, reserve=reserve, card=card
    )
