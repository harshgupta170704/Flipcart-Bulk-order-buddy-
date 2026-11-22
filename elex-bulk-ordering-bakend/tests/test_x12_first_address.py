"""An account with no saved address: Buy now opens Flipkart's first-address
form (15 Nov 2026), which is filled from the Address Book entry."""

import asyncio

import pytest

from app.automation.ordering import AddressInfo, fill_first_address_fields, on_first_address_form, split_street

FORM = """
<form action="#" onsubmit="return false">
  <input name="name" maxlength="300"><input name="phone" value="9120726650">
  <input name="alternatePhone"><input name="pincode" maxlength="6">
  <input name="city"><select name="state"><option value="">--</option><option>Madhya Pradesh</option><option>Uttar Pradesh</option></select>
  <input name="addressLine1"><input name="addressLine2"><input name="landmark">
  <input type="radio" name="locationTypeTag" value="HOME"><input type="radio" name="locationTypeTag" value="WORK">
  <input type="submit" value="Save Address">
</form>
"""

BOOK = AddressInfo(
    name="Harsh Gupta", phone="", pin="462010",
    area="251, Ashok Vihar Colony, Ashok Vihar, 80 Feet Road, Bhopal, Madhya Pradesh",
    line1="251, Ashok Vihar Colony, Ashok Vihar, 80 Feet Road", city="Bhopal", state="Madhya Pradesh",
)


def test_the_street_line_splits_into_house_and_area():
    assert split_street("251, Ashok Vihar Colony, Ashok Vihar, 80 Feet Road") == ("251, Ashok Vihar Colony", "Ashok Vihar, 80 Feet Road")
    assert split_street("D-47, Sector 61") == ("D-47", "Sector 61")
    assert split_street("") == ("", "")


async def _fill():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here ({exc.__class__.__name__})")
        try:
            page = await browser.new_page()
            # Served locally at the form's real address: nothing reaches Flipkart.
            await page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=f"<html><body>{FORM}</body></html>"))
            await page.goto("https://www.flipkart.com/rv/checkout/changeShippingAddress/add/form?checkoutInitiated=true")
            on_form = await on_first_address_form(page)
            await fill_first_address_fields(page, BOOK)
            values = await page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('[name]')]
                .filter(e => e.type !== 'radio').map(e => [e.name, e.value]))""")
            home = await page.evaluate("() => document.querySelector('input[value=HOME]').checked")
            return on_form, values, home
        finally:
            await browser.close()


READONLY_STATE_FORM = FORM.replace(
    '<select name="state"><option value="">--</option><option>Madhya Pradesh</option><option>Uttar Pradesh</option></select>',
    '<input name="state" readonly>',
)


async def _fill_readonly_state():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here ({exc.__class__.__name__})")
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=f"<html><body>{READONLY_STATE_FORM}</body></html>"))
            await page.goto("https://www.flipkart.com/rv/checkout/changeShippingAddress/add/form")
            await asyncio.wait_for(fill_first_address_fields(page, BOOK), timeout=20)
            return await page.evaluate("() => [document.querySelector('[name=state]').value, document.querySelector('[name=addressLine2]').value]")
        finally:
            await browser.close()


def test_a_read_only_state_is_left_to_flipkart_and_the_rest_is_filled():
    state, area = asyncio.run(_fill_readonly_state())
    assert state == ""  # Flipkart fills it from the pincode
    assert area == "Ashok Vihar, 80 Feet Road"


def test_the_form_is_recognised_and_filled_from_the_address_book():
    on_form, values, home = asyncio.run(_fill())
    assert on_form is True
    assert values["name"] == "Harsh Gupta"
    assert values["phone"] == "9120726650"  # no phone on the entry: the account's own stays
    assert values["pincode"] == "462010"
    assert values["city"] == "Bhopal" and values["state"] == "Madhya Pradesh"
    assert values["addressLine1"] == "251, Ashok Vihar Colony"
    assert values["addressLine2"] == "Ashok Vihar, 80 Feet Road"
    assert home is True
