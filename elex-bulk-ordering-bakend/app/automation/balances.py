"""Read an account's SuperCoins and gift card balance from Flipkart.

Read-only: two pages are opened and nothing on them is pressed.

- ``/account/giftcard`` — the account menu reads "Gift Cards" with the
  balance on the next line ("₹0").
- ``/supercoin`` — the SuperCoin Zone header reads "SuperCoin Zone" with the
  balance on the next line ("0").

Both were read off the live desktop pages on 17 Nov 2026. The parsers work on
the page's lines, so a change of markup that keeps the wording keeps working.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from playwright.async_api import Page

log = logging.getLogger("ordering.automation.balances")

GIFT_CARD_URL = "https://www.flipkart.com/account/giftcard"
SUPERCOIN_URL = "https://www.flipkart.com/supercoin"
#: How long a page gets to draw its header after loading.
SETTLE_MS = 3_000

_NUMBER = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*$")
_RUPEES = re.compile(r"^\s*₹\s*([\d,]+(?:\.\d+)?)\s*$")


@dataclass
class Balances:
    supercoins: int | None = None
    gift_card: float | None = None


def _after(lines: list[str], label: str, pattern: re.Pattern) -> str | None:
    """The value on the line right after ``label`` (case-insensitive), when it matches."""
    wanted = label.casefold()
    for index, line in enumerate(lines[:-1]):
        if line.strip().casefold() == wanted:
            match = pattern.match(lines[index + 1])
            if match:
                return match.group(1).replace(",", "")
    return None


def parse_supercoins(text: str | None) -> int | None:
    """The balance under "SuperCoin Zone"; None when the page does not show one."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    value = _after(lines, "SuperCoin Zone", _NUMBER)
    return int(float(value)) if value is not None else None


def parse_gift_card(text: str | None) -> float | None:
    """The balance under "Gift Cards" in the account menu; None when not shown."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    value = _after(lines, "Gift Cards", _RUPEES)
    return float(value) if value is not None else None


async def _page_text(page: Page, url: str) -> str | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(SETTLE_MS)
        return await page.locator("body").inner_text(timeout=10_000)
    except Exception as exc:  # noqa: BLE001 — a page that will not load is "not read"
        log.warning("could not read %s: %s", url, exc.__class__.__name__)
        return None


async def read_balances(page: Page) -> Balances:
    """Both balances, each None when its page could not be read."""
    return Balances(
        gift_card=parse_gift_card(await _page_text(page, GIFT_CARD_URL)),
        supercoins=parse_supercoins(await _page_text(page, SUPERCOIN_URL)),
    )
