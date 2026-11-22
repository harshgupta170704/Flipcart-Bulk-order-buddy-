"""HDFC CorpAssist portal (corpcardassist.hdfc.bank.in), driven with Playwright.

What it does, each from the screens of the owner's recording of 21 Nov 2026:

* **Sign in** with the portal user and password; the OTP the portal sends is
  read from the Employee Phone registered for ``hdfc_portal_otp_mobile``
  (app.core.sms_relay) or, failing that, from the inbox of
  ``hdfc_portal_otp_email`` (app.automation.otp). The sign-in page itself is
  not in the recording, so it is found by its fields (a password input, an OTP
  input) rather than by fixed selectors, and a captcha stops it with a reason.
* **Create Virtual Card (Bulk)** (``/create-virtual-card-bulk-new``): attach
  the sheet (app.io.hdfc_bulk_csv), Submit, Confirm. "Upload Failed!" is
  answered by downloading the portal's output file and reporting its rows.
* **Virtual Card User** (``/all-beneficiary-users``): read every user (ID,
  username, contact email, mobile, status), page by page.
* **Edit Virtual Card User** (``/edit-new-beneficiary-user``): change a user's
  email, which is how the owner points a user's card mails at the shared inbox.

One portal session at a time (:data:`_LOCK`); the signed-in browser state is
kept in memory for :data:`SESSION_MINUTES` so back-to-back work does not ask
for an OTP each time. Nothing is written to disk. Logs name pages and counts,
never a card number, a password or an OTP.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

log = logging.getLogger("ordering.hdfc_portal")

DEFAULT_URL = "https://corpcardassist.hdfc.bank.in"
DASHBOARD = "/dashboard"
USERS = "/all-beneficiary-users"
BULK_CREATE = "/create-virtual-card-bulk-new"
CARD_DETAILS = "/virtual-card-details"
#: How long a signed-in browser state is reused.
SESSION_MINUTES = 10

_LOCK = asyncio.Lock()
_STATE: dict = {"storage": None, "at": 0.0}


class PortalError(RuntimeError):
    """The portal could not do what was asked; the message is for the page."""


class PortalNotConfigured(PortalError):
    pass


@dataclass(frozen=True)
class PortalUser:
    portal_user_id: str
    username: str
    email: str
    mobile: str | None
    active: bool


@dataclass
class BulkResult:
    ok: bool
    #: The portal's words, for the batch: a success line or why it refused.
    detail: str
    #: One line per refused row, from the output file.
    report: list[str] = field(default_factory=list)


class Portal(Protocol):
    async def test_login(self) -> str: ...

    async def bulk_create(self, sheet: bytes, file_name: str) -> BulkResult: ...

    async def list_users(self) -> list[PortalUser]: ...

    async def set_user_email(self, username: str, email: str) -> None: ...


@dataclass(frozen=True)
class PortalConfig:
    url: str
    user: str
    password: str
    otp_mobile: str | None
    otp_email: str | None
    otp_sender: str
    headless: bool
    step_seconds: int

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password)

    @property
    def otp_via(self) -> str | None:
        if self.otp_mobile:
            return "sms"
        if self.otp_email:
            return "email"
        return None


def config_from(settings) -> PortalConfig:
    return PortalConfig(
        url=(getattr(settings, "hdfc_portal_url", "") or DEFAULT_URL).rstrip("/"),
        user=getattr(settings, "hdfc_portal_user", "") or "",
        password=getattr(settings, "hdfc_portal_password", "") or "",
        otp_mobile=(getattr(settings, "hdfc_portal_otp_mobile", "") or "").strip() or None,
        otp_email=(getattr(settings, "hdfc_portal_otp_email", "") or "").strip() or None,
        otp_sender=getattr(settings, "hdfc_portal_otp_sender", "HDFC") or "",
        headless=bool(getattr(settings, "browser_headless", True)),
        step_seconds=int(getattr(settings, "hdfc_portal_step_seconds", 60) or 60),
    )


# --- the browser side ------------------------------------------------------------

_OTP_INPUT = (
    "input[autocomplete='one-time-code'], input[name*='otp' i], input[id*='otp' i], "
    "input[placeholder*='otp' i], input[aria-label*='otp' i]"
)
_USER_INPUT = (
    "input[name*='user' i], input[id*='user' i], input[placeholder*='user' i], "
    "input[name*='login' i], input[id*='login' i], input[placeholder*='customer' i]"
)
_SUBMIT_WORDS = re.compile(r"^\s*(log\s*in|login|sign\s*in|submit|proceed|continue|verify|next)\s*$", re.IGNORECASE)


async def _click_button(page, pattern: re.Pattern | str, *, timeout: float = 5000) -> bool:
    """Click the first visible enabled button whose name matches; False when none."""
    locator = page.get_by_role("button", name=pattern)
    try:
        await locator.first.wait_for(state="visible", timeout=timeout)
    except Exception:  # noqa: BLE001
        return False
    count = await locator.count()
    for i in range(count):
        button = locator.nth(i)
        try:
            if await button.is_visible() and await button.is_enabled():
                await button.click()
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _signed_in(page) -> bool:
    if DASHBOARD in page.url or USERS in page.url:
        return True
    try:
        return await page.get_by_text("Audit Trail", exact=True).first.is_visible()
    except Exception:  # noqa: BLE001
        return False


class PlaywrightPortal:
    """The real portal. Each public method opens a browser, signs in (or reuses
    a recent session), does its one job and closes the browser."""

    def __init__(self, config: PortalConfig, session_factory=None) -> None:
        self.config = config
        self._session_factory = session_factory

    # -- OTP ----------------------------------------------------------------
    async def _otp(self, asked_at: datetime) -> str:
        timeout = max(60, self.config.step_seconds * 2)
        if self.config.otp_mobile:
            from app.core.sms_relay import NoPhoneForNumber, SmsOtpReader

            reader = SmsOtpReader(self._session_factory, sender_like=(self.config.otp_sender,) if self.config.otp_sender else ())
            try:
                code = await reader.get_otp(self.config.otp_mobile, timeout=timeout, not_before=asked_at)
            except NoPhoneForNumber as exc:
                raise PortalError(f"the portal's OTP goes to a mobile with no Employee Phone: {exc}") from None
            if code:
                return code
            if not self.config.otp_email:
                raise PortalError(
                    f"no OTP reached the Employee Phone for …{self.config.otp_mobile[-2:]} within {timeout} s: "
                    "check the phone's SMS forwarder is running"
                )
        if self.config.otp_email:
            from app.automation.otp import OtpNotConfigured, get_otp_reader
            from app.config import get_settings

            try:
                if self._session_factory is not None:
                    from app.core import corpassist

                    with self._session_factory() as db:
                        eff = corpassist.effective_settings(db)
                else:
                    eff = get_settings()
                reader = get_otp_reader(eff)
                code = await reader.get_otp(self.config.otp_email, timeout=timeout, not_before=asked_at)
            except OtpNotConfigured:
                raise PortalError("the portal's OTP goes by email, but no OTP inbox is set up") from None
            if code:
                return code
            raise PortalError(f"no OTP email reached the inbox within {timeout} s")
        raise PortalError(
            "the portal asked for an OTP and no way to read it is set: set ORDERING_HDFC_PORTAL_OTP_MOBILE "
            "(an Employee Phone) or ORDERING_HDFC_PORTAL_OTP_EMAIL"
        )

    # -- sign in ------------------------------------------------------------
    async def _sign_in(self, page) -> None:
        step = self.config.step_seconds * 1000
        await page.goto(self.config.url + DASHBOARD, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=step)
        if await _signed_in(page):
            return
        if await page.locator("img[src*='captcha' i], [id*='captcha' i], [class*='captcha' i]").count():
            raise PortalError("the portal's sign-in asks for a captcha, which is not automated: sign in by hand")

        password = page.locator("input[type='password']").first
        try:
            await password.wait_for(state="visible", timeout=step)
        except Exception:  # noqa: BLE001
            raise PortalError("the portal's sign-in page did not show a password field") from None
        user = page.locator(_USER_INPUT).first
        if not await user.count():
            user = page.locator("input[type='text'], input[type='email'], input:not([type])").first
        await user.fill(self.config.user)
        await password.fill(self.config.password)
        asked_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)
        if not await _click_button(page, _SUBMIT_WORDS):
            await password.press("Enter")

        # Either the dashboard (no OTP asked) or an OTP field.
        deadline = time.monotonic() + self.config.step_seconds
        otp_box = page.locator(_OTP_INPUT)
        boxes = page.locator("input[maxlength='1']")
        while time.monotonic() < deadline:
            if await _signed_in(page):
                return
            if await otp_box.count() or await boxes.count() >= 4:
                break
            error = page.locator("[role='alert'], .error, .toast-error, .invalid-feedback")
            if await error.count():
                text = (await error.first.inner_text()).strip()
                if text:
                    raise PortalError(f"the portal refused the sign-in: {text[:200]}")
            await asyncio.sleep(0.5)
        else:
            raise PortalError("after the password, the portal showed neither the dashboard nor an OTP field")

        code = await self._otp(asked_at)
        if await otp_box.count():
            await otp_box.first.fill(code)
        else:
            count = await boxes.count()
            for i, digit in enumerate(code[:count]):
                await boxes.nth(i).fill(digit)
        if not await _click_button(page, _SUBMIT_WORDS):
            await page.keyboard.press("Enter")
        try:
            await page.wait_for_url(re.compile(re.escape(DASHBOARD)), timeout=step)
        except Exception:  # noqa: BLE001
            if not await _signed_in(page):
                raise PortalError("the portal did not open the dashboard after the OTP (wrong or expired OTP?)") from None
        log.info("HDFC portal: signed in")

    async def _with_page(self, work):
        from playwright.async_api import async_playwright

        if not self.config.configured:
            raise PortalNotConfigured(
                "the HDFC portal sign-in is not set: ORDERING_HDFC_PORTAL_USER and ORDERING_HDFC_PORTAL_PASSWORD"
            )
        async with _LOCK:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=self.config.headless)
                try:
                    fresh = _STATE["storage"] and time.monotonic() - _STATE["at"] < SESSION_MINUTES * 60
                    context = await browser.new_context(
                        accept_downloads=True, storage_state=_STATE["storage"] if fresh else None
                    )
                    page = await context.new_page()
                    page.set_default_timeout(self.config.step_seconds * 1000)
                    await self._sign_in(page)
                    result = await work(page)
                    _STATE["storage"] = await context.storage_state()
                    _STATE["at"] = time.monotonic()
                    return result
                except PortalError:
                    _STATE["storage"] = None
                    raise
                except Exception as exc:  # noqa: BLE001 — a portal change is reported, not a 500
                    _STATE["storage"] = None
                    log.warning("HDFC portal: %s", type(exc).__name__)
                    raise PortalError(f"the portal did not behave as expected ({type(exc).__name__})") from None
                finally:
                    await browser.close()

    # -- the jobs -------------------------------------------------------------
    async def test_login(self) -> str:
        async def work(page) -> str:
            return "signed in to the HDFC portal"

        return await self._with_page(work)

    async def bulk_create(self, sheet: bytes, file_name: str) -> BulkResult:
        async def work(page) -> BulkResult:
            step = self.config.step_seconds * 1000
            await page.goto(self.config.url + BULK_CREATE, wait_until="domcontentloaded")
            await page.get_by_text("Create Virtual Card (Bulk)").first.wait_for(timeout=step)
            await page.locator("input[type='file']").first.set_input_files(
                {"name": file_name, "mimeType": "text/csv", "buffer": sheet}
            )
            failed = await self._upload_failed(page, timeout=4000)
            if failed:
                return failed
            if not await _click_button(page, re.compile(r"^\s*submit\s*$", re.IGNORECASE), timeout=step):
                return BulkResult(False, "the portal's Submit button stayed disabled after the sheet was attached")
            await _click_button(page, re.compile(r"^\s*(confirm|yes|ok)\s*$", re.IGNORECASE), timeout=5000)
            failed = await self._upload_failed(page, timeout=3000)
            if failed:
                return failed
            deadline = time.monotonic() + self.config.step_seconds
            while time.monotonic() < deadline:
                if CARD_DETAILS in page.url:
                    return BulkResult(True, "the portal took the sheet (Virtual Card Details opened)")
                message = page.get_by_text(re.compile(r"success|submitted|request (has been )?(raised|created)", re.IGNORECASE))
                if await message.count():
                    return BulkResult(True, (await message.first.inner_text()).strip()[:200])
                failed = await self._upload_failed(page, timeout=500)
                if failed:
                    return failed
                await asyncio.sleep(0.5)
            return BulkResult(
                True,
                "the portal gave no clear answer after Submit: check Virtual Card Details; Fetch will show the cards if they were made",
            )

        return await self._with_page(work)

    async def _upload_failed(self, page, *, timeout: float) -> BulkResult | None:
        banner = page.get_by_text(re.compile(r"upload failed", re.IGNORECASE))
        try:
            await banner.first.wait_for(state="visible", timeout=timeout)
        except Exception:  # noqa: BLE001
            return None
        report: list[str] = []
        try:
            async with page.expect_download(timeout=15000) as info:
                await page.get_by_role("button", name=re.compile(r"download output file", re.IGNORECASE)).first.click()
            download = await info.value
            path = await download.path()
            if path:
                from app.io.hdfc_bulk_csv import read_output_file

                with open(path, "rb") as fh:
                    report = read_output_file(fh.read())
        except Exception:  # noqa: BLE001
            report = ["the output file could not be read"]
        return BulkResult(False, "the portal refused the sheet (Upload Failed)", report)

    async def list_users(self) -> list[PortalUser]:
        async def work(page) -> list[PortalUser]:
            await page.goto(self.config.url + USERS, wait_until="domcontentloaded")
            await page.get_by_text("Virtual Card User").first.wait_for()
            await page.locator("table tbody tr").first.wait_for()
            seen: dict[str, PortalUser] = {}
            for _ in range(100):
                for user in parse_users_table(await _table_rows(page)):
                    seen.setdefault(user.portal_user_id or user.username, user)
                nxt = page.get_by_text(re.compile(r"^\s*Next\s*›?\s*$"))
                if not await nxt.count():
                    break
                link = nxt.first
                classes = (await link.get_attribute("class") or "") + (await link.get_attribute("aria-disabled") or "")
                if "disabled" in classes.lower() or "true" in classes.lower():
                    break
                first_before = await page.locator("table tbody tr").first.inner_text()
                await link.click()
                await asyncio.sleep(1.0)
                if await page.locator("table tbody tr").first.inner_text() == first_before:
                    break
            log.info("HDFC portal: %d virtual card user(s) read", len(seen))
            return list(seen.values())

        return await self._with_page(work)

    async def set_user_email(self, username: str, email: str) -> None:
        async def work(page) -> None:
            await page.goto(self.config.url + USERS, wait_until="domcontentloaded")
            search = page.get_by_placeholder(re.compile("search", re.IGNORECASE)).first
            if await search.count():
                await search.fill(username)
                await _click_button(page, re.compile(r"^\s*search\s*$", re.IGNORECASE), timeout=3000)
                await asyncio.sleep(1.0)
            row = page.locator("table tbody tr").filter(has_text=re.compile(rf"\b{re.escape(username)}\b", re.IGNORECASE))
            if not await row.count():
                raise PortalError(f"no Virtual Card User named {username!r} on the portal")
            await row.first.get_by_text("Edit", exact=True).click()
            await page.get_by_text("Edit Virtual Card User").first.wait_for()
            box = page.get_by_label(re.compile(r"email", re.IGNORECASE))
            if not await box.count():
                box = page.locator("input[type='email'], input[name*='email' i], input[placeholder*='email' i]")
            await box.first.fill(email)
            if not await _click_button(page, re.compile(r"^\s*submit\s*$", re.IGNORECASE)):
                raise PortalError("the portal's Submit stayed disabled after the email was changed")
            if not await _click_button(page, re.compile(r"^\s*confirm\s*$", re.IGNORECASE)):
                raise PortalError("the portal did not ask to confirm the change")
            await page.wait_for_url(re.compile(re.escape(USERS)))
            log.info("HDFC portal: a virtual card user's email was changed")

        await self._with_page(work)


async def _table_rows(page) -> list[dict[str, str]]:
    """The visible table as a list of {header: cell text}."""
    return await page.evaluate(
        """() => {
            const table = document.querySelector('table');
            if (!table) return [];
            const heads = [...table.querySelectorAll('thead th')].map(th => th.innerText.trim());
            return [...table.querySelectorAll('tbody tr')].map(tr => {
                const cells = [...tr.querySelectorAll('td')].map(td => td.innerText.trim());
                const row = {};
                heads.forEach((h, i) => { row[h] = cells[i] || ''; });
                return row;
            });
        }"""
    )


def _pick(row: dict[str, str], *names: str) -> str:
    wanted = {re.sub(r"[^a-z]", "", n.lower()) for n in names}
    for key, value in row.items():
        if re.sub(r"[^a-z]", "", key.lower()) in wanted:
            return value.strip()
    return ""


def parse_users_table(rows: list[dict[str, str]]) -> list[PortalUser]:
    """The portal's Virtual Card User rows (headers as the page shows them)."""
    users: list[PortalUser] = []
    for row in rows:
        username = _pick(row, "Virtual Card Username", "Username")
        if not username:
            continue
        mobile = re.sub(r"\D", "", _pick(row, "Mobile No.", "Mobile Number", "Mobile"))
        users.append(
            PortalUser(
                portal_user_id=_pick(row, "Virtual Card User ID", "User ID"),
                username=username,
                email=_pick(row, "Contact Email", "Email ID", "Email").lower(),
                mobile=mobile[-10:] if len(mobile) >= 10 else None,
                active=_pick(row, "Status").lower() != "inactive",
            )
        )
    return users


def users_from_csv(raw: bytes) -> list[PortalUser]:
    """The same rows from the table exported as CSV (used by tests and by hand)."""
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig", errors="replace")))
    return parse_users_table([dict(r) for r in reader])


def portal(settings=None, session_factory=None) -> Portal:
    """The portal for these settings (DB-backed effective settings by default)."""
    if settings is None:
        if session_factory is not None:
            from app.core import corpassist

            with session_factory() as db:
                settings = corpassist.effective_settings(db)
        else:
            from app.config import get_settings

            settings = get_settings()
    return PlaywrightPortal(config_from(settings), session_factory)


def busy() -> bool:
    return _LOCK.locked()
