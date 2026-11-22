"""One browser per run, one isolated window per account (PLAN.md §10).

No real browser: a fake launcher stands in for Playwright, so these pin the
pool's own rules — how windows are shared out over browsers, that each
window keeps its own cookies and profile, that a dead browser is replaced,
that nothing launches until a window is asked for, and that closing the pool
closes everything it opened.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.core.browser import (
    IPHONE_SE,
    NO_IMAGES_ARG,
    TELEMETRY_URL,
    BrowserPool,
    browser_page,
    browser_pool,
)
from app.db.models import Account, AccountStatus, Job, JobStatus, JobType
from app.jobs import tasks


# --- a Playwright that does not exist ------------------------------------------
class FakeWindow:
    """What ``browser.new_context`` hands back."""

    def __init__(self, browser: "FakeBrowser", kwargs: dict) -> None:
        self.browser = browser
        self.kwargs = kwargs
        self.routes: list = []
        self.cookies: list[dict] = []
        self.pages = 0
        self.closed = False

    async def route(self, pattern, handler) -> None:
        self.routes.append(pattern)

    async def add_cookies(self, cookies: list[dict]) -> None:
        self.cookies = list(cookies)

    async def new_page(self) -> object:
        self.pages += 1
        return object()

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, kwargs: dict) -> None:
        self.kwargs = kwargs
        self.windows: list[FakeWindow] = []
        self.connected = True
        self.closed = False

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **kwargs) -> FakeWindow:
        if not self.connected:
            raise RuntimeError("browser has been closed")
        window = FakeWindow(self, kwargs)
        self.windows.append(window)
        return window

    async def close(self) -> None:
        self.closed = True
        self.connected = False


def _launcher(launched: list[FakeBrowser]):
    async def launch(**kwargs) -> FakeBrowser:
        browser = FakeBrowser(kwargs)
        launched.append(browser)
        return browser

    return launch


async def _hold(pool: BrowserPool, stack: contextlib.AsyncExitStack, **kwargs) -> FakeWindow:
    """Open a window and keep it open until the stack closes."""
    context, _page = await stack.enter_async_context(pool.page(**kwargs))
    return context


# --- sharing windows over browsers -------------------------------------------------
def test_one_browser_carries_eight_windows_and_the_ninth_opens_another():
    launched: list[FakeBrowser] = []

    async def scenario():
        async with BrowserPool(launcher=_launcher(launched), contexts_per_browser=8) as pool:
            async with contextlib.AsyncExitStack() as stack:
                await asyncio.gather(*(_hold(pool, stack) for _ in range(8)))
                assert pool.browsers == 1 and pool.windows == 8
                await _hold(pool, stack)
                assert pool.browsers == 2 and pool.windows == 9
            assert pool.windows == 0  # every window closed, both browsers still there
            assert pool.browsers == 2

    asyncio.run(scenario())
    assert len(launched) == 2


def test_eight_accounts_starting_together_share_one_browser():
    """The burst at the start of a run must not become eight launches."""
    launched: list[FakeBrowser] = []

    async def scenario():
        async with BrowserPool(launcher=_launcher(launched), contexts_per_browser=8) as pool:
            async with contextlib.AsyncExitStack() as stack:
                await asyncio.gather(*(_hold(pool, stack) for _ in range(8)))
            assert pool.launches == 1

    asyncio.run(scenario())


def test_a_closed_window_frees_its_seat_for_the_next_account():
    launched: list[FakeBrowser] = []

    async def scenario():
        async with BrowserPool(launcher=_launcher(launched), contexts_per_browser=2) as pool:
            async with pool.page() as (first, _):
                async with pool.page():
                    pass  # the second window closes here
                async with pool.page():
                    assert pool.browsers == 1  # the freed seat was reused
            assert first.closed is True

    asyncio.run(scenario())


def test_nothing_is_launched_until_a_window_is_asked_for():
    launched: list[FakeBrowser] = []

    async def scenario():
        async with BrowserPool(launcher=_launcher(launched)):
            pass

    asyncio.run(scenario())
    assert launched == []


# --- what a window is ------------------------------------------------------------------
def test_each_window_keeps_its_own_cookies_and_profile():
    launched: list[FakeBrowser] = []

    async def scenario():
        async with BrowserPool(launcher=_launcher(launched)) as pool:
            async with contextlib.AsyncExitStack() as stack:
                desktop = await _hold(pool, stack, cookies=[{"name": "SN", "value": "a@x.com"}])
                phone = await _hold(pool, stack, mobile=True, cookies=[{"name": "SN", "value": "b@x.com"}])
                assert desktop.cookies == [{"name": "SN", "value": "a@x.com"}]
                assert phone.cookies == [{"name": "SN", "value": "b@x.com"}]
                assert desktop.kwargs == {}
                assert phone.kwargs["viewport"] == IPHONE_SE["viewport"]
                assert phone.kwargs["is_mobile"] is True
                assert desktop.browser is phone.browser  # one browser, two walled-off windows
                assert desktop.pages == 1 and phone.pages == 1

    asyncio.run(scenario())


def test_a_lean_pool_refuses_telemetry_and_images_and_uses_the_proxy():
    launched: list[FakeBrowser] = []

    async def scenario():
        async with BrowserPool(
            launcher=_launcher(launched), headless=False, proxy="http://proxy.local:3128"
        ) as pool:
            async with pool.page() as (window, _):
                assert window.routes == [TELEMETRY_URL]
        browser = launched[0]
        assert browser.kwargs["headless"] is False
        assert browser.kwargs["proxy"] == {"server": "http://proxy.local:3128"}
        assert browser.kwargs["args"] == [NO_IMAGES_ARG]

    asyncio.run(scenario())


def test_a_window_that_cannot_open_gives_its_seat_back():
    launched: list[FakeBrowser] = []

    class Refusing(FakeBrowser):
        async def new_context(self, **kwargs):
            raise RuntimeError("out of memory")

    async def launch(**kwargs):
        browser = Refusing(kwargs)
        launched.append(browser)
        return browser

    async def scenario():
        async with BrowserPool(launcher=launch) as pool:
            with pytest.raises(RuntimeError):
                async with pool.page():
                    pass
            assert pool.windows == 0

    asyncio.run(scenario())


# --- a browser that dies -------------------------------------------------------------
def test_a_dead_browser_is_replaced_by_the_next_window():
    launched: list[FakeBrowser] = []

    async def scenario():
        async with BrowserPool(launcher=_launcher(launched), contexts_per_browser=8) as pool:
            async with pool.page():
                pass
            launched[0].connected = False  # Chromium crashed between two accounts
            async with pool.page() as (window, _):
                assert window.browser is launched[1]
            assert pool.browsers == 1

    asyncio.run(scenario())
    assert len(launched) == 2


# --- closing ---------------------------------------------------------------------------
def test_closing_the_pool_closes_every_browser_it_launched():
    launched: list[FakeBrowser] = []

    async def scenario():
        async with browser_pool(launcher=_launcher(launched), contexts_per_browser=1) as pool:
            async with contextlib.AsyncExitStack() as stack:
                await asyncio.gather(*(_hold(pool, stack) for _ in range(3)))
            assert pool.browsers == 3
        assert pool.closed is True
        assert pool.browsers == 0

    asyncio.run(scenario())
    assert all(browser.closed for browser in launched)
    assert len(launched) == 3


def test_a_closed_pool_refuses_new_windows():
    async def scenario():
        pool = BrowserPool(launcher=_launcher([]))
        await pool.aclose()
        with pytest.raises(RuntimeError):
            async with pool.page():
                pass

    asyncio.run(scenario())


# --- the page factory the jobs call ------------------------------------------------
def test_browser_page_with_a_pool_is_a_window_of_that_pool():
    launched: list[FakeBrowser] = []

    async def scenario():
        async with BrowserPool(launcher=_launcher(launched)) as pool:
            async with browser_page(
                headless=True, proxy="ignored-the-pool-decides", cookies=[{"name": "SN", "value": "1"}], pool=pool
            ) as (window, _page):
                assert window.browser is launched[0]
                assert window.cookies == [{"name": "SN", "value": "1"}]
            assert pool.windows == 0

    asyncio.run(scenario())
    assert len(launched) == 1
    assert "proxy" not in launched[0].kwargs  # the pool's settings, not the call's


# --- the job bodies ----------------------------------------------------------------------
def test_a_login_run_hands_every_account_the_same_pool_and_closes_it_after(db, monkeypatch):
    accounts = []
    for email in ("one@test.com", "two@test.com"):
        account = Account(email=email, status=AccountStatus.new)
        db.add(account)
        db.commit()
        accounts.append(account)
    job = Job(type=JobType.login, status=JobStatus.queued)
    db.add(job)
    db.commit()

    pools: list = []

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        pools.append(kwargs.get("pool"))
        from tests.fakes import FakeContext, FakePage

        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def fake_run_login(page, email, otp_reader=None, **kwargs):
        return [{"name": "SN", "value": email}]

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_login", fake_run_login)

    asyncio.run(tasks.run_login_job(job.id, [a.id for a in accounts]))

    assert len(pools) == 2
    assert all(isinstance(pool, BrowserPool) for pool in pools)
    assert pools[0] is pools[1]  # one pool for the run
    assert pools[0].closed is True  # closed after the batch
    assert pools[0].launches == 0  # the fake page factory never asked it for a window
    db.expire_all()
    assert db.get(Job, job.id).status == JobStatus.completed
