"""Coupon application (ported from add-coupons/Add Coupon).

Runs under mobile emulation, types the coupon on the rewards page, claims it,
and reports the outcome. The caller restores the account session first (see
``core.session_store`` + ``core.browser.browser_page(mobile=True, cookies=...)``).
"""

from __future__ import annotations

import logging

from playwright.async_api import Page

from app.core import selectors as S

log = logging.getLogger("ordering.coupons")

# Outcome constants
CLAIMED = "claimed"
ALREADY_USED = "already_used"
ERROR = "error"


async def apply_coupon(page: Page, coupon: str, *, result_wait: int = 5) -> str:
    """Apply one coupon to the currently-logged-in session. Returns an outcome."""
    await page.goto(S.REWARDS_URL)
    try:
        await page.locator(f"xpath={S.COUPON_INPUT}").fill(coupon)
        claim = page.locator(f"xpath={S.COUPON_CLAIM}")
        await claim.click()
        await claim.click()  # legacy double-click
    except Exception as exc:  # noqa: BLE001
        log.error("coupon entry failed for %r: %s", coupon, exc)
        return ERROR

    # Success message?
    try:
        text = await page.locator(f"xpath={S.COUPON_SUCCESS}").inner_text(
            timeout=result_wait * 1000
        )
        if "Claim Successful" in text:
            return CLAIMED
    except Exception:
        pass

    # Already used?
    try:
        text = await page.locator(f"xpath={S.COUPON_ALREADY_USED}").inner_text(
            timeout=result_wait * 1000
        )
        if "already been used" in text:
            return ALREADY_USED
    except Exception:
        pass

    log.error("coupon %r: no success/already-used message detected", coupon)
    return ERROR
