"""SuperCoins and gift card: reading the balances off the account pages, and
turning the payment page's toggles on (against a stand-in page in a real
Chromium, since no account had coins to show Flipkart's own)."""

from __future__ import annotations

import asyncio

import pytest

from app.automation import ordering
from app.automation.balances import parse_gift_card, parse_supercoins
from app.core import selectors as S

# The lines as the live pages printed them on 17 Nov 2026.
SUPERCOIN_PAGE = "Harsh\nMore\n3\nCart\nSuperCoin Zone\n1,250\nStarts in\n07\nHrs\n"
GIFT_PAGE = "MY ORDERS\nPAYMENTS\nGift Cards\n₹2,500\nSaved UPI\nSaved Cards\n"


def test_balances_are_read_from_the_page_lines():
    assert parse_supercoins(SUPERCOIN_PAGE) == 1250
    assert parse_supercoins("SuperCoin Zone\n0\n") == 0
    assert parse_supercoins("SuperCoin Zone\nStarts in\n") is None
    assert parse_supercoins(None) is None
    assert parse_gift_card(GIFT_PAGE) == 2500.0
    assert parse_gift_card("Gift Cards\nGift Cards\n₹0\n") == 0.0
    assert parse_gift_card("Gift Cards\nBuy a Gift Card\n") is None


PAYMENT_PAGE = """
<html><body>
  <div id="coins"><div><span>Use 120 SuperCoins</span><span>Save ₹120</span></div>
    <input type="checkbox" id="coinbox"></div>
  <div id="gift"><span>Gift Card balance ₹500</span><div role="switch" aria-checked="true" id="giftsw"></div></div>
  <div><span>Credit / Debit / ATM Card</span></div>
</body></html>
"""


def _with_page(html: str, check):
    playwright = pytest.importorskip("playwright.async_api")

    async def go():
        async with playwright.async_playwright() as pw:
            try:
                browser = await pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium cannot start here: {exc.__class__.__name__}")
            page = await browser.new_page()
            await page.set_content(html)
            try:
                return await check(page)
            finally:
                await browser.close()

    return asyncio.run(go())


def test_supercoins_box_is_ticked_and_a_switch_already_on_is_left():
    async def check(page):
        first = await ordering.use_balance_toggle(page, S.SUPERCOINS_ROW, timeout_ms=1_000)
        ticked = await page.locator("#coinbox").is_checked()
        again = await ordering.use_balance_toggle(page, S.SUPERCOINS_ROW, timeout_ms=1_000)
        gift = await ordering.use_balance_toggle(page, S.GIFT_CARD_ROW, timeout_ms=1_000)
        return first, ticked, again, gift

    assert _with_page(PAYMENT_PAGE, check) == (
        ordering.TOGGLE_APPLIED,
        True,
        ordering.TOGGLE_ALREADY,
        ordering.TOGGLE_ALREADY,
    )


def test_a_page_without_supercoins_says_not_offered():
    async def check(page):
        return await ordering.use_balance_toggle(page, S.SUPERCOINS_ROW, timeout_ms=500)

    assert _with_page("<html><body><div>Cash on Delivery</div></body></html>", check) == ordering.TOGGLE_ABSENT
