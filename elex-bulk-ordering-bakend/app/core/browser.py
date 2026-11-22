"""Playwright browser/context factory (replaces the old Selenium driver setup).

Provides an async context manager that yields a ready page, optionally with a
mobile-emulation profile (iPhone-SE-like, matching the legacy coupon flow),
an HTTP proxy, and pre-loaded cookies to restore a saved session.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from playwright.async_api import BrowserContext, Page, async_playwright

log = logging.getLogger("ordering.browser")

# --- What an order run does not need to download -----------------------------
# Measured live 2026-10-24: a Flipkart product page loads dozens of images and
# the checkout keeps posting analytics the whole way through. The flows read
# text, structured data and the DOM — never a pixel — so images are switched
# off in the browser itself (no per-request cost) and the telemetry hosts are
# refused. Scripts, stylesheets and the page's own API calls are never touched:
# the checkout's data arrives that way, and blocking a script Flipkart's
# React-Native-Web shell needs would break hydration silently.
#: Hosts that only receive telemetry. Matched as the host or any subdomain.
TELEMETRY_HOSTS: tuple[str, ...] = (
    "sonic.fdp.api.flipkart.com",
    "nr-data.net",
    "newrelic.com",
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "clarity.ms",
    "hotjar.com",
)
TELEMETRY_URL = re.compile(
    r"^https?://(?:[^/]+\.)?(?:" + "|".join(re.escape(h) for h in TELEMETRY_HOSTS) + r")(?::\d+)?/",
    re.IGNORECASE,
)
#: Chromium's own switch for not loading images. Unlike a request route it
#: costs nothing per request.
NO_IMAGES_ARG = "--blink-settings=imagesEnabled=false"


def is_telemetry(url: str) -> bool:
    """Whether ``url`` goes to a host that only collects analytics."""
    return bool(TELEMETRY_URL.match(url or ""))

# iPhone-SE-like profile — the legacy add-coupons flow ran under mobile
# emulation ("iPhone SE"); we reproduce it with an explicit profile so we don't
# depend on Playwright's device registry naming.
IPHONE_SE = {
    "viewport": {"width": 375, "height": 667},
    "user_agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 "
        "Mobile/15E148 Safari/604.1"
    ),
    "device_scale_factor": 2,
    "is_mobile": True,
    "has_touch": True,
}


@contextlib.asynccontextmanager
async def browser_page(
    *,
    headless: bool = True,
    mobile: bool = False,
    proxy: str | None = None,
    cookies: list[dict] | None = None,
    lean: bool = True,
    pool: "BrowserPool | None" = None,
) -> AsyncIterator[tuple[BrowserContext, Page]]:
    """Yield ``(context, page)``; tears everything down on exit.

    ``lean`` (the default) skips images and telemetry — see the module notes.
    Pass ``lean=False`` for a session where the pictures matter, such as a
    screenshot the operator will look at.

    With ``pool`` the page is a window of that run's shared browser rather
    than a browser of its own (:class:`BrowserPool`): ``headless``, ``proxy``
    and ``lean`` are then the pool's, fixed when the run opened it, and only
    ``mobile`` and ``cookies`` describe this window.
    """
    if pool is not None:
        async with pool.page(mobile=mobile, cookies=cookies) as (context, page):
            yield context, page
        return

    async with async_playwright() as pw:
        launch_kwargs: dict = {"headless": headless}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        if lean:
            launch_kwargs["args"] = [NO_IMAGES_ARG]
        browser = await pw.chromium.launch(**launch_kwargs)

        context_kwargs = dict(IPHONE_SE) if mobile else {}
        context = await browser.new_context(**context_kwargs)
        if lean:
            # A regex route intercepts only the matching requests, so the
            # page's own traffic pays nothing for this.
            await context.route(TELEMETRY_URL, lambda route: route.abort("blockedbyclient"))
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()
        try:
            yield context, page
        finally:
            with contextlib.suppress(Exception):
                await context.close()
            with contextlib.suppress(Exception):
                await browser.close()


# --- One browser per run, one window per account ------------------------------
#: How many account windows one Chromium carries before the pool opens another
#: (``ORDERING_CONTEXTS_PER_BROWSER``). The browser program is the heavy part
#: — a few hundred megabytes before it shows a page — and a window inside it
#: costs a tab, so eight accounts in one browser weigh far less than eight
#: browsers. Not unbounded: a browser that dies takes every window with it,
#: and a pool of several keeps that blast radius to this many accounts.
DEFAULT_CONTEXTS_PER_BROWSER = 8


@dataclass
class _Seat:
    """One launched browser and how many account windows it holds right now."""

    browser: object
    windows: int = 0


class BrowserPool:
    """One Chromium per run — or one per :attr:`contexts_per_browser` accounts
    — with an isolated window (a Playwright *context*) for each account.

    Before this a run started a complete browser for every account and shut it
    down after: ten accounts, ten Chromiums, each paying its own start-up and
    its own memory (PLAN.md §10 "One browser per run"). A window inside one
    browser has its own cookies, storage and login, walled off from the other
    windows exactly like separate incognito windows, so the account isolation
    the old shape gave is kept; what is shared is the browser program.

    Lazy on purpose: nothing starts until the first account asks for a window,
    so a run that skips every account — or a test that stands in for the page
    factory — launches no browser. A browser that has died is dropped the next
    time a window is asked for, and a fresh one takes its place. Closing the
    pool closes every browser it launched and stops Playwright.

    ``launcher`` is a test seam: an ``async`` callable taking the launch
    keywords and returning a browser-like object (``new_context``, ``close``,
    ``is_connected``); the default launches Chromium through Playwright.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | None = None,
        lean: bool = True,
        contexts_per_browser: int = DEFAULT_CONTEXTS_PER_BROWSER,
        launcher=None,
    ) -> None:
        self.headless = headless
        self.proxy = proxy
        self.lean = lean
        self.contexts_per_browser = max(1, int(contexts_per_browser or 1))
        self._launcher = launcher
        self._playwright = None
        self._seats: list[_Seat] = []
        # Taken while a seat is chosen or a browser launched, so a burst of
        # accounts starting together shares one browser instead of each
        # launching its own — the very thing the pool exists to prevent.
        self._lock = asyncio.Lock()
        #: Browsers launched over the pool's life (diagnostics and tests).
        self.launches = 0
        self.closed = False

    async def __aenter__(self) -> "BrowserPool":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    @property
    def browsers(self) -> int:
        """Browsers alive in the pool right now."""
        return len(self._seats)

    @property
    def windows(self) -> int:
        """Account windows open across every browser right now."""
        return sum(seat.windows for seat in self._seats)

    def launch_kwargs(self) -> dict:
        """What every browser of this pool is launched with."""
        kwargs: dict = {"headless": self.headless}
        if self.proxy:
            kwargs["proxy"] = {"server": self.proxy}
        if self.lean:
            kwargs["args"] = [NO_IMAGES_ARG]
        return kwargs

    async def _launch(self) -> _Seat:
        if self._launcher is not None:
            browser = await self._launcher(**self.launch_kwargs())
        else:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            browser = await self._playwright.chromium.launch(**self.launch_kwargs())
        seat = _Seat(browser)
        self._seats.append(seat)
        self.launches += 1
        log.info("browser %d of the run launched", self.launches)
        return seat

    @staticmethod
    def _alive(browser) -> bool:
        probe = getattr(browser, "is_connected", None)
        if not callable(probe):
            return True
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 — a browser that cannot even say is gone
            return False

    async def _acquire(self) -> _Seat:
        """A seat with room for one more window, launching a browser if none has."""
        if self.closed:
            raise RuntimeError("the browser pool is closed")
        async with self._lock:
            for seat in list(self._seats):
                if not self._alive(seat.browser):
                    log.warning(
                        "a browser of the run died with %d window(s) open; a new one takes its place",
                        seat.windows,
                    )
                    self._seats.remove(seat)
            seat = next((s for s in self._seats if s.windows < self.contexts_per_browser), None)
            if seat is None:
                seat = await self._launch()
            seat.windows += 1
            return seat

    @staticmethod
    def _release(seat: _Seat) -> None:
        seat.windows = max(0, seat.windows - 1)

    @contextlib.asynccontextmanager
    async def page(
        self, *, mobile: bool = False, cookies: list[dict] | None = None
    ) -> AsyncIterator[tuple[BrowserContext, Page]]:
        """One account's window: yield ``(context, page)``, close the window after.

        Only the window is closed on exit; the browser stays for the next
        account and goes with the pool.
        """
        seat = await self._acquire()
        try:
            context = await seat.browser.new_context(**(dict(IPHONE_SE) if mobile else {}))
        except Exception:
            self._release(seat)
            raise
        try:
            if self.lean:
                await context.route(TELEMETRY_URL, lambda route: route.abort("blockedbyclient"))
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            yield context, page
        finally:
            with contextlib.suppress(Exception):
                await context.close()
            self._release(seat)

    async def aclose(self) -> None:
        """Close every browser the pool launched, then stop Playwright."""
        self.closed = True
        seats, self._seats = self._seats, []
        for seat in seats:
            with contextlib.suppress(Exception):
                await seat.browser.close()
        if self._playwright is not None:
            playwright, self._playwright = self._playwright, None
            with contextlib.suppress(Exception):
                await playwright.stop()


@contextlib.asynccontextmanager
async def browser_pool(**kwargs) -> AsyncIterator[BrowserPool]:
    """A :class:`BrowserPool` for one run, closed when the run is over."""
    pool = BrowserPool(**kwargs)
    try:
        yield pool
    finally:
        await pool.aclose()
