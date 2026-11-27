"""The bank's password step after Pay (24 Nov 2026, job 689).

HDFC's 3-D Secure page took the card's password, but the run reported "could
not submit the bank's password form (TimeoutError)" and the page ended on
"404 Not Found". HDFC's page had no submit button in its form, so the run
pressed Enter; Enter made the browser send the form by itself, without the
page's own Submit step, and the run then waited 30 s for HDFC to answer that.

Now: the page's own Submit button is pressed; nothing waits for the bank's
answer; nothing is pressed a second time once anything reached the bank;
Enter is never used to send a form the plain way; and a card order that does
not confirm leaves a record of the bank's page (without any value typed).
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from app.automation import ordering
from app.automation.ordering import PAYMENT_CARD, BankStep, CardInfo, _BankTraffic
from app.core import selectors as S
from app.db.models import OrderStatus
from tests.fakes import FakePage, FakeRequest
from tests.test_order_submission import _run

PASSWORD = "s3cret-pw"
CARD = CardInfo(number="4111111111111111", month="12", year="28", cvv="123", password=PASSWORD)


# --- small stand-ins for the bank's page, its box and its buttons ----------------
class _Control:
    def __init__(self, page: "_Place", name: str, *, raises: bool = False, sends: bool = False) -> None:
        self.place, self.name, self.raises, self.sends = page, name, raises, sends

    async def is_visible(self) -> bool:
        return True

    async def evaluate(self, script: str):
        return self.name.lower()

    async def click(self, timeout=None, no_wait_after=None) -> None:
        self.place.pressed.append((self.name, no_wait_after))
        if self.sends:
            self.place.send()
        if self.raises:
            raise TimeoutError("Timeout 4000ms exceeded")


class _Many:
    def __init__(self, items: list) -> None:
        self.items = items

    async def all(self) -> list:
        return list(self.items)


class _Box:
    def __init__(self, place: "_Place", *, plain_form: bool = False) -> None:
        self.place, self.plain_form = place, plain_form
        self.form_controls: list = []

    def locator(self, selector: str) -> _Many:
        return _Many(self.form_controls)

    async def evaluate(self, script: str):
        return self.plain_form

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return True

    async def press(self, key: str, timeout=None, no_wait_after=None) -> None:
        self.place.pressed.append((f"key:{key}", no_wait_after))


class _Place:
    url = "https://securehdfc-acs2ui.example.bank.in/v1/acs/services/browser/creq/L/888"

    def __init__(self) -> None:
        self.pressed: list = []
        self.worded: list = []
        self.traffic = _BankTraffic()
        self.waited_ms = 0

    def send(self) -> None:
        self.traffic.on_request(FakeRequest("POST", "https://securehdfc-acs2ui.example.bank.in/v1/acs/challenge?x=1"))

    def locator(self, selector: str) -> _Many:
        return _Many(self.worded)

    async def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms += ms


def _submit(place: _Place, box: _Box) -> BankStep:
    step = BankStep()
    asyncio.run(ordering._submit_bank_form(place, box, step, place.traffic))
    return step


def test_the_pages_own_submit_button_is_pressed_when_the_form_has_none():
    """Job 689: HDFC's form showed no submit button of its own. The page's
    button saying SUBMIT is pressed, without waiting for the bank's answer."""
    place = _Place()
    box = _Box(place, plain_form=True)
    place.worded = [_Control(place, "SUBMIT", sends=True)]

    step = _submit(place, box)

    assert step.problem is None and step.sent
    assert "the page's Submit button" in step.how
    assert place.pressed == [("SUBMIT", True)]  # pressed once, no waiting for the page that follows


def test_enter_never_sends_a_form_the_plain_way():
    """A form with no submit control and no button saying Submit: Enter would
    make the browser send the form by itself, skipping the page's own step
    (job 689's "404 Not Found"). Nothing is pressed; the step says why."""
    place = _Place()
    box = _Box(place, plain_form=True)

    step = _submit(place, box)

    assert place.pressed == []
    assert step.problem and "could not submit the bank's password form" in step.problem
    assert "Enter was not pressed" in step.problem


def test_nothing_is_pressed_again_once_anything_reached_the_bank():
    """A press that raised but sent the form all the same (the click that
    "timed out" on 23 Nov) is not followed by a second way of sending: a
    second sending of the password can count as a wrong try."""
    place = _Place()
    box = _Box(place)
    box.form_controls = [_Control(place, "Submit", raises=True, sends=True)]
    place.worded = [_Control(place, "Submit")]

    step = _submit(place, box)

    assert step.problem is None and step.sent
    assert [name for name, _ in place.pressed] == ["Submit"]  # not the page's button, not Enter


def test_a_press_that_could_not_be_made_moves_on_to_the_next_way():
    place = _Place()
    box = _Box(place)
    box.form_controls = [_Control(place, "Submit", raises=True)]
    place.worded = [_Control(place, "Continue", sends=True)]

    step = _submit(place, box)

    assert step.sent and "the page's Submit button" in step.how
    assert [name for name, _ in place.pressed] == ["Submit", "Continue"]
    assert step.tried == ["the form's Submit button (TimeoutError)"]


def test_a_press_with_no_sign_is_not_repeated_and_says_so():
    place = _Place()
    box = _Box(place)
    box.form_controls = [_Control(place, "Submit")]
    place.worded = [_Control(place, "Submit")]

    step = _submit(place, box)

    assert step.problem is None and not step.sent
    assert "nothing was seen reaching the bank" in step.how and "not pressed again" in step.how
    assert [name for name, _ in place.pressed] == ["Submit"]
    assert place.waited_ms >= ordering.BANK_SUBMIT_SIGN_MS


def test_of_several_buttons_the_one_saying_only_submit_is_pressed():
    """SUBMIT beside "Verify with OTP instead": the one saying a submit word
    and nothing else is the button."""
    place = _Place()
    box = _Box(place, plain_form=True)
    place.worded = [_Control(place, "Verify with OTP instead"), _Control(place, "Submit", sends=True)]

    step = _submit(place, box)

    assert step.sent and [name for name, _ in place.pressed] == ["Submit"]


def test_a_forms_only_submit_control_saying_cancel_is_never_pressed():
    """The real SUBMIT drawn outside the form, CANCEL its only submit
    control: CANCEL is not taken for the form's Submit button."""
    place = _Place()
    box = _Box(place)
    box.form_controls = [_Control(place, "CANCEL")]
    place.worded = [_Control(place, "SUBMIT", sends=True)]

    step = _submit(place, box)

    assert step.sent and [name for name, _ in place.pressed] == ["SUBMIT"]


def test_several_buttons_none_alone_are_not_guessed_at():
    place = _Place()
    box = _Box(place, plain_form=True)
    place.worded = [_Control(place, "Verify now"), _Control(place, "Continue to verify")]

    step = _submit(place, box)

    assert place.pressed == [] and step.problem and "none was guessed at" in step.problem


# --- the bank's page in a real browser ---------------------------------------------
BANK = "https://bank.test"
CREQ = f"{BANK}/v1/acs/services/browser/creq/L/888"

#: HDFC as job 689 met it: the password's form has no submit control; the
#: page's own SUBMIT button (type="button") sends the password by script.
HDFC_LIKE = """<!doctype html><html><body>
<form id="f" method="post"><input type="password" id="pw" name="pw" maxlength="20">
<input type="hidden" name="threeDSSessionData" value="abc"></form>
<a href="#" onclick="return false">Resend OTP</a>
<button type="button">CANCEL</button>
<button type="button" id="go" onclick="window.sent = (window.sent || 0) + 1;
  fetch('/v1/acs/challenge', {method: 'POST', body: document.getElementById('pw').value});">SUBMIT</button>
</body></html>"""

#: 23 Nov's page: the form's one submit button posts the form, and the bank
#: takes longer to answer than any wait.
SLOW_FORM = """<!doctype html><html><body>
<form method="post" action="/v1/acs/challenge"><input type="password" id="pw" name="pw">
<button type="submit">Submit</button></form></body></html>"""

#: No submit control and no button: Enter would send the form the plain way.
NO_BUTTON = """<!doctype html><html><body>
<form method="post"><input type="password" id="pw" name="pw"></form></body></html>"""


def _in_chromium(html: str):
    """Load ``html`` as the bank's page, type and submit; the bank never
    answers a POST. Returns (step, posts, seconds, window.sent)."""
    from playwright.async_api import async_playwright

    async def go():
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Chromium cannot start here: {exc}")
            posts: list[str] = []
            held: list = []
            try:
                page = await browser.new_page()

                async def bank(route):
                    request = route.request
                    if request.method == "GET" and request.url.startswith(CREQ):
                        await route.fulfill(status=200, content_type="text/html", body=html)
                    elif request.method == "POST":
                        posts.append(request.url.split("?")[0])
                        held.append(route)  # never answered, as HDFC's server was not within 30 s
                    else:
                        await route.abort()

                await page.route(f"{BANK}/**", bank)
                await page.goto(CREQ)
                traffic = _BankTraffic()
                for event, handler in traffic.listeners():
                    page.on(event, handler)
                started = time.monotonic()
                step = await ordering._type_and_submit(page, page.locator("#pw"), PASSWORD, traffic)
                seconds = time.monotonic() - started
                sent_by_script = 0
                with contextlib.suppress(Exception):
                    # A page loading its next page answers no script: not waited for.
                    sent_by_script = await asyncio.wait_for(page.evaluate("window.sent || 0"), 2)
                return step, posts, seconds, sent_by_script, traffic
            finally:
                for route in held:
                    with contextlib.suppress(Exception):
                        await route.abort()
                await browser.close()

    return asyncio.run(go())


def test_in_chromium_hdfcs_page_is_sent_by_its_own_submit_button_without_waiting():
    step, posts, seconds, sent_by_script, traffic = _in_chromium(HDFC_LIKE)

    assert step.problem is None and step.sent, step
    assert "the page's Submit button" in step.how
    assert posts == [f"{BANK}/v1/acs/challenge"]  # once, by the page's own step; never the plain form
    assert sent_by_script == 1
    assert seconds < 8
    assert PASSWORD not in "\n".join(traffic.lines)


def test_in_chromium_a_slow_bank_is_not_taken_for_a_failed_submit_and_gets_one_post():
    step, posts, seconds, _, _ = _in_chromium(SLOW_FORM)

    assert step.problem is None and step.sent, step
    assert "the form's Submit button" in step.how
    assert posts == [f"{BANK}/v1/acs/challenge"]  # one sending, not a click and then an Enter
    assert seconds < 8


#: A page that sends its form by itself once the box is full.
SENDS_ITSELF = """<!doctype html><html><body>
<form method="post" action="/v1/acs/challenge"><input type="password" id="pw" name="pw" maxlength="9"
 oninput="if (this.value.length >= 9) this.form.submit()"><button type="submit">Submit</button></form>
</body></html>"""


def test_in_chromium_a_page_that_sends_itself_as_the_box_fills_gets_nothing_pressed():
    step, posts, seconds, _, _ = _in_chromium(SENDS_ITSELF)

    assert step.problem is None and step.sent, step
    assert "sent it by itself" in step.how
    assert posts == [f"{BANK}/v1/acs/challenge"]  # the page's own sending only; no Submit, no Enter
    assert seconds < 8


def test_in_chromium_a_form_with_no_button_is_not_sent_with_enter():
    step, posts, _, _, _ = _in_chromium(NO_BUTTON)

    assert posts == []
    assert step.problem and "Enter was not pressed" in step.problem


# --- what a card order that does not confirm leaves behind ------------------------
class _StuckBankPage(FakePage):
    """Pay lands on the bank, whose address carries what its form sent; the
    bank never sends the browser back to Flipkart, and its page is a bare 404."""

    def clicked(self, xpath: str) -> None:
        super().clicked(xpath)
        if xpath == S.CARD_PAY_BTN:
            self.url = f"{CREQ}?pw={PASSWORD}&threeDSSessionData=abc"


def test_an_unconfirmed_card_order_keeps_a_record_and_never_shows_the_password(tmp_path, monkeypatch):
    monkeypatch.setattr(ordering, "DIAGNOSTICS_DIR", tmp_path)
    page = _StuckBankPage(inner_text_map={S.ORDER_TOTAL: "₹17,707"})
    page.confirmation_url = None  # the bank never sends the browser back
    page.inner_text_by_url = {"bank.test": {S.PAGE_BODY: "404 Not Found\n\nnginx"}}

    result = _run(page, mode=PAYMENT_CARD, card=CARD)

    assert result["status"] == OrderStatus.unconfirmed
    assert PASSWORD not in str(result)
    assert f"the browser was on {CREQ})" in result["error"]  # the address without its query
    assert "ended on a bare error page ('404 Not Found')" in result["error"]
    records = list(tmp_path.glob("*-bank-step.txt"))
    assert len(records) == 1
    record = records[0].read_text(encoding="utf-8")
    assert "bank's page: bank.test" in record
    assert PASSWORD not in record and "4111111111111111" not in record


def test_a_cod_order_keeps_no_bank_record(tmp_path, monkeypatch):
    monkeypatch.setattr(ordering, "DIAGNOSTICS_DIR", tmp_path)
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹17,707"})
    page.confirmation_url = None  # never confirms

    result = _run(page)

    assert result["status"] == OrderStatus.unconfirmed
    assert list(tmp_path.glob("*-bank-step.txt")) == []


def test_a_bare_error_page_is_named_but_a_long_page_mentioning_a_number_is_not():
    page = FakePage()
    page.url = CREQ
    page.inner_text_map[S.PAGE_BODY] = "404 Not Found\n\nnginx"
    assert "('404 Not Found')" in asyncio.run(ordering._bank_authentication_blocked(page))

    page.inner_text_map[S.PAGE_BODY] = "500 points earned on this card. " + "Enter your password to continue. " * 12
    assert asyncio.run(ordering._bank_authentication_blocked(page)) == ""


def test_addresses_away_from_flipkart_are_shown_without_their_query():
    assert ordering._shown_url(f"{CREQ}?pw={PASSWORD}#x") == CREQ
    flipkart = "https://www.flipkart.com/viewcheckout?otracker=abc"
    assert ordering._shown_url(flipkart) == flipkart
