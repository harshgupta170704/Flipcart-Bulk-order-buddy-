"""Submitting an order, and proving it exists (mapped live 2026-10-24).

Every scripted Cash on Delivery order before this was silently never placed,
and reported as placed anyway:

* COD is two clicks. "Place Order" only opens a "Confirm Cash on Delivery
  Order" popup; the popup's "Confirm order" sends the order. The flow pressed
  the first, then navigated away and threw the popup out.
* It then read the newest row of the account's order list and called that its
  order — a cancelled backpack from the day before, ₹346 against a ₹17,707
  checkout, under the same id on three separate runs.

So these pin both directions. Nothing is "placed" without Flipkart's own
confirmation page naming the order; and once the order has been sent, nothing
is "failed" either, because a real order recorded as failed hands its units
back and the next account buys them again.
"""

import asyncio

import pytest

from app.automation import ordering as order_flow
from app.automation.ordering import (
    PAYMENT_CARD,
    PAYMENT_COD,
    AddressInfo,
    CardInfo,
    GstInfo,
    full_order_id,
    reference_from_url,
)
from app.core import selectors as S
from app.db.models import OrderStatus
from tests.fakes import FakePage

ADDR = AddressInfo(name="N", phone="", pin="226013", area="Lucknow")
GST = GstInfo(gstin="", business_name="")
CARD = CardInfo(number="4111111111111111", month="12", year="28", cvv="123")
#: As Flipkart writes it: the reference is the order id minus its last two digits.
CONFIRMED = "https://www.flipkart.com/orderresponse?reference_id=OD3389638644358401"


def _run(page, *, mode=PAYMENT_COD, target=18_000.0, card=None):
    return asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://dl.flipkart.com/s/uFy91dNNNN",
            address=ADDR,
            gst=GST,
            card=card if card is not None else (CARD if mode == PAYMENT_CARD else None),
            qty=1,
            target=target,
            payment_mode=mode,
        )
    )


def _page(total="₹17,707", **attrs) -> FakePage:
    page = FakePage(inner_text_map={S.ORDER_TOTAL: total})
    page.confirmation_url = CONFIRMED
    for name, value in attrs.items():
        setattr(page, name, value)
    return page


# --- Cash on Delivery: two clicks, and only the second one orders -----------
def test_cod_presses_place_order_then_confirm_order_once_each():
    page = _page()
    result = _run(page)

    assert result["status"] == OrderStatus.placed
    assert result["order_id"] == "OD338963864435840100"
    assert page.clicks.count(S.COD_PLACE_ORDER_BTN) == 1
    assert page.clicks.count(S.COD_CONFIRM_ORDER_BTN) == 1
    assert page.clicks.index(S.COD_PLACE_ORDER_BTN) < page.clicks.index(
        S.COD_CONFIRM_ORDER_BTN
    )


def test_place_order_alone_is_never_reported_as_an_order():
    """The bug itself: Place Order pressed, popup never confirmed.

    With the popup's button never appearing, nothing was sent — so this is a
    clean failure, and it must say so rather than claim an order.
    """
    page = _page()
    page.counts[S.COD_CONFIRM_ORDER_BTN] = 0  # the popup never opens
    result = _run(page)

    assert result["status"] == OrderStatus.failed
    assert "popup never appeared" in result["error"]
    assert "nothing was sent" in result["error"]
    assert S.COD_PLACE_ORDER_BTN in page.clicks
    assert not page.submitted


def test_an_ambiguous_popup_is_refused_rather_than_guessed():
    """Two candidates for the one control that spends money: press neither."""
    page = _page()
    page.counts[S.COD_CONFIRM_ORDER_BTN] = 2
    result = _run(page)

    assert result["status"] == OrderStatus.failed
    assert "found 2" in result["error"]
    assert not page.submitted


def test_a_place_order_that_cannot_be_pressed_sends_nothing():
    page = _page()
    page.fail_clicks_on = {S.COD_PLACE_ORDER_BTN}
    result = _run(page)

    assert result["status"] == OrderStatus.failed
    assert "nothing was sent" in result["error"]
    assert S.COD_CONFIRM_ORDER_BTN not in page.clicks


def test_over_target_never_even_opens_the_popup():
    page = _page(total="₹18,000.01")
    result = _run(page)

    assert result["status"] == OrderStatus.over_target
    assert S.COD_PLACE_ORDER_BTN not in page.clicks
    assert not page.submitted


# --- after the order is sent: placed only on proof, never "failed" ----------
def test_a_confirmation_that_never_comes_is_unconfirmed_not_failed():
    page = _page(confirmation_url=None)
    result = _run(page)

    assert result["status"] == OrderStatus.unconfirmed
    assert result["order_id"] is None
    assert "MAY exist" in result["error"]
    # Everything the record needs to be checked and counted is still there.
    assert result["amount"] == 17_707.0
    assert result["qty"] == 1
    assert result["payment_mode"] == PAYMENT_COD
    # ...and the last page was kept for whoever checks it.
    assert page.screenshots and "unconfirmed" in page.screenshots[0]


def test_a_confirm_click_that_errors_is_unconfirmed_not_failed():
    """Playwright can raise after delivering a click, so an error on the one
    control that orders is not proof that nothing was sent."""
    page = _page()
    page.fail_clicks_on = {S.COD_CONFIRM_ORDER_BTN}
    result = _run(page)

    assert result["status"] == OrderStatus.unconfirmed
    assert "reported an error" in result["error"]


def test_a_confirmation_page_reporting_failure_is_unconfirmed():
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = "Payment failed. Order OD338963864435840100"
    result = _run(page)

    assert result["status"] == OrderStatus.unconfirmed
    assert "payment failed" in result["error"].lower()


@pytest.mark.parametrize(
    "wording",
    [
        "Your payment has failed",
        "Payment unsuccessful",
        "Payment failure. Order OD3389638644358401",
        "Order not placed",
        "Your payment is pending",
        "Transaction declined",
        "Sorry, your order could not be placed",
    ],
)
def test_failure_wordings_beyond_a_fixed_phrase_list(wording):
    """A fixed phrase list missed "Your payment has failed" and would have
    called a failed payment placed."""
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = f"{wording}. Reference OD3389638644358401"
    assert _run(page)["status"] == OrderStatus.unconfirmed


def test_failure_shown_on_any_read_counts_even_if_a_later_read_lacks_it():
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = [
        "Payment failed for OD3389638644358401",
        "Order OD3389638644358401 — continue shopping",
        "Order OD3389638644358401 — continue shopping",
    ]
    assert _run(page)["status"] == OrderStatus.unconfirmed


def test_a_success_page_is_still_placed():
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = "Order placed successfully! Order ID OD338963864435840100"
    assert _run(page)["status"] == OrderStatus.placed


# --- the order request itself is watched ---------------------------------------
def test_place_order_sending_the_order_itself_is_never_failed():
    """If Flipkart ever skipped the popup, Place Order would send the order
    and the popup wait would time out. Saying "failed, nothing sent" then would
    hand the units to the next account."""
    page = _page(place_order_sends=True)
    page.counts[S.COD_CONFIRM_ORDER_BTN] = 0  # no popup on this checkout
    result = _run(page)

    assert result["status"] == OrderStatus.placed
    assert result["order_id"] == "OD338963864435840100"


def test_place_order_sending_without_a_confirmation_is_unconfirmed():
    page = _page(place_order_sends=True, confirmation_url=None)
    page.counts[S.COD_CONFIRM_ORDER_BTN] = 0
    result = _run(page)

    assert result["status"] == OrderStatus.unconfirmed
    assert "order request was sent anyway" in result["error"]


def test_the_unconfirmed_note_says_whether_the_order_request_left():
    sent = _page(confirmation_url=None)
    assert "order request WAS sent" in _run(sent)["error"]

    not_sent = _page()
    not_sent.fail_clicks_on = {S.COD_CONFIRM_ORDER_BTN}  # the click errored
    assert "no order request was seen" in _run(not_sent)["error"]


def test_the_watcher_stays_on_through_the_confirmation_wait(monkeypatch):
    """Confirm order sends its request a moment AFTER the click returns, so a
    watcher removed when the buttons are done would miss it."""
    from tests.fakes import ORDER_REQUEST_URL, FakeRequest

    async def late_request(page, **_):
        for handler in list(page.handlers.get("request", [])):
            handler(FakeRequest("POST", ORDER_REQUEST_URL))
        return None, "Flipkart's order confirmation page did not appear"

    monkeypatch.setattr(order_flow, "await_order_confirmation", late_request)
    page = _page()
    page.fail_clicks_on = {S.COD_CONFIRM_ORDER_BTN}  # no request at click time
    result = _run(page)

    assert result["status"] == OrderStatus.unconfirmed
    assert "order request WAS sent" in result["error"]
    assert page.handlers["request"] == []  # and it is removed afterwards


def test_the_order_id_is_read_off_the_page_when_the_address_has_none():
    page = _page(confirmation_url="https://www.flipkart.com/orderresponse?reference_id=PG77")
    page.inner_text_map[S.PAGE_BODY] = "Order placed! Order ID OD555512345678901234"
    result = _run(page)

    assert result["status"] == OrderStatus.placed
    assert result["order_id"] == "OD555512345678901234"


def test_a_confirmation_page_naming_no_order_is_unconfirmed():
    page = _page(confirmation_url="https://www.flipkart.com/orderresponse")
    page.inner_text_map[S.PAGE_BODY] = "Thank you for shopping"
    result = _run(page)

    assert result["status"] == OrderStatus.unconfirmed
    assert "did not name the order" in result["error"]


def test_an_address_that_merely_mentions_the_route_is_not_the_confirmation_page():
    """A login bounce carrying the route as a parameter must not count: the
    path itself has to be the confirmation page."""
    page = _page(
        confirmation_url=(
            "https://www.flipkart.com/account/login?ret=/orderresponse?reference_id=OD3389638644358401"
        )
    )
    result = _run(page)
    assert result["status"] == OrderStatus.unconfirmed
    assert result["order_id"] is None


def test_being_on_the_confirmation_address_alone_is_not_proof():
    """A reference in the address bar with a page that says nothing about the
    order (still loading, unreadable, an error template) is not a placed order."""
    for body in ("", "Loading... Please wait", "Something went wrong"):
        page = _page()
        page.inner_text_map[S.PAGE_BODY] = body
        result = _run(page)
        assert result["status"] == OrderStatus.unconfirmed, body
        assert result["order_id"] is None


#: Flipkart's real success page, as seen live on two orders (2026-10-24): a
#: heading, a delivery date, a "Track & manage order" link — and no order id in
#: the text at all.
REAL_SUCCESS_TEXT = (
    "Thanks for shopping with us! Delivery by Sun, Oct 25th '26 Track & manage order "
    "Coupons unlocked with this purchase Continue Shopping Send Order Details "
    "Go to My Orders Ayush Agarwal Tower5-1005 Garden Bay, Lucknow Uttar Pradesh - 226013"
)


def test_flipkarts_real_success_page_is_recognised():
    """The page that two real orders landed on was recorded as UNCONFIRMED,
    because it says "Thanks for shopping" (not "thank you") and shows no id."""
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = REAL_SUCCESS_TEXT
    result = _run(page)
    assert result["status"] == OrderStatus.placed
    assert result["order_id"] == "OD338963864435840100"


def test_the_order_id_is_taken_from_the_track_order_link():
    """The strongest proof on the page: it links to the order itself."""
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = REAL_SUCCESS_TEXT
    page.order_links = [
        "https://www.flipkart.com/order_details?order_id=OD338963864435840100&item_id=1&unit_id=2"
    ]
    result = _run(page)
    assert result["status"] == OrderStatus.placed
    assert result["order_id"] == "OD338963864435840100"


def test_a_link_to_some_other_order_is_not_this_orders_proof():
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = "Loading"
    page.order_links = ["https://www.flipkart.com/order_details?order_id=OD111111111111111100"]
    result = _run(page)
    assert result["status"] == OrderStatus.unconfirmed


def test_a_page_that_confirms_in_words_is_enough_with_a_reference():
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = "Your order has been placed. We'll email you the details."
    result = _run(page)
    assert result["status"] == OrderStatus.placed
    assert result["order_id"] == "OD338963864435840100"  # rebuilt from the reference


def test_leaving_the_confirmation_page_mid_read_is_unconfirmed(monkeypatch):
    """The proof is only good for the page it was read on."""
    page = _page()
    original = page.wait_for_timeout

    async def bounce(ms):
        page.url = "https://www.flipkart.com/account/login"
        await original(ms)

    page.inner_text_map[S.PAGE_BODY] = ["Loading", "Order placed. Order ID OD338963864435840100"]
    page.wait_for_timeout = bounce
    result = _run(page)
    assert result["status"] == OrderStatus.unconfirmed
    assert "left the confirmation page" in result["error"]


def test_nothing_after_submitting_can_raise(monkeypatch):
    """Once sent, an exception would reach the job runner as a failure — and a
    failure gives the units back for the next account to buy again."""

    async def boom(page, **_):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(order_flow, "await_order_confirmation", boom)
    result = _run(_page())

    assert result["status"] == OrderStatus.unconfirmed
    assert "browser crashed" in result["error"]


# --- the card form must hold the card before Pay (audit F-1 / F-2, 25 Oct 2026) ---
def test_a_card_form_that_cannot_be_filled_stops_before_pay():
    """Pay over an empty form charges whatever the payment page had preselected,
    or nothing. The refusal names the field and the error's type, never the
    number that was being typed."""
    page = _page()
    page.fail_clicks_on.add(S.CARD_NUMBER)  # the fill of this field fails

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert "card could not be entered" in result["error"] and "card number" in result["error"]
    assert "nothing was charged" in result["error"]
    assert "4111" not in result["error"]
    assert S.CARD_PAY_BTN not in page.clicks and not page.submitted


def test_a_filled_card_form_reaches_pay_exactly_once():
    page = _page()

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.placed
    assert page.fills[S.CARD_NUMBER] == "4111111111111111"
    assert page.fills[S.CARD_EXPIRY] == "12/28" and page.fills[S.CARD_CVV] == "123"
    assert page.clicks.count(S.CARD_PAY_BTN) == 1


# --- the bank's page after Pay (audit F-5; HDFC's card password, 26 Oct 2026) -----
BANK_URL = "https://netsafe.hdfcbank.com/authenticate"
PASSWORD_FIELD = "//input[@type='password']"
CARD_WITH_PASSWORD = CardInfo(number="4111111111111111", month="12", year="28", cvv="123", password="s3cret-pw")


class _BankPage(FakePage):
    """Pressing Pay lands on the bank's page; Flipkart's confirmation follows
    only once the right password has been typed into the bank's one field."""

    def clicked(self, xpath: str) -> None:
        super().clicked(xpath)
        if xpath == S.CARD_PAY_BTN:
            self.url = BANK_URL

    async def wait_for_url(self, pattern, timeout=None, wait_until=None):
        if callable(pattern):
            if self.fills.get(PASSWORD_FIELD) == "s3cret-pw":
                self.url = self.confirmation_url
                return
            raise RuntimeError("no navigation: the bank is still waiting")
        return await super().wait_for_url(pattern, timeout, wait_until)


def _bank_page() -> _BankPage:
    page = _BankPage(inner_text_map={S.ORDER_TOTAL: "₹17,707"})
    page.confirmation_url = CONFIRMED
    return page


def test_the_cards_password_is_typed_on_the_banks_page_and_the_order_confirms():
    page = _bank_page()

    result = _run(page, mode=PAYMENT_CARD, card=CARD_WITH_PASSWORD)

    assert result["status"] == OrderStatus.placed
    assert page.fills[PASSWORD_FIELD] == "s3cret-pw"
    assert page.clicks.count(S.CARD_PAY_BTN) == 1
    assert "s3cret-pw" not in str(result)


def test_without_a_password_on_the_sheet_the_banks_page_is_left_to_a_person_and_the_order_is_unconfirmed():
    page = _bank_page()

    result = _run(page, mode=PAYMENT_CARD)  # CARD carries no password

    assert result["status"] == OrderStatus.unconfirmed
    assert "no password was typed on the bank's page" in result["error"] and "netsafe.hdfcbank.com" in result["error"]
    # It points to the by-hand option and never claims a window is already open:
    # this default run is headless and has none (audit of 27 Oct 2026).
    assert "finish the bank's step by hand" in result["error"]
    assert "in the browser window" not in result["error"]
    # Nothing was typed into the bank's field.
    assert PASSWORD_FIELD not in page.fills
    assert "4111" not in result["error"]


class _Frame:
    """A frame inside the payment page holding the bank's page; its boxes
    record on the owning page like the page's own."""

    def __init__(self, page: FakePage, url: str) -> None:
        self.page = page
        self.url = url

    def locator(self, selector: str):
        return self.page.locator(selector)


def test_the_banks_page_inside_a_frame_is_found_and_the_password_typed_there():
    """3-D Secure often loads the bank inside a frame while the address bar
    still says flipkart.com."""

    class _FramedBankPage(_BankPage):
        def clicked(self, xpath: str) -> None:
            FakePage.clicked(self, xpath)
            if xpath == S.CARD_PAY_BTN:
                self.frames = [_Frame(self, BANK_URL)]  # the page's own address stays Flipkart's

    page = _FramedBankPage(inner_text_map={S.ORDER_TOTAL: "₹17,707"})
    page.confirmation_url = CONFIRMED

    result = _run(page, mode=PAYMENT_CARD, card=CARD_WITH_PASSWORD)

    assert result["status"] == OrderStatus.placed
    assert page.fills[PASSWORD_FIELD] == "s3cret-pw"


def test_a_site_framed_in_the_payment_page_before_pay_is_not_taken_for_the_bank():
    """Payment pages carry fraud-check and captcha frames from other sites.
    One of those is not the bank's page: the bank is the site that appears
    after Pay."""

    class _FramedAfterPay(_BankPage):
        def clicked(self, xpath: str) -> None:
            FakePage.clicked(self, xpath)  # the address bar stays Flipkart's
            if xpath == S.CARD_PAY_BTN:
                self.frames = [*self.frames, _Frame(self, BANK_URL)]

    page = _FramedAfterPay(inner_text_map={S.ORDER_TOTAL: "₹17,707"})
    page.confirmation_url = CONFIRMED
    page.frames = [_Frame(page, "https://h.online-metrix.net/fp/tags.js")]  # there before Pay

    result = _run(page, mode=PAYMENT_CARD)  # no password on the sheet: the note names the bank

    assert result["status"] == OrderStatus.unconfirmed
    assert "the bank's page (netsafe.hdfcbank.com)" in result["error"]
    assert "online-metrix" not in result["error"]


def test_a_card_form_that_still_complains_after_typing_stops_before_pay():
    """The first live card runs (26 Oct 2026) pressed Pay and the browser never
    left Flipkart's payment page. Whatever the form thinks of the details is
    now read before Pay: its own complaint stops the run while nothing has
    been paid."""
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = "Complete Payment Card Number Card number is required Valid Thru Pay ₹759"

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert "Card number is required" in result["error"] and "nothing was charged" in result["error"]
    assert S.CARD_PAY_BTN not in page.clicks and not page.submitted


def test_a_complaint_that_runs_on_into_the_number_keeps_no_digit():
    """The complaint is read to the end of its line; on a page that showed the
    number beside it, spaced out, the message would carry the card."""
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = "Enter valid card number 4111 1111 1111 1111 Pay ₹759"

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert "Enter valid card number" in result["error"]
    assert not any(ch.isdigit() for ch in result["error"].split("says '")[1].split("'")[0])
    assert "4111" not in result["error"]


def test_an_expiry_complaint_also_stops_before_pay():
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = "Valid Thru Enter valid month/year CVV Pay ₹759"

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert "Enter valid month/year" in result["error"]
    assert not page.submitted


def test_a_four_digit_year_is_typed_as_two_digits():
    """The box reads "MM / YY": "12/2028" in it is December 2020, an expired card."""
    page = _page()
    card = CardInfo(number="4111111111111111", month="7", year="2028", cvv="123")

    result = _run(page, mode=PAYMENT_CARD, card=card)

    assert result["status"] == OrderStatus.placed
    assert page.fills[S.CARD_EXPIRY] == "07/28"


def _slash_keeping_box(text: str, key: str) -> str:
    """A "MM / YY" box with room for seven characters that adds its own " / "
    after the month and keeps a typed "/" as well."""
    if len(text) >= 7:
        return text
    text += key
    if key.isdigit() and sum(ch.isdigit() for ch in text) == 2:
        text += " / "
    return text[:7]


def test_an_expiry_box_that_keeps_a_typed_slash_gets_the_digits_alone():
    """"12/28" typed into such a box holds "12 / /2": the read-back sees it and
    the digits alone, which the box formats itself, go in instead — all
    before Pay."""
    page = _page()
    page.key_rules[S.CARD_EXPIRY] = _slash_keeping_box

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.placed
    assert page.fills[S.CARD_EXPIRY] == "12 / 28"
    assert page.clicks.count(S.CARD_PAY_BTN) == 1


def test_an_expiry_box_that_holds_neither_way_stops_before_pay():
    page = _page()
    page.max_chars[S.CARD_EXPIRY] = 3  # neither "12/28" nor "1228" fits

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert "expiry box" in result["error"] and not page.submitted


def test_offer_small_print_outside_the_card_form_does_not_stop_the_run():
    """The page carries offers; "not supported on corporate cards" in one of
    them is no complaint about this card. Complaints are read in the form."""
    page = _page()
    page.inner_text_map[order_flow.CARD_FORM_BLOCK] = "Card Number Valid Thru CVV Pay ₹17,707"
    page.inner_text_map[S.PAGE_BODY] = (
        "Bank offer: corporate cards are not supported for this offer. "
        "Order placed successfully. Order ID OD428123456789100"
    )

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.placed


def test_a_complaint_inside_the_card_form_stops_the_run():
    page = _page()
    page.inner_text_map[order_flow.CARD_FORM_BLOCK] = "Card Number Card number is invalid CVV Pay ₹17,707"

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert "Card number is invalid" in result["error"] and not page.submitted


def test_a_complaint_that_shows_a_moment_after_the_last_box_stops_the_run():
    """A check of the card with its bank answers after the box is left."""
    page = _page()
    page.inner_text_map[order_flow.CARD_FORM_BLOCK] = [
        "Card Number Valid Thru CVV",
        "Card Number Valid Thru CVV",
        "Card Number Card number is invalid CVV",
    ]

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert "Card number is invalid" in result["error"] and not page.submitted


def test_a_complaint_that_shows_after_the_price_check_stops_pay():
    """The last look is right before Pay, after the order total was read and
    the order recorded: still nothing sent, and the record is kept to be
    settled as failed."""
    page = _page()

    def reserve(_row: dict) -> int:
        page.inner_text_map[S.PAGE_BODY] = "Card Number Card number is invalid Pay ₹17,707"
        return 7

    result = asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://dl.flipkart.com/s/uFy91dNNNN",
            address=ADDR,
            gst=GST,
            card=CARD,
            qty=1,
            target=18_000.0,
            payment_mode=PAYMENT_CARD,
            reserve=reserve,
        )
    )

    assert result["status"] == OrderStatus.failed
    assert "Card number is invalid" in result["error"] and "nothing was sent" in result["error"]
    assert S.CARD_PAY_BTN not in page.clicks and not page.submitted
    assert result["reservation"] == 7


def test_a_card_prints_masked():
    card = CardInfo(number="4111111111111111", month="12", year="28", cvv="123", password="s3cret-pw")

    for shown in (repr(card), str(card), f"{card}"):
        assert "4111111111111111" not in shown and "s3cret" not in shown
        assert "123" not in shown and "12/28" not in shown
        assert "1111" in shown  # the last four, as a statement shows them


@pytest.mark.parametrize(
    ("month", "year", "shown"),
    [("12", "28", "12/28"), ("1", "2031", "01/31"), ("09", "2030", "09/30"), ("", "28", ""), ("12", "", "")],
)
def test_the_expiry_as_the_box_takes_it(month, year, shown):
    assert order_flow._expiry_for_form(month, year) == shown


def test_a_box_that_keeps_fewer_digits_than_were_typed_stops_before_pay():
    """A box that cut the number short holds another card; the read-back of
    every box catches it while nothing has been paid."""
    page = _page()
    page.max_chars[S.CARD_NUMBER] = 12  # sixteen digits typed, twelve kept

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert "card number box" in result["error"] and "does not hold what was typed" in result["error"]
    assert "nothing was charged" in result["error"]
    assert "4111" not in result["error"]
    assert not page.submitted


def test_a_cut_short_expiry_is_retyped_as_digits_and_goes_through():
    """"12/28" into a four-character box keeps "12/2"; the digits alone fit."""
    page = _page()
    page.max_chars[S.CARD_EXPIRY] = 4

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.placed
    assert page.fills[S.CARD_EXPIRY] == "1228"


def test_card_entry_presses_nothing_that_could_pay():
    """Pay is pressed in one place, after the price gate. The old flow also
    clicked a "card submit" button found by a stale class name."""
    page = _page()

    _run(page, mode=PAYMENT_CARD)

    assert S.CARD_SUBMIT not in page.clicks and S.PAY_ADD_CARD not in page.clicks
    assert S.PAY_ADD_NEW_CARD not in page.clicks  # the form was already shown
    assert page.clicks.count(S.CARD_PAY_BTN) == 1


def test_a_hidden_card_form_is_opened_by_its_add_new_card_link():
    """An account with saved cards shows those first; the new-card form opens
    from a link found by its wording."""
    page = _page()
    page.hidden.add(S.CARD_NUMBER)

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.placed
    assert S.PAY_ADD_NEW_CARD in page.clicks
    assert page.clicks.index(S.PAY_ADD_NEW_CARD) < page.clicks.index(S.CARD_PAY_BTN)


def test_a_card_flipkart_will_not_take_stops_before_pay():
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = "Card Number This card is not supported for this payment Pay ₹759"

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert "card is not supported for this payment" in result["error"]
    assert not page.submitted


def test_the_card_boxes_are_painted_over_in_the_screenshot_not_emptied():
    """Emptying the boxes made Flipkart's form print "Card number is required"
    and "Enter valid month/year" into the very screenshot meant to explain the
    run (26 Oct 2026). The boxes are masked in the picture; the page is left
    as it was."""
    page = _page(confirmation_url=None)

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.unconfirmed
    assert page.screenshots and "unconfirmed" in page.screenshots[0]
    masked = page.screenshot_masks[0]
    assert {S.CARD_NUMBER, S.CARD_EXPIRY, S.CARD_CVV, PASSWORD_FIELD} <= set(masked)
    assert page.fills[S.CARD_NUMBER] == "4111111111111111"  # left as typed, not emptied


def test_a_screenshot_that_cannot_be_masked_empties_the_boxes_instead():
    page = _page(confirmation_url=None)
    original = page.screenshot

    async def no_masks(path=None, full_page=False, mask=None, mask_color=None):
        if mask:
            raise RuntimeError("mask is not supported here")
        await original(path=path, full_page=full_page)

    page.screenshot = no_masks

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.unconfirmed
    assert page.screenshots  # the plain picture was taken...
    assert page.fills[S.CARD_NUMBER] == "" and page.fills[S.CARD_CVV] == ""  # ...after emptying the boxes


def test_an_unconfirmed_card_run_says_where_the_browser_went_after_pay():
    """Pay pressed, no bank page and no confirmation: the run says so, rather
    than leaving the operator one screenshot to guess from."""
    page = _page(confirmation_url=None)

    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.unconfirmed
    assert "the browser never left Flipkart after Pay" in result["why_unconfirmed"]
    assert "Flipkart's order request WAS sent" in result["why_unconfirmed"]
    assert "4111" not in result["why_unconfirmed"]


def test_an_unconfirmed_card_run_names_the_bank_it_reached():
    page = _bank_page()

    result = _run(page, mode=PAYMENT_CARD)  # no password on the sheet: the bank waits

    assert result["status"] == OrderStatus.unconfirmed
    assert "pages away from Flipkart after Pay: netsafe.hdfcbank.com" in result["why_unconfirmed"]


def test_the_banks_page_in_a_new_window_gets_the_password():
    popup = FakePage()
    popup.url = BANK_URL

    class _PopupBankPage(FakePage):
        def clicked(self, xpath: str) -> None:
            super().clicked(xpath)
            if xpath == S.CARD_PAY_BTN:
                for handler in list(self.handlers.get("popup", [])):
                    handler(popup)

        async def wait_for_url(self, pattern, timeout=None, wait_until=None):
            if callable(pattern):
                if popup.fills.get(PASSWORD_FIELD) == "s3cret-pw":
                    self.url = self.confirmation_url
                    return
                raise RuntimeError("no navigation: the bank is still waiting")
            return await super().wait_for_url(pattern, timeout, wait_until)

    page = _PopupBankPage(inner_text_map={S.ORDER_TOTAL: "₹17,707"})
    page.confirmation_url = CONFIRMED

    result = _run(page, mode=PAYMENT_CARD, card=CARD_WITH_PASSWORD)

    assert result["status"] == OrderStatus.placed
    assert popup.fills[PASSWORD_FIELD] == "s3cret-pw"
    assert PASSWORD_FIELD not in page.fills


def test_a_password_box_that_draws_late_is_waited_for():
    """The address turns to the bank's before its form has drawn."""
    page = _bank_page()
    page.counts[PASSWORD_FIELD] = 0
    original = page.wait_for_timeout
    looks_after_pay = {"n": 0}

    async def later(ms):
        if S.CARD_PAY_BTN in page.clicks:
            looks_after_pay["n"] += 1
            if looks_after_pay["n"] >= 3:
                page.counts[PASSWORD_FIELD] = 1
        await original(ms)

    page.wait_for_timeout = later

    result = _run(page, mode=PAYMENT_CARD, card=CARD_WITH_PASSWORD)

    assert result["status"] == OrderStatus.placed
    assert page.fills[PASSWORD_FIELD] == "s3cret-pw"
    assert looks_after_pay["n"] >= 3


def test_a_banks_page_that_never_shows_a_password_box_is_unconfirmed():
    page = _bank_page()
    page.counts[PASSWORD_FIELD] = 0

    result = _run(page, mode=PAYMENT_CARD, card=CARD_WITH_PASSWORD)

    assert result["status"] == OrderStatus.unconfirmed
    assert "showed no password field" in result["error"]
    assert "s3cret-pw" not in str(result)


def test_the_bank_form_is_submitted_by_enter_when_its_submit_button_is_not_unique():
    """Two submit buttons can be Submit and the bank's Cancel: neither is
    guessed at; Enter in the password box submits its form."""
    page = _bank_page()
    submit = "ancestor::form[1]//*[(self::button or self::input) and @type='submit']"
    page.counts[submit] = 2

    result = _run(page, mode=PAYMENT_CARD, card=CARD_WITH_PASSWORD)

    assert result["status"] == OrderStatus.placed
    assert f"key:Enter@{PASSWORD_FIELD}" in page.clicks
    assert submit not in page.clicks


def test_the_runs_message_for_an_unconfirmed_order_carries_the_reason():
    """The first live card runs ended with "unconfirmed: submitted for 759.00
    but Flipkart never confirmed it" and nothing on why. The reason now
    follows, with anything card-shaped taken out."""
    from app.jobs.tasks import _order_message

    result = {
        "amount": 759.0,
        "why_unconfirmed": "no order request was seen leaving the browser; card 4111111111111111",
        "error": "the longer text with what to do",
    }

    message = _order_message(OrderStatus.unconfirmed, result, 10_000.0)

    assert message.startswith("unconfirmed: submitted for ₹759.00 but Flipkart never confirmed it")
    assert message.endswith("(no order request was seen leaving the browser; card [redacted])")
    assert "4111111111111111" not in message


def test_a_banks_page_with_more_than_one_password_field_is_not_guessed_at():
    page = _bank_page()
    page.counts[PASSWORD_FIELD] = 2

    result = _run(page, mode=PAYMENT_CARD, card=CARD_WITH_PASSWORD)

    assert result["status"] == OrderStatus.unconfirmed
    assert "found 2" in result["error"]
    assert page.fills.get(PASSWORD_FIELD, "") == "" and "s3cret-pw" not in str(result)


# --- a quantity the checkout will not confirm (audit F-10) -----------------------
def test_a_quantity_the_checkout_will_not_confirm_stops_the_order():
    """APPLY was pressed for two units and the box cannot be read back: the
    checkout may carry two while the record would say one. The order stops
    before any payment step."""
    page = _page()  # the fake offers a quantity control but never shows "Qty: N"

    result = asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://dl.flipkart.com/s/uFy91dNNNN",
            address=ADDR,
            gst=GST,
            card=None,
            qty=2,
            target=50_000.0,
            payment_mode=PAYMENT_COD,
        )
    )

    assert result["status"] == OrderStatus.failed
    assert "did not confirm the quantity" in result["error"] and "nothing was charged" in result["error"]
    assert not page.submitted


# --- the checkout's own GST (audit F-4) ----------------------------------------
CHOSEN_GST = GstInfo(gstin="09AAUFT2204P1ZI", business_name="Transparent Deals")


def _run_with_gst(page, gst: GstInfo):
    return asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://dl.flipkart.com/s/uFy91dNNNN",
            address=ADDR,
            gst=gst,
            card=None,
            qty=1,
            target=18_000.0,
            payment_mode=PAYMENT_COD,
        )
    )


def test_an_order_the_checkout_bills_under_another_gstin_is_refused_before_payment():
    """The order would be invoiced to another entity and read correctly on no
    screen until the read-back: refused while nothing has been sent."""
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = "GST Invoice 27ABCDE1234F1Z5 Change · Price details"

    result = _run_with_gst(page, CHOSEN_GST)

    assert result["status"] == OrderStatus.failed
    assert "27ABCDE1234F1Z5" in result["error"] and "09AAUFT2204P1ZI" in result["error"]
    assert S.COD_PLACE_ORDER_BTN not in page.clicks and not page.submitted


def test_the_chosen_gstin_on_the_checkout_lets_the_order_through_as_verified():
    page = _page()
    page.inner_text_map[S.PAGE_BODY] = (
        "GST Invoice 09AAUFT2204P1ZI Change. Order placed successfully. Order ID OD338963864435840100"
    )

    result = _run_with_gst(page, CHOSEN_GST)

    assert result["status"] == OrderStatus.placed
    assert result["gst_verified"] is True


def test_a_checkout_naming_no_gstin_is_let_through_but_not_called_verified():
    """The read-back's GST check is the other half: a page that names no GSTIN
    neither proves nor disproves the billing entity."""
    result = _run_with_gst(_page(), CHOSEN_GST)

    assert result["status"] == OrderStatus.placed
    assert result["gst_verified"] is False


# --- what gets recorded --------------------------------------------------------
def test_the_account_order_list_is_never_read_after_submitting():
    """Reading "the newest row" is what reported a stale backpack as our
    order. The confirmation page names the order; the list is not consulted."""
    page = _page()
    _run(page)

    assert S.ORDERS_URL not in page.goto_urls


def test_amount_and_model_come_from_this_checkout_not_from_another_order():
    """The phantom row read ₹346 and an HP backpack on a ₹17,707 vivo run.

    The amount is the price check's own figure; the model is read from the
    product page before buying. Neither can come from some other order now.
    """
    page = _page()
    page.jsonld = [{"@type": "Product", "name": "vivo T5 Lite 44W 5G", "color": "Twilight Shadow"}]
    # What a stale order's page would have said, were it ever read:
    page.inner_text_map[S.ORDER_AMOUNT] = "₹346"
    page.inner_text_map[S.ORDER_MODEL] = "HP X Entry 32 L Laptop Backpack"
    result = _run(page)

    assert result["amount"] == 17_707.0
    assert result["model"] == "vivo T5 Lite 44W 5G"
    assert result["color"] == "Twilight Shadow"


# --- card: one press of Pay, then the same proof -------------------------------
def test_card_presses_pay_once_and_waits_for_the_confirmation():
    page = _page(total="₹16,105")
    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.placed
    assert result["order_id"] == "OD338963864435840100"
    assert page.clicks.count(S.CARD_PAY_BTN) == 1
    assert S.COD_PLACE_ORDER_BTN not in page.clicks


def test_card_with_no_pay_button_sends_nothing():
    page = _page(total="₹16,105")
    page.counts[S.CARD_PAY_BTN] = 0
    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.failed
    assert not page.submitted


def test_card_pay_without_a_confirmation_is_unconfirmed():
    page = _page(total="₹16,105", confirmation_url=None)
    result = _run(page, mode=PAYMENT_CARD)

    assert result["status"] == OrderStatus.unconfirmed


# --- reading the confirmation address ------------------------------------------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (CONFIRMED, "OD3389638644358401"),
        (CONFIRMED + "&src=or&pr=1", "OD3389638644358401"),
        ("https://www.flipkart.com/orderresponse?reference_id=PG123", None),
        ("https://www.flipkart.com/orderresponse", None),
        ("https://www.flipkart.com/orderresponse?reference_id=OD12", None),  # too short
        ("", None),
    ],
)
def test_reference_from_url(url, expected):
    assert reference_from_url(url) == expected


def test_the_reference_is_the_order_id_without_its_last_two_digits():
    """Flipkart's own tracking rebuilds the id as reference + "00". Recording
    the bare reference would file the order under an id nothing else uses, and
    the next sync would import the real order as a second row."""
    assert full_order_id("OD3389638644358401", "") == "OD338963864435840100"


def test_a_full_id_on_the_page_beats_the_rebuild():
    text = "Order placed. Order ID: OD338963864435840101"
    assert full_order_id("OD3389638644358401", text) == "OD338963864435840101"


def test_ids_that_do_not_extend_the_reference_are_ignored():
    """A different order mentioned on the page (a recommendation, an older
    order) must never be recorded as this one."""
    text = "You recently ordered OD111111111111111100. Order OD3389638644358401"
    assert full_order_id("OD3389638644358401", text) == "OD338963864435840100"


def test_without_a_reference_the_page_id_is_used():
    assert full_order_id(None, "Order ID OD555512345678901234") == "OD555512345678901234"
    assert full_order_id(None, "Thank you") is None


def test_the_other_confirmation_address_is_accepted_too():
    """Flipkart's own page check accepts orderConfirmation as well as
    orderresponse; waiting only for one would file real orders as unconfirmed."""
    page = _page(confirmation_url="https://www.flipkart.com/orderConfirmation?reference_id=OD3389638644358401")
    result = _run(page)

    assert result["status"] == OrderStatus.placed
    assert result["order_id"] == "OD338963864435840100"


def test_a_bank_page_that_blocks_the_browser_is_named_in_the_note():
    """HDFC's 3-D Secure returned "Request Blocked" to the automated browser
    (seen live 27 Oct 2026). The order may still have reached Flipkart, so the
    status stays unconfirmed, but the note says the bank refused the browser,
    not just that Flipkart never confirmed."""
    page = _bank_page()  # Pay lands on netsafe.hdfcbank.com; with no password the browser stays there
    page.inner_text_map[S.PAGE_BODY] = "HDFC BANK Request Blocked Access to this endpoint is blocked by our security systems"

    result = _run(page, mode=PAYMENT_CARD)  # no password on the sheet

    assert result["status"] == OrderStatus.unconfirmed
    assert "Request Blocked" in result["error"]
    assert "visible window" in result["error"]
