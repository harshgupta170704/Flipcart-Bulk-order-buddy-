"""The Buy button selector against the two shapes Flipkart served on 13 Nov 2026.

Cookers: the button reads "Buy at ₹759" with its price drawn digit by digit
(an odometer), so no node says "Buy now", and an offers line near the price
also reads "Buy at ₹759" but is not the button. Phones: "Buy now" as before.
The markup below is reduced to the structure that matters, and loaded into a
real Chromium with nothing reaching the network. Skipped without Chromium.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import selectors as S

ODOMETER = "".join(f"<div>{d}</div>" for d in "0123456789" * 3)

COOKER = f"""
<div id="price"><div>₹799</div><div><div>Buy at ₹759</div><div>Apply offers</div></div></div>
<div id="bar">
  <div><div><div>Add to cart</div></div></div>
  <div id="buy"><div>B</div><div>u</div><div>y</div><div> </div><div>a</div><div>t</div><div> </div><div>₹</div>{ODOMETER}</div>
</div>
"""

PHONE = """
<div id="price"><div>Buy at ₹25,649</div></div>
<div id="bar"><div><div>Add to cart</div></div><div><div id="buy">Buy now</div></div></div>
"""

PHONE_NO_CART = '<div id="bar"><div id="buy">Buy now</div></div>'

APPAREL = '<div id="bar"><div>Add to cart</div></div><div>Buy at ₹499 with offers</div>'


async def _matched_id(html: str) -> list[str | None]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here ({exc.__class__.__name__})")
        try:
            page = await browser.new_page(java_script_enabled=False)
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(f"<html><body>{html}</body></html>")
            found = page.locator(f"xpath={S.BUY_NOW_BTN}")
            return [await found.nth(i).evaluate("e => e.closest('[id]') && e.closest('[id]').id") for i in range(await found.count())]
        finally:
            await browser.close()


@pytest.mark.parametrize(
    "html, expected",
    [(COOKER, ["buy"]), (PHONE, ["buy"]), (PHONE_NO_CART, ["buy"])],
    ids=["cooker-odometer", "phone-buy-now", "phone-without-add-to-cart"],
)
def test_the_buy_button_is_found_and_only_the_button(html, expected):
    assert asyncio.run(_matched_id(html)) == expected


def test_an_offers_line_alone_is_never_taken_for_the_button():
    # Only Add to cart offered (apparel): nothing to press for Buy now.
    assert asyncio.run(_matched_id(APPAREL)) == []


# --- the checkout's price block, as served on 13 Nov 2026 ------------------------
PRICE_BLOCK = """
<div><div>Price Details</div>
  <div><div>Selling Price </div><div>₹2,999 </div></div>
  <div><div>Fees </div><div>₹19 </div></div>
  <div><div>Discounts </div><div>₹2,200 </div></div>
  <div><div>Total Amount </div><div>₹818 </div></div>
</div>
"""


async def _first_text(html: str, xpath: str) -> str | None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here ({exc.__class__.__name__})")
        try:
            page = await browser.new_page(java_script_enabled=False)
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(f"<html><body>{html}</body></html>")
            found = page.locator(f"xpath={xpath}")
            return (await found.first.inner_text()).strip() if await found.count() else None
        finally:
            await browser.close()


def test_the_cart_amount_is_read_from_selling_price():
    assert asyncio.run(_first_text(PRICE_BLOCK, S.CHECKOUT_CART_AMOUNT)) == "₹2,999"


def test_the_total_includes_the_fees():
    assert asyncio.run(_first_text(PRICE_BLOCK, S.ORDER_TOTAL)) == "₹818"
