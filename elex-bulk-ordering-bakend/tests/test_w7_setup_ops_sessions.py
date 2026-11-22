"""An account a read-back found signed out has no session (review of 27 Oct
2026).

The read-back marks such an account ``session_invalid`` and leaves its cookie
set as it was, still marked valid. Before this fix a plain login run (force
off, the Sessions page's default) skipped the account as "already has
session", so it was never signed in again, while coupon and order runs went on
loading the dead cookies into a browser. The account's status now decides:
marked ``session_invalid``, it has no session to give, a login run signs it in
again, and other runs pass it over with "no session".
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.automation.coupons import CLAIMED
from app.automation.sync import NotSignedIn
from app.core import session_store
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    CouponRun,
    Gst,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    Session as SessionModel,
)
from app.jobs import sync_tasks, tasks
from tests.fakes import FakeContext, FakePage

#: The cookies a read-back found no longer sign in. Made up.
DEAD = [{"name": "SN", "value": "signed-out-on-flipkart"}]


def _account(db, email: str, status: AccountStatus) -> Account:
    """An account holding one cookie set marked valid, whatever its status."""
    account = Account(email=email, status=status)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=DEAD, valid=True))
    db.commit()
    return account


def _signed_out(db, email: str = "signed-out@test.com") -> Account:
    """What a read-back leaves behind: the account marked, its set untouched."""
    return _account(db, email, AccountStatus.session_invalid)


def _job(db, job_type: JobType) -> Job:
    job = Job(type=job_type, status=JobStatus.queued)
    db.add(job)
    db.commit()
    return job


def _item(db, job: Job, account: Account) -> JobItem:
    db.expire_all()
    return db.query(JobItem).filter(JobItem.job_id == job.id, JobItem.account_id == account.id).one()


@pytest.fixture
def browsers(monkeypatch) -> list[dict]:
    """Every browser a run opens, recorded instead of launched."""
    opened: list[dict] = []

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        opened.append(kwargs)
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    return opened


def test_an_account_marked_signed_out_has_no_session_to_give(db):
    account = _signed_out(db)

    assert session_store.load_cookies(db, account) is None
    assert session_store.has_valid_session(db, account) is False

    # The same set on an account not marked signed out is still handed over.
    fine = _account(db, "fine@test.com", AccountStatus.session_valid)
    assert session_store.load_cookies(db, fine) == DEAD
    assert session_store.has_valid_session(db, fine) is True


def test_a_plain_login_run_signs_it_in_again(db, browsers, monkeypatch):
    account = _signed_out(db)
    job = _job(db, JobType.login)
    fresh = [{"name": "SN", "value": "signed-in-again"}]

    async def fake_run_login(page, email, otp_reader=None, **kwargs):
        return fresh

    monkeypatch.setattr(tasks, "run_login", fake_run_login)

    asyncio.run(tasks.run_login_job(job.id, [account.id]))  # force off

    item = _item(db, job, account)
    assert item.status == JobItemStatus.done, item.message
    assert item.message != "already has session"
    assert len(browsers) == 1
    account = db.get(Account, account.id)
    assert account.status == AccountStatus.session_valid
    # The dead set went with the login (session_store.save_session).
    assert [row.cookies for row in account.sessions] == [fresh]


def test_after_a_read_back_finds_it_signed_out_the_next_login_run_signs_it_in(db, browsers, monkeypatch):
    """The whole path, as audit F-8 promised it: the read-back marks the
    account and the next login run (force off) picks it up."""
    account = _account(db, "gone@test.com", AccountStatus.session_valid)
    read_back = _job(db, JobType.sync)

    @contextlib.asynccontextmanager
    async def read_back_browser(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def bounced_to_login(page, **kwargs):
        raise NotSignedIn("the account is not signed in: Flipkart sent the orders page to its login page")

    monkeypatch.setattr(sync_tasks, "browser_page", read_back_browser)
    monkeypatch.setattr(sync_tasks, "scrape_orders", bounced_to_login)
    asyncio.run(sync_tasks.run_sync_job(read_back.id, [account.id]))
    db.expire_all()
    assert db.get(Account, account.id).status == AccountStatus.session_invalid

    fresh = [{"name": "SN", "value": "signed-in-again"}]

    async def fake_run_login(page, email, otp_reader=None, **kwargs):
        return fresh

    monkeypatch.setattr(tasks, "run_login", fake_run_login)
    login = _job(db, JobType.login)

    asyncio.run(tasks.run_login_job(login.id, [account.id]))  # force off

    item = _item(db, login, account)
    assert (item.status, item.message) == (JobItemStatus.done, f"session saved ({len(fresh)} cookies)")
    assert db.get(Account, account.id).status == AccountStatus.session_valid
    assert session_store.load_cookies(db, db.get(Account, account.id)) == fresh


def test_a_coupon_run_passes_it_over_without_opening_a_browser(db, browsers, monkeypatch):
    account = _signed_out(db)
    job = _job(db, JobType.coupon)
    applied: list[str] = []

    async def fake_apply_coupon(page, coupon, *, result_wait=5):
        applied.append(coupon)
        return CLAIMED

    monkeypatch.setattr(tasks, "apply_coupon", fake_apply_coupon)

    asyncio.run(tasks.run_coupon_job(job.id, [account.id], "SAVE10"))

    item = _item(db, job, account)
    assert (item.status, item.message) == (JobItemStatus.skipped, "no session")
    assert browsers == [] and applied == []
    assert db.query(CouponRun).count() == 0


def test_an_order_run_passes_it_over_without_opening_a_browser(db, browsers, monkeypatch):
    gst = Gst(label="ACME", gstin="29ABCDE1234F1Z5", business_name="Acme Traders", is_default=True)
    db.add(gst)
    db.commit()
    db.add(Address(label="works", name="Acme Traders", line1="1 Industrial Area", pincode="560001", gst_id=gst.id, is_default=True))
    db.commit()
    account = _signed_out(db)
    job = _job(db, JobType.order)
    ordered: list[dict] = []

    async def fake_run_order(page, **kwargs):
        ordered.append(kwargs)
        raise AssertionError("no order may be attempted on a signed-out account")

    monkeypatch.setattr(tasks, "run_order", fake_run_order)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [account.id],
            product_link="https://www.flipkart.com/p/1",
            payment_mode=tasks.PAYMENT_COD,
        )
    )

    item = _item(db, job, account)
    assert (item.status, item.message) == (JobItemStatus.skipped, "no session")
    assert browsers == [] and ordered == []
    assert db.query(Order).count() == 0



def test_the_session_check_still_finds_the_set_of_an_account_marked_signed_out(db):
    """``python -m app.cli check`` tests a stored set and, with --fix, deletes
    a dead one: the set of an account a read-back marked signed out is exactly
    its case, so it must still find it (checker of 27 Oct 2026)."""
    account = _signed_out(db)
    assert session_store.load_cookies(db, account) is None  # no run loads it
    assert session_store.stored_cookies(db, account) == DEAD  # the check does
