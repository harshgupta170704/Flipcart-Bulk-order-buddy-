"""Tests for the three job bodies — no real browser, no real Flipkart.

``app.jobs.tasks.browser_page`` and the automation entry points are
monkeypatched, so these exercise the job plumbing: concurrency, per-account
status, persistence, the price-target outcome (PLAN.md §6a) and the rule that
card data is never written anywhere (PLAN.md §5).
"""

import asyncio
import contextlib

import pytest

from app.automation.coupons import ALREADY_USED, CLAIMED, ERROR
from app.automation.ordering import CardInfo
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
    OrderStatus,
    Session as SessionModel,
    Setting,
)
from app.jobs import tasks
from tests.fakes import FakeContext, FakePage

# Distinctive, obviously-fake card values. They must appear NOWHERE in the DB.
CARD_NUMBER = "4111111111111111"
CARD_CVV = "4821"
OVER_TARGET_CARD = "4000000000000002"


# --- helpers -----------------------------------------------------------------
def _account(db, email, *, with_session=True, phone=None):
    account = Account(email=email, phone=phone, status=AccountStatus.new)
    db.add(account)
    db.commit()
    if with_session:
        db.add(
            SessionModel(
                account_id=account.id,
                cookies=[{"name": "SN", "value": email}],
                valid=True,
            )
        )
        db.commit()
    return account


def _job(db, job_type, payload=None):
    job = Job(type=job_type, status=JobStatus.queued, payload=payload)
    db.add(job)
    db.commit()
    return job


def _reload(db, job):
    db.expire_all()
    return db.get(Job, job.id)


def _items_by_account(db, job_id):
    db.expire_all()
    return {
        item.account_id: item
        for item in db.query(JobItem).filter(JobItem.job_id == job_id).all()
    }


def _fake_browser(calls):
    """Stand-in for core.browser.browser_page: yields (context, page)."""

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        calls.append(kwargs)
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    return fake_browser_page


@pytest.fixture(autouse=True)
def _address_book(db):
    """Every order run ships to an Address Book address, so one default pair is
    on file throughout. These tests are about the job plumbing; which address
    and GST a run picks is ``test_order_address_gst.py``'s business."""
    gst = Gst(label="ACME", gstin="29ABCDE1234F1Z5", business_name="Acme Traders", is_default=True)
    db.add(gst)
    db.commit()
    db.add(
        Address(
            label="acme-works",
            name="Acme Traders",
            line1="1 Industrial Area",
            pincode="560001",
            gst_id=gst.id,
            is_default=True,
        )
    )
    db.commit()


# --- login -------------------------------------------------------------------
def test_login_job_saves_sessions_and_skips_accounts_that_have_one(db, monkeypatch):
    fresh = _account(db, "fresh@test.com", with_session=False)
    existing = _account(db, "existing@test.com", with_session=True)
    job = _job(db, JobType.login)

    browser_calls: list[dict] = []
    monkeypatch.setattr(tasks, "browser_page", _fake_browser(browser_calls))

    async def fake_run_login(page, email, otp_reader=None, **kwargs):
        return [{"name": "SN", "value": email}]

    monkeypatch.setattr(tasks, "run_login", fake_run_login)

    asyncio.run(tasks.run_login_job(job.id, [fresh.id, existing.id]))

    job = _reload(db, job)
    assert job.status == JobStatus.completed
    assert job.progress == 100
    assert job.total_items == 2

    items = _items_by_account(db, job.id)
    assert items[fresh.id].status == JobItemStatus.done
    assert items[existing.id].status == JobItemStatus.skipped
    assert items[existing.id].message == "already has session"

    # Only the account without a session opened a browser.
    assert len(browser_calls) == 1
    db.expire_all()
    assert db.get(Account, fresh.id).status == AccountStatus.session_valid


def test_login_job_force_relogs_an_account_that_has_a_session(db, monkeypatch):
    account = _account(db, "existing@test.com", with_session=True)
    job = _job(db, JobType.login)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))

    async def fake_run_login(page, email, otp_reader=None, **kwargs):
        return [{"name": "SN", "value": "forced"}]

    monkeypatch.setattr(tasks, "run_login", fake_run_login)

    asyncio.run(tasks.run_login_job(job.id, [account.id], force=True))

    items = _items_by_account(db, job.id)
    assert items[account.id].status == JobItemStatus.done
    valid = [s for s in db.get(Account, account.id).sessions if s.valid]
    assert len(valid) == 1
    assert valid[0].cookies == [{"name": "SN", "value": "forced"}]


# --- coupons -----------------------------------------------------------------
def test_coupon_job_writes_coupon_runs_and_completes(db, monkeypatch):
    first = _account(db, "c1@test.com")
    second = _account(db, "c2@test.com")
    job = _job(db, JobType.coupon, payload={"coupon_code": "SAVE10"})

    browser_calls: list[dict] = []
    monkeypatch.setattr(tasks, "browser_page", _fake_browser(browser_calls))

    results = {first.id: CLAIMED, second.id: ALREADY_USED}
    seen_coupons: list[str] = []

    async def fake_apply_coupon(page, coupon, *, result_wait=5):
        seen_coupons.append(coupon)
        # The cookies of the account under test came through the browser kwargs.
        cookies = await page.context.cookies()
        email = cookies[0]["value"]
        account_id = first.id if email == "c1@test.com" else second.id
        return results[account_id]

    monkeypatch.setattr(tasks, "apply_coupon", fake_apply_coupon)

    asyncio.run(tasks.run_coupon_job(job.id, [first.id, second.id], "SAVE10"))

    job = _reload(db, job)
    assert job.status == JobStatus.completed
    assert (job.done_items, job.failed_items, job.progress) == (2, 0, 100)

    runs = {r.account_id: r for r in db.query(CouponRun).all()}
    assert runs[first.id].result == CLAIMED
    assert runs[second.id].result == ALREADY_USED
    assert all(r.coupon_code == "SAVE10" for r in runs.values())
    assert seen_coupons == ["SAVE10", "SAVE10"]

    # The legacy coupon flow is mobile-only and needs the restored session.
    assert all(call["mobile"] is True for call in browser_calls)
    assert all(call["cookies"] for call in browser_calls)


def test_coupon_job_skips_accounts_without_a_session(db, monkeypatch):
    without = _account(db, "nosession@test.com", with_session=False)
    with_session = _account(db, "ok@test.com")
    job = _job(db, JobType.coupon)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))

    async def fake_apply_coupon(page, coupon, *, result_wait=5):
        return CLAIMED

    monkeypatch.setattr(tasks, "apply_coupon", fake_apply_coupon)

    asyncio.run(tasks.run_coupon_job(job.id, [without.id, with_session.id], "SAVE10"))

    items = _items_by_account(db, job.id)
    assert items[without.id].status == JobItemStatus.skipped
    assert items[without.id].message == "no session"
    assert items[with_session.id].status == JobItemStatus.done
    # No browser ran for the skipped account, so no CouponRun row either.
    assert [r.account_id for r in db.query(CouponRun).all()] == [with_session.id]


def test_coupon_job_records_an_error_row_when_the_flow_raises(db, monkeypatch):
    broken = _account(db, "boom@test.com")
    fine = _account(db, "fine@test.com")
    job = _job(db, JobType.coupon)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))

    async def fake_apply_coupon(page, coupon, *, result_wait=5):
        cookies = await page.context.cookies()
        if cookies[0]["value"] == "boom@test.com":
            raise RuntimeError("page crashed")
        return CLAIMED

    monkeypatch.setattr(tasks, "apply_coupon", fake_apply_coupon)

    asyncio.run(tasks.run_coupon_job(job.id, [broken.id, fine.id], "SAVE10"))

    job = _reload(db, job)
    assert job.status == JobStatus.completed  # one failure never fails the batch
    assert (job.done_items, job.failed_items) == (2, 1)

    runs = {r.account_id: r.result for r in db.query(CouponRun).all()}
    assert runs[broken.id] == ERROR
    assert runs[fine.id] == CLAIMED

    items = _items_by_account(db, job.id)
    assert items[broken.id].status == JobItemStatus.failed
    assert "page crashed" in items[broken.id].message
    assert items[fine.id].status == JobItemStatus.done


# --- ordering ----------------------------------------------------------------
def _order_stub(over_target_amount=1000.01, target=1000.00):
    """run_order stand-in: the over-target card returns the §6a outcome."""
    calls: list[dict] = []

    async def fake_run_order(
        page,
        *,
        product_link,
        address,
        gst,
        card=None,
        qty=1,
        target=None,
        payment_mode=tasks.PAYMENT_CARD,
        **_extra,
    ):
        calls.append(
            {
                "product_link": product_link,
                "address": address,
                "gst": gst,
                "qty": qty,
                "target": target,
                "payment_mode": payment_mode,
                "card_number": card.number if card is not None else None,
            }
        )
        if card.number == OVER_TARGET_CARD:
            # run_order never clicked confirm: no order id, nothing charged.
            return {
                "status": OrderStatus.over_target,
                "amount": over_target_amount,
                "target": target,
            }
        return {
            "status": OrderStatus.placed,
            "amount": 999.00,
            "target": target,
            "order_id": f"OD-{address.phone or 'x'}",
            "model": "Model X",
            "color": "Blue",
            "pin": address.pin,
            "qty": qty,
        }

    return fake_run_order, calls


def _order_batch(db):
    """Three accounts: over-target, placed, and one with no card at all."""
    over = _account(db, "over@test.com", phone="9000000001")
    good = _account(db, "good@test.com", phone="9000000002")
    nocard = _account(db, "nocard@test.com", phone="9000000003")
    cards = {
        over.id: CardInfo(number=OVER_TARGET_CARD, month="12", year="2031", cvv=CARD_CVV),
        good.id: CardInfo(number=CARD_NUMBER, month="12", year="2031", cvv=CARD_CVV),
    }
    return over, good, nocard, cards


def test_over_target_order_is_recorded_and_the_batch_continues(db, monkeypatch):
    over, good, nocard, cards = _order_batch(db)
    job = _job(
        db,
        JobType.order,
        payload={"product_link": "https://flipkart.com/p/1", "target": 1000.00},
    )

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, _calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [over.id, good.id, nocard.id],
            product_link="https://flipkart.com/p/1",
            qty=2,
            target=1000.00,
            cards=cards,
        )
    )

    orders = {o.account_id: o for o in db.query(Order).all()}

    # The over-target account: recorded, not placed, no order id.
    assert orders[over.id].status == OrderStatus.over_target
    assert orders[over.id].amount == pytest.approx(1000.01)
    assert orders[over.id].target == pytest.approx(1000.00)
    assert orders[over.id].order_id is None
    assert orders[over.id].placed_at is None

    # ...and the rest of the batch still went through. This is the §6a rule.
    assert orders[good.id].status == OrderStatus.placed
    assert orders[good.id].order_id == "OD-9000000002"
    assert orders[good.id].amount == pytest.approx(999.00)
    assert orders[good.id].model == "Model X"
    assert orders[good.id].color == "Blue"
    assert orders[good.id].qty == 2
    assert orders[good.id].placed_at is not None
    assert nocard.id not in orders  # skipped before any browser opened

    job = _reload(db, job)
    assert job.total_items == 3
    assert job.done_items == 3
    # Skips are counted separately from failures: the over-target order is a
    # real failure, the no-card account was merely passed over. Lumping them
    # together would show the user a red "2 failed" for a healthy run.
    assert job.failed_items == 1  # the over-target order
    assert job.skipped_items == 1  # the account with no card
    assert job.progress == 100
    assert job.status == JobStatus.completed  # the run itself did not fail

    items = _items_by_account(db, job.id)
    assert items[over.id].status == JobItemStatus.failed
    assert items[over.id].message == "over target: paid nothing, ₹1,000.01 > ₹1,000.00"
    assert items[good.id].status == JobItemStatus.done
    assert "OD-9000000002" in items[good.id].message


def test_order_job_skips_an_account_with_no_card(db, monkeypatch):
    over, good, nocard, cards = _order_batch(db)
    job = _job(db, JobType.order)

    browser_calls: list[dict] = []
    monkeypatch.setattr(tasks, "browser_page", _fake_browser(browser_calls))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [nocard.id, good.id],
            product_link="https://flipkart.com/p/1",
            target=1000.00,
            cards=cards,
        )
    )

    items = _items_by_account(db, job.id)
    assert items[nocard.id].status == JobItemStatus.skipped
    assert items[nocard.id].message == "no card supplied"
    assert items[good.id].status == JobItemStatus.done

    # Skipped before opening a browser or calling the ordering flow.
    assert len(browser_calls) == 1
    assert [c["card_number"] for c in calls] == [CARD_NUMBER]


def test_order_job_skips_an_account_with_no_session(db, monkeypatch):
    nosession = _account(db, "nosession@test.com", with_session=False)
    job = _job(db, JobType.order)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, _calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    cards = {
        nosession.id: CardInfo(
            number=CARD_NUMBER, month="12", year="2031", cvv=CARD_CVV
        )
    }
    asyncio.run(
        tasks.run_order_job(
            job.id,
            [nosession.id],
            product_link="https://flipkart.com/p/1",
            cards=cards,
        )
    )

    items = _items_by_account(db, job.id)
    assert items[nosession.id].status == JobItemStatus.skipped
    assert items[nosession.id].message == "no session"
    assert db.query(Order).count() == 0


def test_order_job_ships_to_the_address_book_defaults(db, monkeypatch):
    """No ids given: the run uses the rows marked default (seeded above), and
    nothing else — there is no Settings fallback to fall through to."""
    account = _account(db, "good@test.com", phone="9000000002")
    job = _job(db, JobType.order)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    cards = {
        account.id: CardInfo(number=CARD_NUMBER, month="12", year="2031", cvv=CARD_CVV)
    }
    asyncio.run(
        tasks.run_order_job(
            job.id,
            [account.id],
            product_link="https://flipkart.com/p/1",
            cards=cards,
        )
    )

    address = calls[0]["address"]
    gst = calls[0]["gst"]
    assert address.name == "Acme Traders"
    assert address.pin == "560001"
    assert address.area == "1 Industrial Area"
    assert address.phone == "9000000002"  # the account's own phone
    assert (gst.gstin, gst.business_name) == ("29ABCDE1234F1Z5", "Acme Traders")

    order = db.query(Order).one()
    assert order.gst_name == "Acme Traders"
    assert order.pin == "560001"


def test_order_job_records_a_failed_order_when_the_flow_raises(db, monkeypatch):
    broken = _account(db, "broken@test.com", phone="9000000009")
    job = _job(db, JobType.order)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))

    async def exploding_run_order(page, **kwargs):
        raise RuntimeError(f"driver died with card {CARD_NUMBER}")

    monkeypatch.setattr(tasks, "run_order", exploding_run_order)

    cards = {
        broken.id: CardInfo(number=CARD_NUMBER, month="12", year="2031", cvv=CARD_CVV)
    }
    asyncio.run(
        tasks.run_order_job(
            job.id,
            [broken.id],
            product_link="https://flipkart.com/p/1",
            target=1000.00,
            cards=cards,
        )
    )

    order = db.query(Order).one()
    assert order.status == OrderStatus.failed
    assert order.order_id is None
    assert order.placed_at is None

    job = _reload(db, job)
    assert job.status == JobStatus.failed  # the only item failed
    assert job.failed_items == 1

    # Even a third-party message that echoed the PAN is scrubbed (PLAN.md §5).
    message = _items_by_account(db, job.id)[broken.id].message
    assert CARD_NUMBER not in message
    assert "[redacted]" in message


# --- the security rule: card data is never persisted (PLAN.md §5) -----------
def test_only_the_card_number_is_persisted_and_only_on_the_order_row(db, monkeypatch):
    """The owner's decision of 25 Oct 2026: the number of the card an order
    was paid with is kept on that order row (spec FR-004's card column), so
    the ledger can be matched against a statement. Nothing else about the card
    is written anywhere — not the expiry, not the CVV, and the number itself
    not in the job's payload, its log, the item messages or the sessions."""
    over, good, nocard, cards = _order_batch(db)
    job = _job(
        db,
        JobType.order,
        payload={"product_link": "https://flipkart.com/p/1", "target": 1000.00},
    )

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [over.id, good.id, nocard.id],
            product_link="https://flipkart.com/p/1",
            qty=1,
            target=1000.00,
            cards=cards,
        )
    )

    assert CARD_NUMBER in [c["card_number"] for c in calls]

    job = _reload(db, job)
    orders = db.query(Order).all()
    by_account = {o.account_id: o for o in orders}
    assert by_account[good.id].card_number == CARD_NUMBER
    # An over-target attempt charged nothing, so there is no statement line to
    # match and its row keeps no card number (review of 26 Oct 2026).
    assert by_account[over.id].card_number is None
    assert nocard.id not in by_account

    def without_card(row):
        return {k: v for k, v in _row_values(row).items() if k != "card_number"}

    haystack = "\n".join(
        [
            job.log or "",
            repr(job.payload),
            repr([without_card(o) for o in orders]),
            repr([i.message for i in db.query(JobItem).all()]),
            repr([_row_values(s) for s in db.query(SessionModel).all()]),
        ]
    )
    for secret in (CARD_NUMBER, OVER_TARGET_CARD, CARD_CVV, "2031"):
        assert secret not in haystack, f"{secret!r} leaked beyond the order row"

    # The number's column is the only card-shaped column there is.
    assert [c.name for c in Order.__table__.columns if _looks_like_card(c.name)] == ["card_number"]


def test_a_cash_on_delivery_order_row_holds_no_card_number(db, monkeypatch):
    """COD runs carry no card, and a row must not inherit one from anywhere."""
    account = _account(db, "cod@test.com")
    job = _job(db, JobType.order, payload={"product_link": "https://flipkart.com/p/1"})
    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, _calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)
    asyncio.run(
        tasks.run_order_job(
            job.id, [account.id], product_link="https://flipkart.com/p/1", qty=1, target=None,
            cards=None, payment_mode=tasks.PAYMENT_COD,
        )
    )
    rows = db.query(Order).filter(Order.account_id == account.id).all()
    assert rows and all(row.card_number is None for row in rows)


def _row_values(row):
    """A row's columns for a secret search — without its timestamps, whose
    microseconds can spell any four digits (a run's expiry year "2031" once
    turned up inside "…, 28, 820311)")."""
    from datetime import datetime as _datetime

    return {
        c.name: getattr(row, c.name)
        for c in row.__table__.columns
        if not isinstance(getattr(row, c.name), _datetime)
    }


# --- the optional arq adapter ------------------------------------------------
def test_arq_worker_imports_without_redis_configured():
    """It must be importable on a machine with no Redis (redis_url blank)."""
    from app.config import get_settings
    from app.jobs import arq_worker

    assert get_settings().redis_url == ""
    assert arq_worker.queue_enabled() is False
    assert arq_worker.WorkerSettings.redis_settings is not None  # built, not connected
    assert arq_worker.WorkerSettings.functions == [
        arq_worker.login_job,
        arq_worker.coupon_job,
    ]


def test_order_jobs_are_never_queueable():
    """Queueing an order would serialise card data into Redis (PLAN.md §5)."""
    from app.jobs import arq_worker

    assert JobType.order not in arq_worker.QUEUE_SAFE_TYPES
    with pytest.raises(ValueError, match="never queued"):
        asyncio.run(arq_worker.enqueue_job(JobType.order, 1, [1]))

    # ...and a queue-safe type with no broker simply tells the caller to run
    # the job in-process, rather than trying to connect.
    assert asyncio.run(arq_worker.enqueue_job(JobType.login, 1, [1])) is None


def _looks_like_card(name: str) -> bool:
    return any(token in name.lower() for token in ("card", "cvv", "expiry"))
