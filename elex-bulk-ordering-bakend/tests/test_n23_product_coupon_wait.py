"""The product-page coupon step against pages that wake up the way Flipkart's
did on 23 Nov 2026 (vivo T5 Lite, signed-in catbar accounts).

Live, the coupon tile ("Coupons · ₹500 off · Apply") arrives as server-drawn
HTML; Flipkart's code wakes it 1–4 s later and redraws it. A press before that
does nothing, and the redraw removed the pressed element — which the old step
counted as "applied". Every order then reached checkout ₹500 over target
(jobs 669 and 671: ₹16,924 against ₹16,419).

The pages below act that out in a real Chromium, offline: "waking up" puts a
``__reactFiber`` key on the nodes, as React does. Skipped when Chromium cannot
start here.
"""

from __future__ import annotations

import asyncio

import pytest

from app.automation import ordering

#: A tile drawn by the server: dead until window.wake() redraws it live.
_LATE_WAKE = """<!doctype html><html><body>
<div id="header">Flipkart</div>
<div id="offers">
  <div id="coupon-tile"><div>Coupons</div><div>₹500 off</div><div>Save ₹500 extra on 1 item(s)</div><div style="cursor:pointer">Apply</div></div>
  <div id="bank-tile"><div>Bank offers</div><div>₹2,300 off</div><div>Flipkart Axis Credit Card</div><div id="b-apply" style="cursor:pointer">Apply</div></div>
</div>
<script>
  window.clicked = [];
  window.deadPresses = 0;
  // The page's own code is there from the start (the header is live) ...
  document.getElementById('header')['__reactFiber$test'] = {};
  document.getElementById('b-apply').addEventListener('click', () => window.clicked.push('bank'));
  // ... but the coupon tile only wakes up later, redrawn as a new element.
  document.querySelector('#coupon-tile div:last-child').addEventListener('click', () => window.deadPresses++);
  window.wake = (mode) => {
    const tile = document.getElementById('coupon-tile');
    tile.innerHTML = '<div>Coupons</div><div>Ends in 01d 22m 05s</div><div>₹500 off</div><div>Save ₹500 extra on 1 item(s)</div><div id="c-btn" style="cursor:pointer">Apply</div>';
    const btn = document.getElementById('c-btn');
    btn['__reactFiber$test'] = {};
    btn.addEventListener('click', (e) => {
      if (!e.isTrusted) return;
      window.clicked.push('coupon');
      if (mode === 'refused') {
        // Flipkart is asked and answers, but the coupon is not for this account.
        fetch('https://2.rome.api.flipkart.com/api/1/action/view', {method: 'POST'}).catch(() => {});
        return;
      }
      if (mode === 'redraw-only') {
        // The tile is redrawn but the coupon is not taken: still "Apply".
        window.wake('redraw-only');
        return;
      }
      setTimeout(() => { btn.textContent = 'Remove'; }, 150);
    });
  };
</script>
</body></html>"""


async def _with_page(html: str, body, *, answer_actions: bool = False):
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here: {exc}")
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            if answer_actions:
                await page.route(
                    "**/api/1/action/view",
                    lambda route: route.fulfill(
                        status=200,
                        body="{}",
                        headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"},
                    ),
                )
            await page.set_content(html)
            return await body(page)
        finally:
            await browser.close()


def test_waits_for_the_coupon_to_wake_up_before_pressing():
    async def body(page):
        await page.evaluate("setTimeout(() => window.wake(), 1200)")
        result = await ordering.apply_product_coupons(page)
        state = await page.evaluate("document.getElementById('c-btn').textContent")
        return result, await page.evaluate("window.clicked"), await page.evaluate("window.deadPresses"), state

    result, clicked, dead, state = asyncio.run(_with_page(_LATE_WAKE, body))
    assert dead == 0, "pressed the coupon before the page had woken it up"
    assert clicked == ["coupon"], "pressed once, and never the bank offer"
    assert state == "Remove"
    assert result.applied == ["₹500 off"] and not result.failed
    assert result.label == "coupon ₹500 off applied"


def test_a_coupon_that_never_wakes_up_is_reported_not_applied():
    async def body(page):
        return await ordering.apply_product_coupons(page, ready_timeout_ms=1_500)

    result = asyncio.run(_with_page(_LATE_WAKE, body))
    assert result.applied == []
    assert result.failed == ["₹500 off"]
    assert result.label == "coupon ₹500 off NOT applied"


def test_a_redraw_without_remove_is_not_counted_as_applied(monkeypatch):
    # The old step's mistake: the pressed element vanished, so it said "applied".
    monkeypatch.setattr(ordering, "COUPON_CONFIRM_TIMEOUT_MS", 400)
    monkeypatch.setattr(ordering, "COUPON_SLOW_ANSWER_MS", 0)

    async def body(page):
        await page.evaluate("window.wake('redraw-only')")
        result = await ordering.apply_product_coupons(page)
        return result, await page.evaluate("window.clicked")

    result, clicked = asyncio.run(_with_page(_LATE_WAKE, body))
    assert result.applied == []
    assert result.failed == ["₹500 off"]
    assert clicked == ["coupon"] * ordering.COUPON_CLICK_ATTEMPTS


def test_a_coupon_already_on_is_left_alone():
    html = _LATE_WAKE.replace(
        "</script>",
        "window.wake(); document.getElementById('c-btn').textContent = 'Remove';</script>",
    )

    async def body(page):
        result = await ordering.apply_product_coupons(page)
        return result, await page.evaluate("window.clicked")

    result, clicked = asyncio.run(_with_page(html, body))
    assert clicked == [], "pressing 'Remove' would take the coupon off"
    assert result.already == ["₹500 off"] and result.applied == []
    assert result.label == "coupon ₹500 off applied"


def test_a_page_without_coupons_returns_quickly():
    html = "<!doctype html><html><body><div id='h'>Flipkart</div><div>Bank offers</div><div>Apply</div><script>document.getElementById('h')['__reactFiber$x']={};</script></body></html>"

    async def body(page):
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await ordering.apply_product_coupons(page)
        return result, loop.time() - started

    result, seconds = asyncio.run(_with_page(html, body))
    assert result.label is None
    assert seconds < 4


def test_a_coupon_flipkart_says_no_to_is_pressed_once(monkeypatch):
    # Pressing again does not change Flipkart's mind; it only costs time.
    monkeypatch.setattr(ordering, "COUPON_AFTER_ANSWER_MS", 300)

    async def body(page):
        await page.evaluate("window.wake('refused')")
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await ordering.apply_product_coupons(page)
        return result, await page.evaluate("window.clicked"), loop.time() - started

    result, clicked, seconds = asyncio.run(_with_page(_LATE_WAKE, body, answer_actions=True))
    assert clicked == ["coupon"]
    assert result.failed == ["₹500 off"] and result.applied == []
    assert result.label == "coupon ₹500 off NOT applied"
    assert seconds < 3


def test_no_press_starts_after_the_time_limit(monkeypatch):
    # A live button whose presses never reach Flipkart: the step stops at the
    # time limit plus one press's wait, not after every allowed press.
    monkeypatch.setattr(ordering, "COUPON_CONFIRM_TIMEOUT_MS", 800)

    async def body(page):
        await page.evaluate("window.wake('redraw-only')")
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await ordering.apply_product_coupons(page, ready_timeout_ms=300)
        return result, await page.evaluate("window.clicked"), loop.time() - started

    result, clicked, seconds = asyncio.run(_with_page(_LATE_WAKE, body))
    assert clicked == ["coupon"]
    assert result.failed == ["₹500 off"]
    assert seconds < 3


def test_the_run_line_names_what_went_on_and_what_did_not():
    both = ordering.CouponResult(applied=["₹500 off"], failed=["₹100 off"])
    assert both.label == "coupon ₹500 off applied; ₹100 off NOT applied"
    assert ordering.CouponResult(already=["₹500 off"]).label == "coupon ₹500 off applied"
    assert ordering.CouponResult().label is None


#: Flipkart marks its page root the moment it STARTS waking the page; the coupon
#: tile here is drawn only when the page has woken, together with Buy now.
_LATE_BUY = """<!doctype html><html><body>
<div id="container">
  <div id="header">Flipkart</div>
  <div id="offers"></div>
  <div id="buy" style="cursor:pointer">Buy now</div>
</div>
<script>
  window.clicked = [];
  document.getElementById('container')['__reactContainer$test'] = {};
  window.wake = () => {
    document.getElementById('buy')['__reactFiber$test'] = {};
    document.getElementById('offers').innerHTML =
      '<div id="coupon-tile"><div>Coupons</div><div>₹300 off</div><div id="c-btn" style="cursor:pointer">Apply</div></div>';
    const btn = document.getElementById('c-btn');
    btn['__reactFiber$test'] = {};
    btn.addEventListener('click', (e) => {
      if (!e.isTrusted) return;
      window.clicked.push('coupon');
      setTimeout(() => { btn.textContent = 'Remove'; }, 100);
    });
  };
</script>
</body></html>"""


def test_no_coupon_is_decided_only_once_buy_now_is_live():
    # Deciding "no coupon" when waking merely STARTED would miss this tile.
    async def body(page):
        await page.evaluate("setTimeout(() => window.wake(), 2500)")
        result = await ordering.apply_product_coupons(page)
        return result, await page.evaluate("window.clicked")

    result, clicked = asyncio.run(_with_page(_LATE_BUY, body))
    assert clicked == ["coupon"]
    assert result.applied == ["₹300 off"]


def test_a_buy_now_that_never_wakes_holds_the_no_coupon_decision_only_briefly(monkeypatch):
    monkeypatch.setattr(ordering, "COUPON_BUY_WAIT_MS", 1_500)

    async def body(page):
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await ordering.apply_product_coupons(page)
        return result, loop.time() - started

    result, seconds = asyncio.run(_with_page(_LATE_BUY, body))
    assert result.label is None
    assert 1.5 <= seconds < 6


# --- Review of 24 Nov 2026: tiles that answer through Flipkart's action address.
#: ``liveTile(id, mode)`` wakes one tile's button. Pressed, it POSTs to the
#: action address; in 'accept' mode it reads Remove when the answer is OK.
_TILE_HELPERS = """<script>
  window.clicked = [];
  window.liveTile = (id, mode) => {
    const btn = document.getElementById(id);
    btn['__reactFiber$t'] = {};
    btn.addEventListener('click', async (e) => {
      if (!e.isTrusted) return;
      window.clicked.push(id);
      const r = await fetch('https://2.rome.api.flipkart.com/api/1/action/view', {method: 'POST'}).catch(() => null);
      if (r && r.ok && mode === 'accept') btn.textContent = 'Remove';
    });
  };
</script>"""


async def _with_actions(html: str, body, statuses=()):
    """``html`` in a real Chromium, offline; the action address answers with
    ``statuses`` in turn, then 200."""
    from playwright.async_api import async_playwright

    answers = list(statuses)

    async def answer(route):
        status = answers.pop(0) if answers else 200
        await route.fulfill(
            status=status, body="{}",
            headers={"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"},
        )

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here: {exc}")
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.route("**/api/1/action/view", answer)
            await page.set_content(html)
            return await body(page)
        finally:
            await browser.close()


async def _apply_and_clicks(page, **kwargs):
    result = await ordering.apply_product_coupons(page, **kwargs)
    return result, await page.evaluate("window.clicked")


def test_a_refused_percent_coupon_with_a_ticking_countdown_is_pressed_once(monkeypatch):
    # Its name must not carry the countdown, or "never press a no again" fails.
    monkeypatch.setattr(ordering, "COUPON_AFTER_ANSWER_MS", 300)
    html = _TILE_HELPERS + """<div id="h">Flipkart</div>
    <div id="t1"><div>Coupons</div><div id="cd">Ends in 00d 10m 05s</div><div>Extra 10% off up to ₹1,000</div><div id="c1" style="cursor:pointer">Apply</div></div>
    <script>
      document.getElementById('h')['__reactFiber$t'] = {};
      liveTile('c1', 'refuse');
      let s = 5;
      setInterval(() => { s = (s + 1) % 60; document.getElementById('cd').textContent = 'Ends in 00d 10m ' + String(s).padStart(2, '0') + 's'; }, 200);
    </script>"""

    result, clicked = asyncio.run(_with_actions(html, _apply_and_clicks))
    assert clicked == ["c1"]
    assert result.failed == ["10% off"]
    assert result.label == "coupon 10% off NOT applied"


def test_two_tiles_with_the_same_amount_are_both_tried(monkeypatch):
    monkeypatch.setattr(ordering, "COUPON_AFTER_ANSWER_MS", 300)
    html = _TILE_HELPERS + """<div id="h">Flipkart</div>
    <div id="t1"><div>Coupons</div><div>₹500 off</div><div id="c1" style="cursor:pointer">Apply</div></div>
    <div id="t2"><div>Coupons</div><div>₹500 off</div><div id="c2" style="cursor:pointer">Apply</div></div>
    <script>document.getElementById('h')['__reactFiber$t'] = {}; liveTile('c1', 'refuse'); liveTile('c2', 'accept');</script>"""

    result, clicked = asyncio.run(_with_actions(html, _apply_and_clicks))
    assert clicked == ["c1", "c2"]
    assert result.applied == ["₹500 off"]


def test_a_tile_that_vanishes_while_redrawn_is_still_applied():
    # Seen once, a coupon is never taken for "no coupon" because it blinked.
    html = _TILE_HELPERS + """<div id="h">Flipkart</div>
    <div id="offers"><div id="t1"><div>Coupons</div><div>₹500 off</div><div id="c1" style="cursor:pointer">Apply</div></div></div>
    <script>
      document.getElementById('h')['__reactFiber$t'] = {};
      setTimeout(() => { document.getElementById('offers').innerHTML = ''; }, 300);
      setTimeout(() => {
        document.getElementById('offers').innerHTML = '<div id="t1"><div>Coupons</div><div>₹500 off</div><div id="c1" style="cursor:pointer">Apply</div></div>';
        liveTile('c1', 'accept');
      }, 1800);
    </script>"""

    result, clicked = asyncio.run(_with_actions(html, _apply_and_clicks))
    assert clicked == ["c1"]
    assert result.applied == ["₹500 off"]


def test_a_tile_that_vanishes_for_good_is_reported_not_applied():
    html = _TILE_HELPERS + """<div id="h">Flipkart</div>
    <div id="offers"><div id="t1"><div>Coupons</div><div>₹500 off</div><div id="c1" style="cursor:pointer">Apply</div></div></div>
    <script>
      document.getElementById('h')['__reactFiber$t'] = {};
      setTimeout(() => { document.getElementById('offers').innerHTML = ''; }, 300);
    </script>"""

    async def body(page):
        return await _apply_and_clicks(page, ready_timeout_ms=1_500)

    result, clicked = asyncio.run(_with_actions(html, body))
    assert clicked == []
    assert result.label == "coupon ₹500 off NOT applied"


def test_a_busy_answer_from_flipkart_is_pressed_again(monkeypatch):
    monkeypatch.setattr(ordering, "COUPON_ERROR_PAUSE_MS", 200)
    html = _TILE_HELPERS + """<div id="h">Flipkart</div>
    <div id="t1"><div>Coupons</div><div>₹500 off</div><div id="c1" style="cursor:pointer">Apply</div></div>
    <script>document.getElementById('h')['__reactFiber$t'] = {}; liveTile('c1', 'accept');</script>"""

    result, clicked = asyncio.run(_with_actions(html, _apply_and_clicks, statuses=(503,)))
    assert clicked == ["c1", "c1"]
    assert result.applied == ["₹500 off"]


def test_a_slow_page_gets_longer_while_its_buy_now_is_asleep():
    html = _TILE_HELPERS + """<div id="container">
      <div id="t1"><div>Coupons</div><div>₹500 off</div><div id="c1" style="cursor:pointer">Apply</div></div>
      <div id="buy" style="cursor:pointer">Buy now</div>
    </div>
    <script>
      document.getElementById('container')['__reactContainer$t'] = {};
      setTimeout(() => { document.getElementById('buy')['__reactFiber$t'] = {}; liveTile('c1', 'accept'); }, 2500);
    </script>"""

    async def body(page):
        return await _apply_and_clicks(page, ready_timeout_ms=1_000, slow_page_timeout_ms=6_000)

    result, clicked = asyncio.run(_with_actions(html, body))
    assert clicked == ["c1"]
    assert result.applied == ["₹500 off"]
