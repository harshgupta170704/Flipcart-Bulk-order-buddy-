"""Coupon application (verified live on /myrewards?enableCouponCode=true, 2026-11-22).

Runs under mobile emulation, types the coupon on the rewards page using key
events (so React-Native-Web controlled state updates), clicks "Add Coupon",
and reports the outcome. The caller restores the account session first (see
``core.session_store`` + ``core.browser.browser_page(mobile=True, cookies=...)``).
"""

from __future__ import annotations

import asyncio
import logging
import time

from playwright.async_api import Page

from app.core import selectors as S

log = logging.getLogger("ordering.coupons")

# Outcome constants
CLAIMED = "claimed"
ALREADY_USED = "already_used"
ERROR = "error"


def _classify(text: str | None) -> str | None:
    low = (text or "").strip().lower()
    if not low:
        return None
    if "claim successful" in low or "coupon added" in low or "successfully added" in low:
        return CLAIMED
    if "already been used" in low or "already claimed" in low or "already applied" in low:
        return ALREADY_USED
    return None


async def _read_first_visible(page: Page, xpath: str, *, timeout_ms: int) -> str | None:
    loc = page.locator(f"xpath={xpath}")
    target = getattr(loc, "first", loc)
    try:
        if hasattr(loc, "count"):
            if await loc.count() == 0:
                return None
            if hasattr(target, "is_visible") and not await target.is_visible():
                return None
        text = await target.inner_text(timeout=timeout_ms)
        return (text or "").strip() or None
    except Exception:
        return None


async def apply_coupon(page: Page, coupon: str, *, result_wait: int = 5) -> str:
    """Apply one coupon to the currently-logged-in session. Returns an outcome."""
    await page.goto(S.REWARDS_URL)
    real_playwright = hasattr(page, "wait_for_timeout")
    try:
        box = page.locator(f"xpath={S.COUPON_INPUT}")
        if hasattr(box, "press_sequentially"):
            # Real Playwright locator: wait for the RNW coupon sheet to mount,
            # type key-by-key so React controlled state updates, and let it
            # settle briefly before clicking "Add Coupon".
            first_box = getattr(box, "first", box)
            await first_box.wait_for(state="visible", timeout=max(result_wait, 15) * 1000)
            await first_box.click(timeout=5000)
            await first_box.fill("")
            await first_box.press_sequentially(coupon, delay=35)
            if real_playwright:
                await page.wait_for_timeout(250)
        else:
            await box.fill(coupon)
        claim = page.locator(f"xpath={S.COUPON_CLAIM}")
        first_claim = getattr(claim, "first", claim)
        await first_claim.click()
    except Exception as exc:  # noqa: BLE001
        log.error("coupon entry failed for %r: %s", coupon, exc)
        return ERROR

    if not real_playwright:
        # FakePage unit-test path: direct single check against each selector.
        timeout_ms = result_wait * 1000
        for xpath, expected in (
            (S.COUPON_SUCCESS, CLAIMED),
            (S.COUPON_ALREADY_USED, ALREADY_USED),
        ):
            text = await _read_first_visible(page, xpath, timeout_ms=timeout_ms)
            if _classify(text) == expected:
                return expected
        invalid_text = await _read_first_visible(page, S.COUPON_INVALID, timeout_ms=1000)
        if invalid_text:
            log.warning("coupon %r rejected by Flipkart: %s", coupon, invalid_text)
            return ERROR
        log.error("coupon %r: no success/already-used message detected", coupon)
        return ERROR

    # Real browser path: poll all three messages together so we catch the
    # response quickly (and before transient toasts disappear).
    deadline = time.monotonic() + max(result_wait, 3)
    while time.monotonic() < deadline:
        text = await _read_first_visible(page, S.COUPON_SUCCESS, timeout_ms=200)
        if _classify(text) == CLAIMED:
            return CLAIMED

        text = await _read_first_visible(page, S.COUPON_ALREADY_USED, timeout_ms=200)
        if _classify(text) == ALREADY_USED:
            return ALREADY_USED

        invalid_text = await _read_first_visible(page, S.COUPON_INVALID, timeout_ms=200)
        if invalid_text:
            log.warning("coupon %r rejected by Flipkart: %s", coupon, invalid_text)
            return ERROR

        await asyncio.sleep(0.25)

    log.error("coupon %r: no success/already-used message detected", coupon)
    return ERROR
