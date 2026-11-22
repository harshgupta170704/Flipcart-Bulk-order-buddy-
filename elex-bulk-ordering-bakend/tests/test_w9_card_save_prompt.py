"""Flipkart's "Secure your card as per RBI guidelines" dialog after Pay.

The owner's card run of 27 Oct 2026 (18:35 IST) pressed "Pay ₹638" and then
stood for 90 s in front of a dialog the flow did not know: "Secure your card
as per RBI guidelines", with "Maybe later" and "Yes, secure my card" (the
run's own screenshot, card boxes painted over). Nothing went to the bank, so
the run ended unconfirmed and no order was made. The wait for the bank's page
now answers it "Maybe later" — once — and goes on waiting for the bank.

The dialog's shape is played out in a real Chromium, offline (every request
refused; skipped when Chromium cannot start here), and the wait itself on the
suite's fake page.
"""

from __future__ import annotations

import asyncio

import pytest

from app.automation import ordering
from app.core import selectors as S
from tests.fakes import FakePage

_DIALOG = """<!doctype html><html><body>
<div>Complete Payment</div><div>Card Number</div><button>Pay ₹638</button>
<div id="dialog" style="{display}">
  <div>Secure your card as per RBI guidelines</div>
  <div>Pay without entering your card details every time</div>
  <div>Enhanced security to secure your cards from frauds</div>
  {later}
  <button onclick="window.clicked.push('yes')">Yes, secure my card</button>
</div>
<script>window.clicked = [];</script>
</body></html>"""

#: "Maybe later" as a bare button, and as a button wrapping a span.
_PLAIN = """<button onclick="window.clicked.push('later'); document.getElementById('dialog').style.display='none'">Maybe later</button>"""
_WRAPPED = """<button onclick="window.clicked.push('later'); document.getElementById('dialog').style.display='none'"><span>Maybe later</span></button>"""


async def _decline_on(html: str) -> tuple[bool, list[str], bool]:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here: {exc}")
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(html)
            answered = await ordering._decline_saving_the_card(page)
            clicked = await page.evaluate("window.clicked")
            still_open = await page.locator("#dialog").is_visible()
        finally:
            await browser.close()
    return answered, clicked, still_open


@pytest.mark.parametrize("later", [_PLAIN, _WRAPPED], ids=["plain-button", "button-with-span"])
def test_the_dialog_is_answered_maybe_later_never_yes(later):
    answered, clicked, still_open = asyncio.run(_decline_on(_DIALOG.format(display="", later=later)))

    assert answered is True
    assert clicked == ["later"]  # never "Yes, secure my card": the card is not saved on the account
    assert still_open is False


def test_no_dialog_means_no_click():
    answered, clicked, _still_open = asyncio.run(_decline_on(_DIALOG.format(display="display:none", later=_PLAIN)))

    assert answered is False
    assert clicked == []


def test_the_wait_for_the_bank_answers_the_dialog_once(monkeypatch):
    monkeypatch.setattr(ordering, "BANK_PAGE_WAIT_MS", 3_000)
    page = FakePage()
    page.url = "https://www.flipkart.com/payments"
    page.counts[S.CARD_SAVE_PROMPT] = 1  # the dialog stands after Pay

    host, place = asyncio.run(ordering._bank_page(page))

    assert (host, place) == (None, None)  # no bank page in the fake
    assert page.clicks.count(S.CARD_SAVE_LATER) == 1


def test_the_wait_for_the_bank_clicks_nothing_without_the_dialog(monkeypatch):
    monkeypatch.setattr(ordering, "BANK_PAGE_WAIT_MS", 2_000)
    page = FakePage()
    page.url = "https://www.flipkart.com/payments"

    asyncio.run(ordering._bank_page(page))

    assert S.CARD_SAVE_LATER not in page.clicks
