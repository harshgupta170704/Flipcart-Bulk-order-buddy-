"""The checkout's GST invoice step against a page shaped as the live checkout
was on 27 Oct 2026 (audit F-4).

Mapped on a signed-in checkout of the second account, nothing ordered (a ₹1
limit stopped each rehearsal before payment): under the item a "Use GST
Invoice" tick box; ticked, a side panel "Add new GST Details" with a GSTIN box
(maxlength 15, typed in capitals), a Business Name box (maxlength 60) and
"Confirm and Save" — React Native Web divs with no names, roles or stable
classes. The legacy tool's class-name buttons matched nothing, so the first
rehearsal ran with "fill_gst failed" and a checkout naming no GSTIN.

The pages below copy that shape and play it out in a real Chromium, offline:
every request is refused. Skipped when Chromium cannot start here.
"""

from __future__ import annotations

import asyncio

import pytest

from app.automation import ordering
from app.automation.ordering import GstInfo

GSTIN = "09AAUFT2204P1ZI"
GST = GstInfo(gstin=GSTIN, business_name="TRANSPARENT DEALS")

#: The checkout's item block and the tick box; ``panel`` is what ticking shows.
_PAGE = """<!doctype html><html><body>
<div id="item"><div>Pigeon Special 3 L Inner Lid Pressure Cooker</div><div>₹675</div>
<div>Delivery by Nov 1, Sun</div>
<div id="billed">{billed}</div>
<div class="css-g5y9jx" style="cursor:pointer" id="tick"><div class="css-146c3p1 r-1et8rh5" style="cursor:pointer">Use GST Invoice</div></div>
</div>
<div id="panel" style="display:none">{panel}</div>
<script>
  window.clicks = [];
  document.getElementById('tick').addEventListener('click', () => {{
    window.clicks.push('tick');
    {on_tick}
  }});
  window.apply = (gstin, name) => {{
    document.getElementById('panel').style.display = 'none';
    document.getElementById('billed').textContent = 'GST Invoice: ' + name + ' ' + gstin;
  }};
</script>
</body></html>"""

_ADD_FORM = """<div>Add new GST Details</div>
<div style="cursor:pointer">GSTIN</div><input maxlength="15" autocapitalize="characters" id="gstin">
<div>0/15</div>
<div style="cursor:pointer">Business Name</div><input maxlength="60" autocapitalize="sentences" id="name">
<div>0/60</div>
<div>Incorrect GSTIN details will lead to order cancellation</div>
<div style="cursor:pointer" id="confirm" onclick="window.clicks.push('confirm'); window.filled = [document.getElementById('gstin').value, document.getElementById('name').value]; window.apply(document.getElementById('gstin').value, document.getElementById('name').value)">Confirm and Save</div>"""

_SAVED_LIST = """<div>Select GST Details</div>
<div style="cursor:pointer" id="saved" onclick="window.clicks.push('saved'); this.dataset.on = '1'">TRANSPARENT DEALS 09AAUFT2204P1ZI</div>
<div style="cursor:pointer" id="confirm" onclick="window.clicks.push('confirm'); if (document.getElementById('saved').dataset.on) window.apply('09AAUFT2204P1ZI', 'TRANSPARENT DEALS')">Confirm and Save</div>"""

_SHOW_PANEL = "document.getElementById('panel').style.display = 'block';"


def _page(*, panel: str = _ADD_FORM, on_tick: str = _SHOW_PANEL, billed: str = "") -> str:
    return _PAGE.format(panel=panel, on_tick=on_tick, billed=billed)


async def _run(html: str, gst: GstInfo = GST) -> tuple[list[str], list[str] | None, str | None]:
    """fill_gst on ``html`` offline: the clicks the page saw, what its boxes
    held at Confirm, and the GSTIN the checkout then names."""
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
            await ordering.fill_gst(page, gst)
            clicks = await page.evaluate("window.clicks")
            filled = await page.evaluate("window.filled || null")
            shown = await ordering.gstin_on_checkout(page)
        finally:
            await browser.close()
    return clicks, filled, shown


@pytest.fixture(autouse=True)
def _quick(monkeypatch):
    # A page that never opens its panel should not cost the suite six seconds.
    monkeypatch.setattr(ordering, "GST_PANEL_WAIT_MS", 1_500)


def test_the_add_form_is_filled_and_confirmed_and_the_checkout_names_the_gstin():
    clicks, filled, shown = asyncio.run(_run(_page()))

    assert clicks == ["tick", "confirm"]
    assert filled == [GSTIN, "TRANSPARENT DEALS"]
    assert shown == GSTIN


def test_a_checkout_already_billed_under_the_gstin_is_left_alone():
    # Ticking again would untick it.
    clicks, filled, shown = asyncio.run(_run(_page(billed=f"GST Invoice: TRANSPARENT DEALS {GSTIN}")))

    assert clicks == []
    assert filled is None
    assert shown == GSTIN


def test_saved_details_naming_the_gstin_are_picked_not_typed():
    clicks, _filled, shown = asyncio.run(_run(_page(panel=_SAVED_LIST)))

    assert clicks == ["tick", "saved", "confirm"]
    assert shown == GSTIN


def test_a_tick_that_opens_nothing_neither_raises_nor_types_anywhere(caplog):
    clicks, filled, shown = asyncio.run(_run(_page(on_tick="")))

    assert clicks == ["tick"]
    assert filled is None and shown is None
    assert "opened no GST panel" in caplog.text


def test_a_long_business_name_is_cut_to_the_boxs_sixty_characters():
    long_name = "TRANSPARENT DEALS " * 5
    _clicks, filled, _shown = asyncio.run(_run(_page(), GstInfo(gstin=GSTIN, business_name=long_name)))

    assert filled == [GSTIN, long_name[:60]]


def test_no_gstin_means_no_click():
    clicks, filled, shown = asyncio.run(_run(_page(), GstInfo(gstin="", business_name="")))

    assert clicks == [] and filled is None and shown is None
