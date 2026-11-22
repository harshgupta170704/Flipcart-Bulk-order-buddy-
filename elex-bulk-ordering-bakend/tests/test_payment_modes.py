"""The order form's other payment modes: gift cards (only, or with a card),
COD + Card advance, RTGS and IMPS."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import jobs as jobs_api
from app.automation import ordering
from app.core.security import create_access_token
from app.db.models import Account, AccountStatus, Address
from app.io.gift_cards import parse_gift_card_sheet


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})

        async def _noop():
            return None

        return _noop()


@pytest.fixture
def api(db) -> TestClient:
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.add_all([Account(email="a@x.in", status=AccountStatus.session_valid), Account(email="b@x.in", status=AccountStatus.session_valid)])
    db.commit()
    app = FastAPI()
    app.include_router(jobs_api.router)
    return TestClient(app, headers={"Authorization": f"Bearer {create_access_token('t', role='admin')}"})


SHEET = b"Email,Voucher Number,Voucher PIN\nA@x.in,1111 2222 3333 4444,123456\na@x.in,5555666677778888,654321\nb@x.in,,111\n"


def test_sheet_is_read_by_heading_and_keeps_nothing_printable():
    rows, rejected = parse_gift_card_sheet(SHEET, "gv.csv")
    assert [(r.email, r.number[-4:]) for r in rows] == [("a@x.in", "4444"), ("a@x.in", "8888")]
    assert rejected == [{"row": 4, "reason": "no voucher number"}]
    assert "1111" not in repr(rows[0]) and "123456" not in repr(rows[0])
    assert parse_gift_card_sheet(b"x,y\n1,2\n")[1][0]["reason"].startswith("the heading row")


CARDS = b"Email,Card Number,Exp Month,Exp Year,CVV\na@x.in,4111111111111111,12,2030,123\n"


def test_gv_plus_card_run_pairs_gift_cards_and_stores_none(api, db, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    resp = api.post(
        "/jobs/order",
        data={"product_link": "https://flipkart.com/p/1", "qty": 1, "target": 1000, "payment_mode": "gv_card", "account_ids": "1"},
        files={"gift_cards_file": ("gv.csv", SHEET, "text/csv"), "cards_file": ("cards.csv", CARDS, "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["payload"]["gift_card_accounts"] == 1
    assert "1111" not in resp.text and "123456" not in resp.text
    cards = recorder.calls[0]["kwargs"]["gift_cards"]
    assert [len(v) for v in cards.values()] == [2]


@pytest.mark.parametrize("mode", ["rtgs", "imps", "cod_card", "gv"])
def test_modes_not_mapped_live_are_refused(api, monkeypatch, mode):
    """Review of 17 Nov 2026: not seen on a live payment page, so no run may
    press Pay with them until they are mapped."""
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    resp = api.post("/jobs/order", data={"product_link": "https://flipkart.com/p/1", "qty": 1, "target": 1000, "payment_mode": mode})
    assert resp.status_code == 422 and "not available yet" in resp.text
    assert recorder.calls == []


def test_gift_voucher_only_must_leave_nothing_to_pay():
    assert ordering.gift_cards_left_to_pay(ordering.PAYMENT_GV, 0.0) is None
    assert "₹120.00" in ordering.gift_cards_left_to_pay(ordering.PAYMENT_GV, 120.0)
    assert ordering.gift_cards_left_to_pay(ordering.PAYMENT_GV, None)
    assert ordering.gift_cards_left_to_pay(ordering.PAYMENT_GV_CARD, 120.0) is None


TRAP_PAGE = """
<html><body>
 <span class="QbbOLN">UPI</span>
 <span class="QbbOLN">Credit / Debit / ATM Card</span>
 <span class="QbbOLN">Cash on Delivery</span>
 <p>Refunds reach your bank by IMPS within 3 days. Advance payment may be needed for some orders.</p>
</body></html>
"""

OFFERED_PAGE = """
<html><body>
 <span class="QbbOLN">UPI</span>
 <span class="QbbOLN" id="rtgs">RTGS</span>
 <span class="QbbOLN iUnLoA">IMPS</span>
</body></html>
"""


def test_a_mode_is_matched_only_as_a_payment_option_label():
    async def check(page):
        await page.set_content(TRAP_PAGE)
        imps = await ordering.choose_payment_method(page, ordering.PAYMENT_IMPS)
        advance = await ordering.choose_payment_method(page, ordering.PAYMENT_COD_CARD)
        await page.set_content(OFFERED_PAGE)
        rtgs = await ordering.choose_payment_method(page, ordering.PAYMENT_RTGS)
        greyed = await ordering.choose_payment_method(page, ordering.PAYMENT_IMPS)
        return imps, advance, rtgs, greyed

    imps, advance, rtgs, greyed = _with_page(check)
    assert imps == "Flipkart offers no IMPS on this order's payment page"
    assert advance == "Flipkart offers no COD + Card (advance payment) on this order's payment page"
    assert rtgs is None
    assert greyed and "unavailable" in greyed


PAYMENT_PAGE = """
<html><body>
 <div>Cash on Delivery</div>
 <div><span>Have a Flipkart Gift Card?</span></div>
 <div id="form" style="display:none">
   <input type="number" placeholder="Enter voucher number" id="n">
   <input type="number" placeholder="Enter voucher PIN" id="p">
   <button id="add">Add Gift Card</button>
 </div>
 <ul id="added"></ul>
 <script>
  document.querySelector('span').onclick = () => document.getElementById('form').style.display = 'block';
  document.getElementById('add').onclick = () => {
    const n = document.getElementById('n');
    if (n.value.endsWith('0000')) return;  // refused: the form keeps it
    const li = document.createElement('li'); li.textContent = 'card ' + n.value.slice(-4);
    document.getElementById('added').appendChild(li);
    n.value = ''; document.getElementById('p').value = '';
  };
 </script>
</body></html>
"""


def _with_page(check):
    playwright = pytest.importorskip("playwright.async_api")

    async def go():
        async with playwright.async_playwright() as pw:
            try:
                browser = await pw.chromium.launch()
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium cannot start here: {exc.__class__.__name__}")
            page = await browser.new_page()
            await page.set_content(PAYMENT_PAGE)
            try:
                return await check(page)
            finally:
                await browser.close()

    return asyncio.run(go())


def test_gift_cards_are_entered_and_a_refused_one_stops_the_order():
    async def check(page):
        ok = await ordering.add_gift_cards(page, [ordering.GiftCardInfo("1111222233334444", "1"), ordering.GiftCardInfo("5555666677778888", "2")])
        added = await page.locator("#added li").all_inner_texts()
        refused = await ordering.add_gift_cards(page, [ordering.GiftCardInfo("9999888877770000", "3")])
        missing = await ordering.choose_payment_method(page, ordering.PAYMENT_RTGS)
        return ok, added, refused, missing

    ok, added, refused, missing = _with_page(check)
    assert ok is None and added == ["card 4444", "card 8888"]
    assert refused and "0000" in refused
    assert missing == "Flipkart offers no RTGS on this order's payment page"


def test_coupons_rewards_and_minutes_are_recorded_and_passed(api, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    resp = api.post(
        "/jobs/order",
        data={
            "product_link": "https://flipkart.com/p/1", "qty": 1, "target": 1000, "payment_mode": "cod",
            "coupon_codes": "SAVE10, SAVE10 NEW50", "reward_ids": "R1;R2", "flipkart_minutes": "true",
        },
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()["payload"]
    assert payload["coupon_codes"] == ["SAVE10", "NEW50"]
    assert payload["reward_ids"] == ["R1", "R2"] and payload["flipkart_minutes"] is True
    assert recorder.calls[0]["kwargs"]["coupon_codes"] == ["SAVE10", "NEW50"]
