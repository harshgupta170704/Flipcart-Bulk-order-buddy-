"""An email Flipkart has no account for: said at once, account marked not registered."""

import asyncio
import contextlib

import pytest

from app.automation.login import NotRegistered, email_not_registered
from app.db.models import Account, AccountStatus, Job, JobItem, JobStatus, JobType
from app.jobs import tasks

NEW_HERE = "<div>Login</div><div>Looks like you're new here!</div><div>Sign up with your number to continue</div>"
OTP_PAGE = "<div>Please enter the OTP sent to</div><button>Verify</button>"


async def _check(html):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium cannot start here ({exc.__class__.__name__})")
        try:
            page = await browser.new_page(java_script_enabled=False)
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(f"<html><body>{html}</body></html>")
            return await email_not_registered(page, wait_ms=500)
        finally:
            await browser.close()


def test_the_new_here_page_is_recognised():
    assert asyncio.run(_check(NEW_HERE)) is True


def test_the_otp_page_is_not_mistaken_for_it():
    assert asyncio.run(_check(OTP_PAGE)) is False


def test_a_sign_in_run_marks_the_account_not_registered_and_says_why(db, monkeypatch):
    account = Account(email="nobody@t.in", status=AccountStatus.new)
    db.add(account)
    job = Job(type=JobType.login, status=JobStatus.queued)
    db.add(job)
    db.commit()

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_kwargs):
        yield None, None

    async def fake_run_login(page, email, *_args, **_kwargs):
        raise NotRegistered(email)

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_login", fake_run_login)

    asyncio.run(tasks.run_login_job(job.id, [account.id]))

    db.expire_all()
    assert db.get(Account, account.id).status == AccountStatus.not_registered
    (item,) = db.query(JobItem).filter(JobItem.job_id == job.id).all()
    assert "not registered on Flipkart" in item.message
