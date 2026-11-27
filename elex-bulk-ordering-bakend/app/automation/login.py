"""Login + session generation (ported from login-otp-generator/FlipkartWindow).

Opens the Flipkart login page, submits the email, triggers the OTP, reads it
from the configured inbox, verifies, and returns the resulting cookies for the
caller to persist via ``core.session_store``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from playwright.async_api import Page

from app.automation import challenges
from app.automation.otp import NullOtpReader, OtpNotConfigured, OtpReader
from app.core.step_timer import mark as step_mark
from app.config import get_settings
from app.core import selectors as S

log = logging.getLogger("ordering.login")


async def run_login(
    page: Page,
    email: str,
    otp_reader: OtpReader | None = None,
    *,
    window_wait: int = 10,
    otp_wait: int = 60,
) -> list[dict]:
    """Drive the login flow for one account. Returns the account's cookies.

    If no OTP reader is configured, the email is submitted and the OTP step is
    skipped (cookies are still returned) so the flow degrades gracefully.
    """
    otp_reader = otp_reader or NullOtpReader()

    await page.goto(S.LOGIN_URL)
    step_mark("open login page")
    await challenges.guard(page, site=challenges.FLIPKART, step="sign-in page")

    # Flipkart opens on PHONE login; the email field does not exist until this
    # link is clicked. Confirmed live 2026-10-23 — without it we were typing an
    # email address into a number input, so login could never have succeeded.
    try:
        toggle = page.locator(f"xpath={S.LOGIN_USE_EMAIL}")
        await toggle.wait_for(timeout=window_wait * 1000)
        await toggle.click()
    except Exception as exc:  # noqa: BLE001
        # Already on the email form (or the link moved) — carry on and let the
        # email field's own wait decide whether we are really stuck.
        log.info("email-login toggle not clicked (%s); continuing", exc)

    email_box = page.locator(f"xpath={S.LOGIN_EMAIL}")
    await email_box.wait_for(timeout=window_wait * 1000)
    await email_box.fill(email)
    # Stamp the request time BEFORE asking, so an older code sitting in the
    # mailbox cannot be mistaken for this one. A minute of slack absorbs clock
    # skew between this machine and the mail server.
    requested_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await page.locator(f"xpath={S.SEND_OTP_BTN}").click()
    step_mark("request OTP")
    # Flipkart puts a captcha here when it doubts the sign-in; a person
    # answers it on the Captchas page and the OTP is sent after it.
    await challenges.guard(page, site=challenges.FLIPKART, step="request OTP")

    # Flipkart answers an email it has no account for with "Looks like you're
    # new here! Sign up with your number" and sends no code (seen live on
    # 14 Nov 2026). Waiting a minute for an OTP that will never come only
    # hides that, so it is said at once.
    if await email_not_registered(page):
        log.error("%s is not registered on Flipkart — no OTP is sent", email)
        raise NotRegistered(email)
    refusal = await login_refusal(page)
    if refusal is not None:
        log.error("Flipkart refuses %s: %s", email, refusal.reason)
        raise refusal

    inbox_set_up = not isinstance(otp_reader, NullOtpReader)
    try:
        code = await otp_reader.get_otp(
            email, timeout=otp_wait, not_before=requested_at
        )
    except OtpNotConfigured:
        log.warning("OTP reader not configured; skipping OTP entry for %s", email)
        code = None
        inbox_set_up = False
    step_mark("wait for OTP" if code else "wait for OTP (none came)")

    if code:
        if get_settings().otp_log_codes:
            # Opt-in debug aid (ORDERING_OTP_LOG_CODES=true). Proves the inbox
            # is being read; turn it back off once setup is confirmed.
            log.info("OTP received for %s: %s", email, code)
        else:
            log.info("OTP received for %s (%d digits)", email, len(code))
        await _enter_otp(page, code)
        try:
            # Verify is disabled until every box holds a digit, so a timeout
            # here means the digits did not land — not that the page is slow.
            await page.locator(f"xpath={S.VERIFY_BTN}").click(timeout=20_000)
            log.info("OTP submitted for %s", email)
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Verify never became clickable for %s — the OTP boxes were "
                "probably not filled. %s",
                email,
                exc,
            )
    else:
        log.warning("No OTP obtained for %s; session may be unauthenticated", email)
        if inbox_set_up:
            # The inbox was read and no code came, so this browser cannot be
            # signed in: checking would only cost ~27 s of waiting for a
            # redirect that never comes. (With no inbox set up the check still
            # runs: a person may have signed in in the visible window.)
            log.error("login did not complete for %s — no OTP arrived, not saving a session", email)
            return None

    # Only now ask Flipkart whether we are actually signed in. Returning cookies
    # unconditionally was a real bug: a login that never completed still got
    # saved and the account was marked session_valid, so the dashboard showed a
    # healthy green count while every later coupon/order run quietly failed
    # against a logged-out browser.
    step_mark("enter OTP")
    await challenges.guard(page, site=challenges.FLIPKART, step="after the OTP")
    if not await verify_logged_in(page):
        refusal = await login_refusal(page)
        if refusal is not None:
            log.error("Flipkart refuses %s: %s", email, refusal.reason)
            raise refusal
        log.error("login did not complete for %s — not saving a session", email)
        return None

    log.info("login confirmed for %s", email)
    return await page.context.cookies()


class NotRegistered(RuntimeError):
    """Flipkart has no account for this email, so it sends no OTP."""


class LoginRefused(RuntimeError):
    """Flipkart answered the sign-in with a refusal that says why: the account
    is blocked, or it has had too many attempts. ``status`` is the account
    status it maps to (an ``AccountStatus`` value)."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


#: Flipkart's wording for an account it will not sign in, lower-case, by the
#: account status each means. VERIFY: put together from the wording Flipkart
#: and BulkOrderBuddy use ("Blocked - Maximum Login Attempts Reached", "Account
#: Blocked"); confirm on an account that shows one, as with every selector.
#: Until 19 Nov 2026 every such refusal read as "session invalid", so the
#: dashboard's Blocked and Multiple attempts columns could never rise above 0.
LOGIN_REFUSALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "multiple_attempts",
        (
            "maximum number of attempts", "maximum attempts", "too many attempts",
            "exceeded the maximum", "attempts exceeded", "maximum login attempts",
            "too many requests",
        ),
    ),
    (
        "blocked",
        (
            "account has been blocked", "account is blocked", "account blocked",
            "account has been suspended", "account is suspended", "account has been deactivated",
            "account is deactivated", "account is disabled",
        ),
    ),
)


def refusal_in_text(text: str | None) -> LoginRefused | None:
    """The refusal a sign-in page's text states, or None."""
    body = (text or "").lower()
    for status, phrases in LOGIN_REFUSALS:
        for phrase in phrases:
            if phrase in body:
                return LoginRefused(status, f"Flipkart says: \"{phrase}\"")
    return None


async def login_refusal(page: Page) -> LoginRefused | None:
    """Read the page once for a stated refusal. Never raises."""
    try:
        body = await page.inner_text("body", timeout=2_000)
    except Exception:  # noqa: BLE001 — unreadable is no refusal
        return None
    return refusal_in_text(body)


#: Flipkart's answer to an email it has no account for.
NOT_REGISTERED_TEXT = ("looks like you're new here", "looks like you’re new here")


async def email_not_registered(page: Page, *, wait_ms: int = 4_000) -> bool:
    """True when the page answers the submitted email with "new here"."""
    waited = 0
    while waited <= wait_ms:
        try:
            body = (await page.inner_text("body", timeout=2_000)).lower()
        except Exception:  # noqa: BLE001 — unreadable is not "new here"
            body = ""
        if any(text in body for text in NOT_REGISTERED_TEXT):
            return True
        if "otp" in body or "verify" in body:
            return False
        await page.wait_for_timeout(500)
        waited += 500
    return False


async def verify_logged_in(page: Page, *, settle_ms: int = 15_000) -> bool:
    """True only if Flipkart serves an account page instead of the login screen.

    Checking the cookie jar is not enough — Flipkart sets auth-looking cookies
    (``at``, ``T``) before verification too, so the only trustworthy test is
    asking for a page that requires a session and seeing whether it bounces.

    Clicking Verify starts Flipkart's own post-login redirect. Navigating while
    that is in flight cancels it and the check fails with ERR_ABORTED — looking
    like a failed login when the login actually worked. So wait for the page to
    settle first, and retry once if a navigation still races us.
    """
    # Flipkart leaves the OTP page (/login/verify) once it has accepted the
    # code. Waiting for that is the reliable signal: on 15 Nov 2026 a login
    # Flipkart accepted (it emailed "New device login detected") was judged
    # failed because the load-state wait below returned at once and the check
    # ran while the redirect was still setting the session cookies.
    waited = 0
    while waited < settle_ms and "/login" in (page.url or ""):
        await page.wait_for_timeout(500)
        waited += 500

    # Let the post-verify redirect finish before we navigate anywhere.
    for state in ("networkidle", "load"):
        try:
            await page.wait_for_load_state(state, timeout=settle_ms)
            break
        except Exception:  # noqa: BLE001 — fall through to the next signal
            continue

    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            await page.goto(S.ORDERS_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            if "/login" not in (page.url or ""):
                return True
            last_error = None
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if attempt == 1:
            # A bounce right after Verify can be the session still settling:
            # one more look a few seconds later before calling it a failure.
            await page.wait_for_timeout(4000)

    if last_error is not None:
        log.error("could not confirm login state: %s", last_error)
    return False


async def _enter_otp(page: Page, code: str) -> None:
    """Type the code across Flipkart's six single-digit boxes.

    Confirmed live 2026-10-23: the OTP screen renders one input per digit, and
    the Verify button stays disabled until every box is filled. Falls back to a
    single field if Flipkart ever changes back, so this keeps working either way.
    """
    digits = [c for c in (code or "") if c.isdigit()]
    boxes = page.locator(f"xpath={S.OTP_DIGIT_BOXES}")
    try:
        count = await boxes.count()
    except Exception:  # noqa: BLE001
        count = 0

    if count <= 1:
        # Single-field form (older layout, or a future change back).
        await boxes.first.fill("".join(digits) or code)
        return

    if len(digits) != count:
        log.warning("OTP has %d digits but the form shows %d boxes", len(digits), count)

    # These boxes advance focus themselves as you type. Setting each one's
    # value directly fights that: the component moves focus mid-write and
    # digits land in the wrong box or nowhere, leaving Verify disabled — which
    # looks like a hang, because Playwright waits for the button to enable.
    # So drive it like a person does: focus the first box and type.
    await boxes.first.click()
    await page.keyboard.type("".join(digits), delay=60)

    # Typing can still drop a digit if the component re-renders mid-sequence,
    # so check and patch per box rather than trusting it.
    missing = await _empty_boxes(boxes, count)
    if missing:
        log.warning("OTP boxes %s empty after typing; filling them directly", missing)
        for index in missing:
            if index < len(digits):
                await boxes.nth(index).fill(digits[index])


async def _empty_boxes(boxes, count: int) -> list[int]:
    """Indexes of OTP boxes that did not take a digit."""
    empty: list[int] = []
    for index in range(count):
        try:
            value = await boxes.nth(index).input_value()
        except Exception:  # noqa: BLE001
            continue
        if not (value or "").strip():
            empty.append(index)
    return empty
