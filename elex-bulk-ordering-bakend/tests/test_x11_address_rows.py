"""Reading whole saved-address rows from the picker (15 Nov 2026 layout).

The row selector matches two pieces of every list entry (name line, first
address line) and also pieces of the checkout outside the list. A row must be
read whole, and never merged with its neighbour or with the checkout.
"""

import asyncio

import pytest

from app.automation import ordering as O
from app.automation.ordering import AddressInfo, address_matches
from app.core import selectors as S

PIECE = 'class="css-146c3p1 r-dnmrzs"'


def _entry(name, line, rest):
    return (
        f'<div class="row"><div><div><div {PIECE}>{name}</div></div>'
        f'<div><div {PIECE}>{line}</div><div>{rest}</div></div></div></div>'
    )


PAGE = (
    # the checkout behind the picker: its own pieces match the selector too
    f'<div class="checkout"><div {PIECE}>Deliver to:</div><div {PIECE}>KIKAKU PRIVATE LIMITED</div></div>'
    '<div class="picker"><div>Select delivery address</div><div class="list">'
    + _entry("KIKAKU PRIVATE LIMITED", "RAILWAY GANJ, CIRCULAR ROAD", "Hardoi")
    + _entry("Akansha Agarwal", "D-47, Sector 61", "Noida")
    + _entry("Harsh Gupta", "251, Ashok Vihar Colony, Ashok Vihar, 80 Feet Road", "Near Rohit Traders, Bhopal")
    + "</div></div>"
)

BOOK = AddressInfo(
    name="Harsh Gupta", phone="", pin="462010",
    area="251, Ashok Vihar Colony, Ashok Vihar, 80 Feet Road, Near Rohit Traders, Bhopal",
)


async def _texts():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here ({exc.__class__.__name__})")
        try:
            page = await browser.new_page(java_script_enabled=False)
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(f"<html><body>{PAGE}</body></html>")
            return await O._all_texts(page.locator(f"xpath={S.ADDRESS_PICKER_ROW}"))
        finally:
            await browser.close()


def test_each_entry_is_read_whole_and_only_the_right_one_matches():
    texts = asyncio.run(_texts())
    matching = {" ".join(t.split()) for t in texts if address_matches(t, BOOK)}
    assert matching == {"Harsh Gupta 251, Ashok Vihar Colony, Ashok Vihar, 80 Feet Road Near Rohit Traders, Bhopal"}


def test_no_row_spills_into_its_neighbour():
    texts = asyncio.run(_texts())
    assert not any("Harsh Gupta" in t and "Noida" in t for t in texts)
    assert not any("Select delivery address" in t for t in texts)
