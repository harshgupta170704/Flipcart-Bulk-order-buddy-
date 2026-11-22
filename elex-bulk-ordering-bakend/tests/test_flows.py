"""Tests for the login and coupon flows over a fake page, plus session store."""

import asyncio

from app.automation import coupons as coupon_flow
from app.automation import login as login_flow
from app.automation.otp import NullOtpReader
from app.core import selectors as S
from app.core import session_store
from app.db.models import Account, AccountStatus
from tests.fakes import FakePage


# --- login -------------------------------------------------------------------
def test_login_switches_to_email_before_typing_an_email():
    """Flipkart opens on PHONE login (confirmed live 2026-10-23).

    The email field does not exist until "Use Email-ID" is clicked, so skipping
    that click means typing an address into a number input — login could never
    succeed. This guards the click specifically.
    """
    page = FakePage()
    asyncio.run(login_flow.run_login(page, "a@test.com", NullOtpReader()))

    assert S.LOGIN_USE_EMAIL in page.clicks
    # ...and the toggle happens BEFORE the email is filled.
    assert page.clicks.index(S.LOGIN_USE_EMAIL) < page.clicks.index(S.SEND_OTP_BTN)


def test_login_survives_a_missing_email_toggle():
    """If the toggle isn't there (already on the email form), login continues."""
    page = FakePage()
    page.fail_clicks_on = {S.LOGIN_USE_EMAIL}
    cookies = asyncio.run(login_flow.run_login(page, "a@test.com", NullOtpReader()))

    assert page.fills[S.LOGIN_EMAIL] == "a@test.com"
    assert cookies == [{"name": "SN", "value": "1"}]


def test_login_fills_email_and_returns_cookies():
    page = FakePage()
    cookies = asyncio.run(login_flow.run_login(page, "a@test.com", NullOtpReader()))

    assert page.fills[S.LOGIN_EMAIL] == "a@test.com"
    assert S.SEND_OTP_BTN in page.clicks
    # No OTP reader configured -> no digit box is filled, but cookies return.
    assert not any(k.startswith(S.OTP_DIGIT_BOXES) for k in page.fills)
    assert S.VERIFY_BTN not in page.clicks
    assert cookies == [{"name": "SN", "value": "1"}]


def test_otp_is_typed_across_the_six_digit_boxes():
    """Flipkart renders one input per digit (confirmed live 2026-10-23).

    A single .fill("123456") would put the whole code in box one and leave
    Verify disabled, so each digit must land in its own box.
    """

    class SixDigitReader(NullOtpReader):
        async def get_otp(self, address, *, timeout=60, not_before=None):
            return "483910"

    page = FakePage()
    page.counts[S.OTP_DIGIT_BOXES] = 6
    page.otp_xpath = S.OTP_DIGIT_BOXES
    asyncio.run(login_flow.run_login(page, "a@test.com", SixDigitReader()))

    # Typed as a person would, letting the widget advance focus itself.
    assert page.typed == "483910"

    for index, digit in enumerate("483910"):
        assert page.fills[f"{S.OTP_DIGIT_BOXES}[{index}]"] == digit
    assert S.VERIFY_BTN in page.clicks


def test_otp_falls_back_to_a_single_field():
    """If Flipkart ever goes back to one input, the whole code goes in it."""

    class Reader(NullOtpReader):
        async def get_otp(self, address, *, timeout=60, not_before=None):
            return "483910"

    page = FakePage()  # count defaults to 1
    asyncio.run(login_flow.run_login(page, "a@test.com", Reader()))

    assert page.fills[S.OTP_DIGIT_BOXES] == "483910"
    assert S.VERIFY_BTN in page.clicks


# --- coupons -----------------------------------------------------------------
def test_coupon_claim_successful():
    page = FakePage(inner_text_map={S.COUPON_SUCCESS: "Claim Successful "})
    result = asyncio.run(coupon_flow.apply_coupon(page, "SAVE10"))
    assert result == coupon_flow.CLAIMED
    assert page.fills[S.COUPON_INPUT] == "SAVE10"


def test_coupon_already_used():
    page = FakePage(
        inner_text_map={S.COUPON_ALREADY_USED: "The code has already been used."}
    )
    result = asyncio.run(coupon_flow.apply_coupon(page, "SAVE10"))
    assert result == coupon_flow.ALREADY_USED


def test_coupon_error_when_no_message():
    page = FakePage()  # no success/already-used text
    result = asyncio.run(coupon_flow.apply_coupon(page, "SAVE10"))
    assert result == coupon_flow.ERROR


# --- session store -----------------------------------------------------------
def test_session_store_save_and_load(db):
    account = Account(email="a@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()

    assert session_store.has_valid_session(db, account) is False

    cookies = [{"name": "c1", "value": "v1"}]
    session_store.save_session(db, account, cookies)

    assert session_store.has_valid_session(db, account) is True
    assert session_store.load_cookies(db, account) == cookies
    assert account.status == AccountStatus.session_valid


def test_session_store_resave_invalidates_old(db):
    account = Account(email="b@test.com", status=AccountStatus.new)
    db.add(account)
    db.commit()

    session_store.save_session(db, account, [{"name": "old"}])
    session_store.save_session(db, account, [{"name": "new"}])

    # only the newest is returned, and only one valid session remains
    assert session_store.load_cookies(db, account) == [{"name": "new"}]
    valid = [s for s in account.sessions if s.valid]
    assert len(valid) == 1


def test_a_superseded_cookie_set_is_deleted_not_kept(db):
    """NFR-003 hardening: a replaced cookie set may still sign in to Flipkart,
    and nothing reads it once replaced, so it leaves the database — including
    one an earlier check marked invalid — and other accounts' sets stay."""
    from app.db.models import Session as SessionModel

    account = Account(email="c@test.com", status=AccountStatus.new)
    other = Account(email="d@test.com", status=AccountStatus.new)
    db.add_all([account, other])
    db.commit()
    session_store.save_session(db, other, [{"name": "theirs"}])
    session_store.save_session(db, account, [{"name": "first"}])
    for stored in account.sessions:  # what `app.cli session-check --fix` does to a dead one
        stored.valid = False
    db.commit()
    session_store.save_session(db, account, [{"name": "second"}])

    session_store.save_session(db, account, [{"name": "third"}])

    db.expire_all()
    kept = db.query(SessionModel).order_by(SessionModel.id).all()
    assert [(row.account_id, row.cookies, row.valid) for row in kept] == [
        (other.id, [{"name": "theirs"}], True),
        (account.id, [{"name": "third"}], True),
    ]
    assert session_store.load_cookies(db, account) == [{"name": "third"}]
    assert session_store.load_cookies(db, other) == [{"name": "theirs"}]


# --- a login that did not actually complete (found live 2026-10-23) ----------
LOGIN_PAGE = "https://www.flipkart.com/login?ret=%2Faccount%2Forders"


def test_no_session_is_saved_when_flipkart_still_shows_the_login_page():
    """The bug: cookies were returned whether or not the OTP step worked.

    Flipkart sets auth-looking cookies (`at`, `T`) BEFORE verification, so a
    failed login still produced a plausible-looking cookie jar. It was saved,
    the account was marked session_valid, and every later coupon/order run
    failed against a logged-out browser with no clue why.
    """
    page = FakePage()
    page.redirect_to = LOGIN_PAGE  # bounced => not authenticated

    cookies = asyncio.run(login_flow.run_login(page, "a@test.com", NullOtpReader()))

    assert cookies is None  # nothing to save


def test_cookies_are_returned_when_the_account_page_loads():
    page = FakePage()
    page.redirect_to = "https://www.flipkart.com/account/orders"

    cookies = asyncio.run(login_flow.run_login(page, "a@test.com", NullOtpReader()))

    assert cookies == [{"name": "SN", "value": "1"}]


def test_verify_logged_in_checks_the_account_page_not_the_cookie_jar():
    page = FakePage()
    page.redirect_to = LOGIN_PAGE
    assert asyncio.run(login_flow.verify_logged_in(page)) is False
    # and it asked for a page that requires a session
    assert any("account/orders" in u for u in page.goto_urls)


def test_verification_retries_when_a_redirect_aborts_the_first_check():
    """Clicking Verify starts Flipkart's own redirect.

    Navigating into that cancels it (net::ERR_ABORTED) and the login looks
    failed when it actually worked, so the check retries once.
    """
    page = FakePage()
    page.redirect_to = "https://www.flipkart.com/account/orders"

    calls = {"n": 0}
    original = page.goto

    async def flaky_goto(url, wait_until=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("net::ERR_ABORTED")
        await original(url, wait_until=wait_until)

    page.goto = flaky_goto
    assert asyncio.run(login_flow.verify_logged_in(page)) is True
    assert calls["n"] == 2  # retried rather than giving up
