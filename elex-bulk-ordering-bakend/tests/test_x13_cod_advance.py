"""Some accounts get no plain Cash on Delivery: Flipkart asks for a ₹25 online
advance first (15 Nov 2026). The run stops with that reason; it never pays."""

import asyncio

import pytest

from app.automation.ordering import _cod_advance_asked

ADVANCE = """<div>Cash on Delivery</div><div>Pay ₹25 as advance and balance amount as Cash on delivery</div>
<div>Pay only ₹25 now</div><div>Place your order now by paying an advance payment of ₹25 using an online
payment option and pay the balance ₹793 at the time of delivery</div><button>Cancel</button><button>Continue</button>"""
PLAIN = """<div>Cash on Delivery</div><div>Due to handling costs, a nominal fee of ₹35 will be charged.</div>
<button>Confirm order</button>"""


async def _ask(html):
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
            return await _cod_advance_asked(page)
        finally:
            await browser.close()


def test_the_advance_popup_is_recognised_with_its_amount():
    assert asyncio.run(_ask(ADVANCE)) == "₹25"


def test_a_plain_cod_page_is_not_mistaken_for_it():
    assert asyncio.run(_ask(PLAIN)) is None
