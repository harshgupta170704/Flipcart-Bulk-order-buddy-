"""Flipkart's "Something's not right! Please try again" orders page must not
be read as "0 orders" (seen live on 17 Nov 2026: four accounts reported done
with nothing read)."""

from __future__ import annotations

import asyncio

import pytest

from app.automation import sync
from app.core import selectors as S

ERROR_PAGE = "<html><body><div>Something's not right!</div><div>Please try again</div><div>RETRY</div></body></html>"
EMPTY_PAGE = "<html><body><div>My Orders</div></body></html>"


def _run(pages: list[str], monkeypatch):
    playwright = pytest.importorskip("playwright.async_api")
    monkeypatch.setattr(sync, "_LIST_TIMEOUT_MS", 300)
    monkeypatch.setattr(sync, "ORDER_LIST_RETRY_WAIT_MS", 10)
    served = []

    async def go():
        async with playwright.async_playwright() as pw:
            try:
                browser = await pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium cannot start here: {exc.__class__.__name__}")
            page = await browser.new_page()

            async def answer(route):
                body = pages[min(len(served), len(pages) - 1)]
                served.append(body)
                await route.fulfill(status=200, content_type="text/html", body=body)

            await page.route("**/*", answer)
            try:
                return await sync.scrape_orders(page, open_details=False)
            finally:
                await browser.close()

    return asyncio.run(go()), served


def test_the_error_page_is_retried_then_fails_the_account(monkeypatch):
    with pytest.raises(sync.OrderListUnavailable):
        _run([ERROR_PAGE], monkeypatch)


def test_an_empty_list_without_the_error_is_still_no_orders(monkeypatch):
    result, served = _run([EMPTY_PAGE], monkeypatch)
    assert result == [] and len(served) == 1


def test_the_error_page_that_clears_on_retry_is_read(monkeypatch):
    row_page = (
        "<html><body>"
        + "".join(f'<div class="row">order {n}</div>' for n in range(2))
        + "</body></html>"
    )
    monkeypatch.setattr(S, "ORDER_ROW", '//div[@class="row"]')
    monkeypatch.setattr(sync, "_scrape_row", lambda row: _fake_row(row))
    result, served = _run([ERROR_PAGE, row_page], monkeypatch)
    assert len(served) == 2 and len(result) == 2


async def _fake_row(row):
    return await row.inner_text()
