"""InstAddr (m.kuku.lu): the web mailbox that holds the accounts' addresses.

InstAddr has no API and no IMAP, so it is read the way its own web page reads
it: in a browser, signed in to the owner's InstAddr account. That account has
email two-factor sign-in, so the app never signs in by itself: a person signs
in once in a window the app opens (``python -m app.cli instaddr-connect``),
and the browser's signed-in state is kept in ``app_settings`` under
:data:`STATE_KEY` — never in a file, never shown by the API — and loaded into
every later read, as Flipkart sessions are.

Mapped live against https://m.kuku.lu/recv.php (18 Nov 2026):
- Filtering with ``recv.php?q=<address>`` narrows rows server-side to that address.
- Each mail row is ``a[id^="link_maildata_"]`` containing ``#area_mail_title_<id>``
  and ``.font_gray`` (``"HH:MM (age) | Sender <from> » recipient@domain"``).
- Clicking the row opens ``smphone.app.recv.view.php`` in ``#area_maildata_iframe_<id>``
  when the full body is needed (when the subject alone lacks the OTP).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
from urllib.parse import quote

from playwright.async_api import async_playwright
from sqlalchemy.orm import Session as DbSession

from app.automation.otp import OtpReader, otp_from_text
from app.db.models import Setting

log = logging.getLogger("ordering.instaddr")

HOME_URL = "https://m.kuku.lu/en.php"
INBOX_URL = "https://m.kuku.lu/recv.php"
ADDR_LIST_URL = "https://m.kuku.lu/datagen.php?action=getAddrList"
#: The ``app_settings`` row holding the signed-in browser state (cookies and
#: local storage) of the owner's InstAddr account.
STATE_KEY = "instaddr.browser_state"
#: Cached domains owned by the connected InstAddr account (e.g. catbar.net).
DOMAINS_KEY = "instaddr.domains"
DEFAULT_DOMAINS: tuple[str, ...] = ("catbar.net",)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ORDINAL_DAY_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_AGE_RE = re.compile(
    r"\(\s*(?:(?P<just>just\s*now)|(?P<num>\d+)\s*(?P<unit>sec(?:ond)?s?|min(?:ute)?s?|hours?|hrs?|days?|weeks?|months?|years?))\s*\)",
    re.IGNORECASE,
)
_TIME_PREFIX_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\b")
_CLOUDFLARE_MARKER = "performing security verification"


def load_state(db: DbSession) -> dict | None:
    row = db.get(Setting, STATE_KEY)
    return row.value if row is not None and isinstance(row.value, dict) else None


def save_state(db: DbSession, state: dict) -> None:
    row = db.get(Setting, STATE_KEY)
    if row is None:
        db.add(Setting(key=STATE_KEY, value=state))
    else:
        row.value = state
    db.commit()


def forget_state(db: DbSession) -> bool:
    deleted = False
    for key in (STATE_KEY, DOMAINS_KEY):
        row = db.get(Setting, key)
        if row is not None:
            db.delete(row)
            deleted = True
    if deleted:
        db.commit()
    return deleted


def load_domains(db: DbSession) -> tuple[str, ...]:
    """Domains routed to InstAddr when a browser session is saved."""
    row = db.get(Setting, DOMAINS_KEY)
    if row is not None and isinstance(row.value, list):
        cleaned = tuple(dict.fromkeys(str(d).strip().lower().lstrip("@") for d in row.value if str(d).strip()))
        if cleaned:
            return cleaned
    return DEFAULT_DOMAINS


def save_domains(db: DbSession, domains: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    cleaned = list(dict.fromkeys(str(d).strip().lower().lstrip("@") for d in domains if str(d).strip()))
    row = db.get(Setting, DOMAINS_KEY)
    if row is None:
        db.add(Setting(key=DOMAINS_KEY, value=cleaned))
    else:
        row.value = cleaned
    db.commit()
    return tuple(cleaned)


async def fetch_domains(state: dict) -> tuple[str, ...]:
    """Read the domains of all addresses held by the signed-in InstAddr account."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=state)
        try:
            resp = await context.request.get(ADDR_LIST_URL, timeout=30_000)
            if resp.status != 200:
                return ()
            text = await resp.text()
            addrs = {m.lower() for m in _EMAIL_RE.findall(text)}
            return tuple(sorted({addr.rpartition("@")[2] for addr in addrs if "@" in addr}))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not fetch InstAddr domain list: %s", exc.__class__.__name__)
            return ()
        finally:
            await browser.close()


async def connect_interactively(wait_for_person) -> dict:
    """Open a visible browser on InstAddr for a person to sign in, then hand
    back its signed-in state. ``wait_for_person`` is awaited while the window
    is open (the CLI waits for Enter). Nothing is typed by the tool."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        try:
            await wait_for_person()
            return await context.storage_state()
        finally:
            await browser.close()


async def dump_inbox(state: dict, url: str = HOME_URL) -> dict:
    """Read-only probe: open the mailbox with the saved state and report what
    the page holds — its text lines and the shape of its links and frames —
    so the reader can be built against the real page. Presses nothing."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=state)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(5_000)
            text = await page.locator("body").inner_text(timeout=15_000)
            links = await page.locator("a[href]").evaluate_all(
                "els => els.slice(0, 80).map(e => [e.innerText.trim().slice(0, 60), e.getAttribute('href')])"
            )
            frames = [frame.url for frame in page.frames]
            return {"url": page.url, "lines": [l.strip() for l in text.splitlines() if l.strip()], "links": links, "frames": frames}
        finally:
            await browser.close()


@dataclass(frozen=True)
class InstAddrMail:
    """One row from https://m.kuku.lu/recv.php."""

    mail_id: str
    subject: str
    meta: str
    date_label: str = ""
    body: str = ""

    @property
    def combined_text(self) -> str:
        return f"{self.subject} {self.body}".strip() if self.body else self.subject


def parse_age_seconds(meta: str) -> float | None:
    """Parse InstAddr's parenthesised relative age such as ``(1hour)`` or ``(25sec)``."""
    match = _AGE_RE.search(meta or "")
    if not match:
        return None
    if match.group("just"):
        return 0.0
    amount = int(match.group("num"))
    unit = match.group("unit").lower()
    if unit.startswith("sec"):
        return float(amount)
    if unit.startswith("min"):
        return float(amount * 60)
    if unit.startswith(("hour", "hr")):
        return float(amount * 3600)
    if unit.startswith("day"):
        return float(amount * 86400)
    if unit.startswith("week"):
        return float(amount * 7 * 86400)
    if unit.startswith("month"):
        return float(amount * 30 * 86400)
    return float(amount * 365 * 86400)


def _parse_date_label(date_label: str) -> datetime | None:
    """Parse headings like ``Wednesday 18th of November 2026``."""
    cleaned = _ORDINAL_DAY_RE.sub(r"\1", (date_label or "").strip())
    cleaned = re.sub(r"\bof\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = " ".join(cleaned.replace(",", " ").split())
    for fmt in ("%A %d %B %Y", "%d %B %Y", "%B %d %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _mail_within_window(
    mail: InstAddrMail,
    *,
    now: datetime,
    not_before: datetime | None,
    max_age_minutes: int,
) -> bool:
    """Check whether a mail row is fresh enough against ``max_age_minutes`` and ``not_before``.

    Uses both InstAddr's relative age (``(30sec)``, ``(2min)``, ``(1hour)``) and
    its ``HH:MM`` clock time when available, so an earlier OTP sitting in the
    mailbox is rejected while codes that just arrived are accepted cleanly.
    """
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    nb_utc = (not_before if not_before.tzinfo else not_before.replace(tzinfo=timezone.utc)) if not_before else None

    age_s = parse_age_seconds(mail.meta)
    max_s = float(max(1, max_age_minutes) * 60)
    if nb_utc is not None:
        # InstAddr rounds `(1min)` / `(2min)` down to whole minutes; allow 60s
        # minute-rounding tolerance alongside the age since `not_before`.
        window_s = max(60.0, (now_utc - nb_utc).total_seconds() + 60.0)
        max_s = min(max_s, window_s)

    if age_s is not None and age_s > max_s:
        return False

    time_match = _TIME_PREFIX_RE.match(mail.meta or "")
    if nb_utc is not None and time_match is not None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        day = _parse_date_label(mail.date_label)
        local_now = now_utc.astimezone(local_tz)
        base_date = day.date() if day is not None else local_now.date()
        hour, minute = int(time_match.group(1)), int(time_match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            # Because InstAddr displays HH:MM without seconds, treat the mail
            # timestamp at the end of that minute (:59) to avoid rejecting a
            # code sent in the exact same minute as `not_before`.
            sent_end_local = datetime(
                base_date.year, base_date.month, base_date.day, hour, minute, 59, tzinfo=local_tz
            )
            if day is None and sent_end_local - timedelta(hours=12) > local_now:
                sent_end_local -= timedelta(days=1)
            if sent_end_local.astimezone(timezone.utc) < nb_utc:
                return False

    return True


def _row_targets_address(meta: str, address: str) -> bool:
    """Verify ``meta`` names ``address`` with exact email boundaries."""
    wanted = (address or "").strip().lower()
    if not wanted:
        return False
    boundary = re.compile(
        rf"(?<![A-Za-z0-9._%+-]){re.escape(wanted)}(?![A-Za-z0-9._%+-])",
        re.IGNORECASE,
    )
    if "»" in (meta or ""):
        recipient_part = meta.rpartition("»")[2]
        if boundary.search(recipient_part):
            return True
    return bool(boundary.search(meta or ""))


def _row_matches_sender(mail: InstAddrMail, senders: tuple[str, ...]) -> bool:
    if not senders:
        return True
    haystack = f"{mail.meta} {mail.subject} {mail.body}".lower()
    return any(sender in haystack for sender in senders)


_EXTRACT_ROWS_JS = """() => {
  const rows = [];
  let currentDate = "";
  const nodes = document.querySelectorAll('a[id^="link_maildata_"], div, span, td, a');
  for (const el of nodes) {
    if (el.matches && el.matches('a[id^="link_maildata_"]')) {
      const id = el.id.replace('link_maildata_', '');
      const titleEl = document.getElementById('area_mail_title_' + id);
      const metaEl = el.querySelector('.font_gray');
      rows.push({
        mail_id: id,
        subject: ((titleEl ? titleEl.innerText : '') || '').trim(),
        meta: ((metaEl ? metaEl.innerText : '') || '').trim(),
        date_label: currentDate,
      });
    } else if (!el.closest || !el.closest('a[id^="link_maildata_"]')) {
      const txt = (el.innerText || '').trim();
      if (txt.length > 8 && txt.length < 50 && /\\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\\b.*\\b20\\d\\d\\b/i.test(txt)) {
        currentDate = txt;
      }
    }
  }
  return rows;
}"""


class InstAddrOtpReader(OtpReader):
    """Reads OTP emails directly from a saved InstAddr (m.kuku.lu) session.

    Shares one Playwright browser context across concurrent logins behind an
    async lock (closed automatically once the last active waiter finishes), so
    bulk login runs poll ``recv.php?q=<address>`` in <1s per check without
    launching multiple browsers.
    """

    def __init__(
        self,
        state: dict,
        *,
        poll_interval: int = 3,
        senders: tuple[str, ...] = ("flipkart",),
        max_age_minutes: int = 15,
    ) -> None:
        self.state = state
        self.poll_interval = max(1, int(poll_interval or 3))
        self.senders = tuple(x.strip().lower() for x in senders if x and x.strip())
        self.max_age_minutes = max(1, int(max_age_minutes or 15))
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self._context = None
        self._waiters = 0
        self._warned_session = False

    async def _ensure_context(self):
        if self._context is None:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
            self._context = await self._browser.new_context(storage_state=self.state)
        return self._context

    async def _close_context(self) -> None:
        browser, pw = self._browser, self._pw
        self._context = None
        self._browser = None
        self._pw = None
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        if pw is not None:
            with contextlib.suppress(Exception):
                await pw.stop()

    async def _read_mail_body(self, page, mail_id: str) -> str:
        """Click one message row to load its full body inside the detail iframe."""
        try:
            await page.locator(f"#link_maildata_{mail_id}").click(timeout=5_000)
            for _ in range(16):
                for frame in page.frames:
                    if "recv.view" in (frame.url or ""):
                        with contextlib.suppress(Exception):
                            txt = (await frame.locator("body").inner_text(timeout=2_000)).strip()
                            if txt:
                                return txt
                await page.wait_for_timeout(250)
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _fetch_page_rows(self, page, address: str) -> list[InstAddrMail]:
        url = f"{INBOX_URL}?q={quote(address)}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Mail rows are loaded asynchronously after #search-basic renders, so
        # wait specifically for mail-row links rather than the search input.
        with contextlib.suppress(Exception):
            await page.wait_for_selector('a[id^="link_maildata_"]', timeout=5_000)
        body_text = (await page.locator("body").inner_text(timeout=5_000)).lower()
        if _CLOUDFLARE_MARKER in body_text or "pagemode_login" in (page.url or ""):
            if not self._warned_session:
                self._warned_session = True
                log.error(
                    "InstAddr session expired or blocked by Cloudflare — run `python -m app.cli instaddr-connect` again"
                )
            return []
        raw_rows = await page.evaluate(_EXTRACT_ROWS_JS)
        return [
            InstAddrMail(
                mail_id=str(row["mail_id"]),
                subject=str(row.get("subject") or ""),
                meta=str(row.get("meta") or ""),
                date_label=str(row.get("date_label") or ""),
            )
            for row in raw_rows
        ]

    async def list_messages(self, address: str, *, include_body: bool = False) -> list[InstAddrMail]:
        """Fetch recent mail rows for ``address`` from ``recv.php?q=...``."""
        clean = (address or "").strip()
        if not clean:
            return []
        async with self._lock:
            context = await self._ensure_context()
            page = await context.new_page()
            try:
                mails = await self._fetch_page_rows(page, clean)
                if include_body and mails:
                    enriched: list[InstAddrMail] = []
                    for mail in mails[:5]:
                        full_body = await self._read_mail_body(page, mail.mail_id)
                        enriched.append(
                            InstAddrMail(
                                mail_id=mail.mail_id,
                                subject=mail.subject,
                                meta=mail.meta,
                                date_label=mail.date_label,
                                body=full_body,
                            )
                        )
                    return enriched + mails[5:]
                return mails
            finally:
                with contextlib.suppress(Exception):
                    await page.close()
                if self._waiters <= 0:
                    await self._close_context()

    async def _poll_once(self, address: str, *, not_before: datetime | None) -> str | None:
        from app.automation.otp import otp_from_text

        clean = (address or "").strip()
        if not clean:
            return None
        now = datetime.now(timezone.utc)
        async with self._lock:
            context = await self._ensure_context()
            page = await context.new_page()
            try:
                for mail in await self._fetch_page_rows(page, clean):
                    if not _row_targets_address(mail.meta, clean):
                        continue
                    if not _mail_within_window(
                        mail,
                        now=now,
                        not_before=not_before,
                        max_age_minutes=self.max_age_minutes,
                    ):
                        continue
                    if _row_matches_sender(mail, self.senders):
                        code = otp_from_text(mail.subject, require_context=True)
                        if code:
                            return code
                    body = await self._read_mail_body(page, mail.mail_id)
                    if not body:
                        continue
                    full_mail = InstAddrMail(
                        mail_id=mail.mail_id,
                        subject=mail.subject,
                        meta=mail.meta,
                        date_label=mail.date_label,
                        body=body,
                    )
                    if not _row_matches_sender(full_mail, self.senders):
                        continue
                    code = otp_from_text(full_mail.combined_text, require_context=True)
                    if code:
                        return code
                return None
            finally:
                with contextlib.suppress(Exception):
                    await page.close()

    async def get_otp(
        self,
        address: str,
        *,
        timeout: int = 60,
        not_before: datetime | None = None,
    ) -> str | None:
        import time

        async with self._lock:
            self._waiters += 1
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                code = await self._poll_once(address, not_before=not_before)
                if code:
                    return code
                await asyncio.sleep(self.poll_interval)
            return None
        finally:
            async with self._lock:
                self._waiters -= 1
                if self._waiters <= 0:
                    await self._close_context()

