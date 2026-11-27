"""Tests for ordering N units across accounts (ROADMAP §9).

No browser and no Flipkart: ``app.jobs.tasks.browser_page`` and
``app.jobs.tasks.run_order`` are monkeypatched, so these exercise the part that
can cost money — how many orders a run places, of how many units each, and
which accounts it passes over.

The headline test is ``test_a_quantity_run_never_overshoots_the_target``. A run
is concurrent, so the naive "stop once enough has been placed" would have every
in-flight worker see the same "2 units still wanted" and order for it; the
budget's claim/release is what makes the count exact.

``tasks`` imports the unit-limit engine's entry points as plain module-level
names, so the ``limits`` fixture patches ``unit_allowance`` rather than seeding
limit rows for every case: how a limit is *counted* is ``app.core.unit_limits``'
own business (and has its own tests), while these are about what the run does
with the answer. The skip-message test does seed a real limit, because the
wording the operator reads comes from the engine.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.automation.ordering import CardInfo
from app.core import app_settings
from app.core.security import create_access_token
from app.core.unit_limits import ProductFacts
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Gst,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    LimitScope,
    Order,
    OrderStatus,
    Session as SessionModel,
    UnitLimit,
)
from app.jobs import tasks
from tests.fakes import FakeContext, FakePage

PRODUCT = "https://www.flipkart.com/vivo-t5x/p/itm1"
CARD_NUMBER = "4111111111111111"


# --- helpers -----------------------------------------------------------------
def _accounts(db, count: int) -> list[Account]:
    """``count`` accounts, each with a session and a distinctive phone.

    The phone is how an order stub tells the accounts apart: it is the one
    per-account value that reaches ``run_order`` (as ``address.phone``).
    """
    rows = [
        Account(email=f"q{i}@test.com", phone=f"9{i:09d}", status=AccountStatus.new)
        for i in range(count)
    ]
    db.add_all(rows)
    db.commit()
    for row in rows:
        db.add(
            SessionModel(
                account_id=row.id, cookies=[{"name": "SN", "value": row.email}], valid=True
            )
        )
    db.commit()
    return rows


def _job(db, payload=None) -> Job:
    job = Job(type=JobType.order, status=JobStatus.queued, payload=payload)
    db.add(job)
    db.commit()
    return job


def _cards(accounts: list[Account]) -> dict[int, CardInfo]:
    return {
        account.id: CardInfo(number=CARD_NUMBER, month="12", year="2031", cvv="321")
        for account in accounts
    }


def _fake_browser(calls: list[dict]):
    """Stand-in for core.browser.browser_page: yields (context, page)."""

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        calls.append(kwargs)
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    return fake_browser_page


def _order_stub(*, fail_phones: set[str] | None = None, over_phones: set[str] | None = None):
    """``run_order`` stand-in recording the units each order was asked for.

    ``fail_phones`` raise (the driver died), ``over_phones`` come back over
    target (nothing charged). Both are failures the budget must give units back
    for, by two different code paths.
    """
    failing = fail_phones or set()
    over = over_phones or set()
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
        calls.append({"phone": address.phone, "qty": qty, "target": target})
        if address.phone in failing:
            raise RuntimeError("driver died")
        if address.phone in over:
            return {"status": OrderStatus.over_target, "amount": 9999.0, "target": target}
        return {
            "status": OrderStatus.placed,
            "amount": 500.0,
            "target": target,
            "order_id": f"OD-{address.phone}",
            "model": "T5x 5G",
            "color": "Blue",
            "pin": address.pin,
            "qty": qty,
        }

    return fake_run_order, calls


def _items(db, job_id: int) -> dict[int, JobItem]:
    db.expire_all()
    return {
        item.account_id: item
        for item in db.query(JobItem).filter(JobItem.job_id == job_id).all()
    }


def _serial(db) -> None:
    """Run one account at a time.

    Releasing a failed claim only helps accounts that have not claimed yet, so
    a test about a released slot being re-used has to pin the concurrency to
    make the ordering deterministic rather than lucky (see test_unit_budget).
    """
    app_settings.set_overrides(db, {"max_concurrency": 1})
    db.commit()


@pytest.fixture(autouse=True)
def _address_book(db):
    """Every order run ships to an Address Book address; one default pair is on
    file so these tests can be about quantities, not about the destination."""
    gst = Gst(label="VIVO-HUB", gstin="29VIVOHUB0001Z5", business_name="Vivo Hub", is_default=True)
    db.add(gst)
    db.commit()
    db.add(
        Address(
            label="hub", name="Vivo Hub", line1="1 Hub Road", pincode="560001", gst_id=gst.id, is_default=True
        )
    )
    db.commit()


@pytest.fixture
def limits(monkeypatch):
    """Stand in for ``app.core.unit_limits``, per account.

    ``allowances[account_id]`` is what the engine reports; an account not in
    the map is unlimited (``None``), which is the common case.
    """
    allowances: dict[int, int | None] = {}
    seen: list[tuple[int, object]] = []

    def fake_allowance(db, account_id, facts):
        seen.append((account_id, facts))
        return allowances.get(account_id)

    assert tasks.ProductFacts is ProductFacts  # the real engine, not a stand-in
    monkeypatch.setattr(tasks, "unit_allowance", fake_allowance)
    return SimpleNamespace(allowances=allowances, seen=seen)


# --- the headline: no overshoot ----------------------------------------------
def test_a_quantity_run_never_overshoots_the_target(db, monkeypatch, limits):
    """10 units, 4 per order, 20 willing accounts => 3 orders of 4 + 4 + 2.

    Every account could have ordered. Placing 20 orders — or even 4 — would be
    real money spent and real quota burned, so the exact count is the test.
    """
    accounts = _accounts(db, 20)
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            cards=_cards(accounts),
            total_quantity=10,
            qty_per_order=4,
            brand="vivo",
        )
    )

    assert len(calls) == 3  # three orders, not twenty
    assert sorted(c["qty"] for c in calls) == [2, 4, 4]
    assert sum(c["qty"] for c in calls) == 10

    orders = db.query(Order).all()
    assert len(orders) == 3
    assert sum(o.qty for o in orders) == 10
    assert all(o.status == OrderStatus.placed for o in orders)

    # The other 17 accounts were passed over, not failed: nothing went wrong.
    items = _items(db, job.id)
    skipped = [i for i in items.values() if i.status == JobItemStatus.skipped]
    assert len(skipped) == 17
    assert all(i.message == "target quantity reached" for i in skipped)

    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == JobStatus.completed
    assert "placed 10 of 10 unit(s) requested" in job.log


def test_once_the_target_is_met_the_rest_cost_no_quota_query(db, monkeypatch, limits):
    """Accounts past the target are skipped without asking about their quota.

    At real scale that matters: 5000 accounts selected for 10 units means 4990
    that were never going to order, and a quota lookup is several queries each.
    """
    accounts = _accounts(db, 20)
    _serial(db)
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            cards=_cards(accounts),
            total_quantity=4,
            qty_per_order=2,
            brand="vivo",
        )
    )

    assert len(calls) == 2
    assert [account_id for account_id, _ in limits.seen] == [
        accounts[0].id,
        accounts[1].id,
    ]
    # ...and the 18 untouched accounts still got a reason, not silence.
    items = _items(db, job.id)
    assert all(
        items[a.id].message == "target quantity reached" for a in accounts[2:]
    )


def test_a_failed_order_frees_its_units_for_another_account(db, monkeypatch, limits):
    """Both kinds of failure give the slot back, and the target is still met.

    The first account's driver dies and the second comes back over target —
    neither bought anything, so their 2+2 units must reach accounts further
    down the list rather than counting against the target.
    """
    accounts = _accounts(db, 5)
    _serial(db)
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub(
        fail_phones={accounts[0].phone}, over_phones={accounts[1].phone}
    )
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            target=1000.0,
            cards=_cards(accounts),
            total_quantity=4,
            qty_per_order=2,
        )
    )

    # Four attempts: two that bought nothing, then two that met the target.
    assert [c["phone"] for c in calls] == [a.phone for a in accounts[:4]]

    placed = db.query(Order).filter(Order.status == OrderStatus.placed).all()
    assert sum(o.qty for o in placed) == 4
    assert {o.account_id for o in placed} == {accounts[2].id, accounts[3].id}

    items = _items(db, job.id)
    assert items[accounts[0].id].status == JobItemStatus.failed
    assert items[accounts[1].id].status == JobItemStatus.failed
    assert items[accounts[4].id].message == "target quantity reached"

    db.expire_all()
    assert "placed 4 of 4 unit(s) requested" in db.get(Job, job.id).log


def test_an_unconfirmed_order_keeps_its_units(db, monkeypatch, limits):
    """The mirror image of the test above, and the costlier one to get wrong.

    An unconfirmed order was submitted and may exist. Freeing its units would
    send them to the next account — two orders for one requested. So 4 units
    at 2 per order, with the first unconfirmed, is exactly two orders.
    """
    accounts = _accounts(db, 5)
    _serial(db)
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    placed_stub, calls = _order_stub()
    unsure_phone = accounts[0].phone

    async def stub(page, *, address, qty=1, target=None, **extra):
        if address.phone == unsure_phone:
            calls.append({"phone": address.phone, "qty": qty, "target": target})
            return {
                "status": OrderStatus.unconfirmed,
                "amount": 17707.0,
                "target": target,
                "qty": qty,
                "order_id": None,
                "error": "Flipkart's order confirmation page did not appear",
            }
        return await placed_stub(page, address=address, qty=qty, target=target, **extra)

    monkeypatch.setattr(tasks, "run_order", stub)
    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            target=18000.0,
            cards=_cards(accounts),
            total_quantity=4,
            qty_per_order=2,
        )
    )

    # Two orders, not three: the unconfirmed one's units were never handed on.
    assert [c["phone"] for c in calls] == [a.phone for a in accounts[:2]]

    unsure = db.query(Order).filter(Order.status == OrderStatus.unconfirmed).one()
    assert unsure.account_id == accounts[0].id
    assert unsure.placed_at is not None  # counted in unit-limit windows

    items = _items(db, job.id)
    assert items[accounts[0].id].message.startswith("unconfirmed:")
    assert "check this account" in items[accounts[0].id].message

    db.expire_all()
    log = db.get(Job, job.id).log
    assert "placed 2 of 4 unit(s) requested" in log
    assert "2 more submitted but UNCONFIRMED" in log


def test_a_crash_after_the_order_was_recorded_leaves_an_unconfirmed_row(db, monkeypatch, limits):
    """run_order reserved the row (the order was about to be, or was, sent) and
    then the browser died. The reserved row must stand as unconfirmed, its
    units stay spent, and no second 'failed' row may appear."""
    accounts = _accounts(db, 3)
    _serial(db)
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))

    async def stub(page, *, address, qty=1, target=None, reserve=None, **_):
        reserve({"status": OrderStatus.unconfirmed, "amount": 17707.0, "qty": qty,
                 "payment_mode": tasks.PAYMENT_COD, "target": target, "model": "vivo T5 Lite"})
        raise RuntimeError("browser crashed mid-confirmation")

    monkeypatch.setattr(tasks, "run_order", stub)
    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            target=18000.0,
            cards=_cards(accounts),
            payment_mode=tasks.PAYMENT_COD,
            total_quantity=2,
            qty_per_order=2,
        )
    )

    rows = db.query(Order).all()
    assert len(rows) == 1  # the reservation, and nothing else
    assert rows[0].status == OrderStatus.unconfirmed
    assert rows[0].account_id == accounts[0].id
    assert rows[0].amount == 17707.0
    assert rows[0].payment_mode == tasks.PAYMENT_COD
    assert rows[0].placed_at is not None

    items = _items(db, job.id)
    assert items[accounts[0].id].status == JobItemStatus.unconfirmed
    assert items[accounts[0].id].message.startswith("unconfirmed:")
    # The units stayed spent: nobody else was asked to order them.
    assert items[accounts[1].id].message == "target quantity reached"

    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == JobStatus.completed  # not "failed": that says retry
    assert job.unconfirmed_items == 1
    assert job.failed_items == 0


def test_a_placed_order_completes_its_reserved_row_rather_than_adding_one(db, monkeypatch, limits):
    accounts = _accounts(db, 1)
    job = _job(db)
    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))

    async def stub(page, *, address, qty=1, target=None, reserve=None, **_):
        row_id = reserve({"status": OrderStatus.unconfirmed, "amount": 500.0, "qty": qty,
                          "payment_mode": tasks.PAYMENT_CARD, "target": target})
        return {"status": OrderStatus.placed, "amount": 500.0, "target": target, "qty": qty,
                "order_id": "OD428123456789100", "reservation": row_id,
                "payment_mode": tasks.PAYMENT_CARD, "model": "T5x 5G"}

    monkeypatch.setattr(tasks, "run_order", stub)
    asyncio.run(
        tasks.run_order_job(job.id, [accounts[0].id], product_link=PRODUCT, cards=_cards(accounts))
    )

    rows = db.query(Order).all()
    assert len(rows) == 1
    assert rows[0].status == OrderStatus.placed
    assert rows[0].order_id == "OD428123456789100"


def test_settling_a_reserved_row_keeps_its_submission_time(db, monkeypatch, limits):
    """placed_at is when the order was submitted (the reservation), not when
    the outcome was written down after the confirmation wait."""
    from datetime import datetime, timedelta

    accounts = _accounts(db, 1)
    job = _job(db)
    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stamped: list[datetime] = []

    async def stub(page, *, address, qty=1, target=None, reserve=None, **_):
        row_id = reserve({"status": OrderStatus.unconfirmed, "amount": 500.0, "qty": qty,
                          "payment_mode": tasks.PAYMENT_CARD, "target": target})
        # Pretend the reservation happened a minute ago, as a long wait would.
        with tasks._session() as inner:
            row = inner.get(Order, row_id)
            row.placed_at = row.placed_at - timedelta(minutes=1)
            stamped.append(row.placed_at)
            inner.commit()
        return {"status": OrderStatus.placed, "amount": 500.0, "target": target, "qty": qty,
                "order_id": "OD428123456789100", "reservation": row_id,
                "payment_mode": tasks.PAYMENT_CARD}

    monkeypatch.setattr(tasks, "run_order", stub)
    asyncio.run(
        tasks.run_order_job(job.id, [accounts[0].id], product_link=PRODUCT, cards=_cards(accounts))
    )

    row = db.query(Order).one()
    assert row.status == OrderStatus.placed
    assert row.placed_at == stamped[0]  # not re-stamped at settle time


def test_every_order_row_names_the_run_that_made_it(db, monkeypatch, limits):
    """So a finished run can deep-link to its own rows once order history has
    a page of its own, and so an order id is stored the way syncs match it."""
    accounts = _accounts(db, 1)
    job = _job(db)
    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))

    async def stub(page, *, address, qty=1, target=None, reserve=None, **_):
        return {"status": OrderStatus.placed, "amount": 500.0, "target": target, "qty": qty,
                "order_id": " od428123456789100 ", "payment_mode": tasks.PAYMENT_CARD}

    monkeypatch.setattr(tasks, "run_order", stub)
    asyncio.run(
        tasks.run_order_job(job.id, [accounts[0].id], product_link=PRODUCT, cards=_cards(accounts))
    )

    row = db.query(Order).one()
    assert row.job_id == job.id
    assert row.order_id == "OD428123456789100"  # trimmed and upper-cased at write time
    assert row.created_at is not None


def test_a_browser_that_fails_to_close_does_not_unplace_the_order(db, monkeypatch, limits):
    """run_order returned "placed", then the browser crashed on the way out.

    The old handler caught that and recorded the order as failed — giving its
    units back, so the next account would buy them again.
    """
    accounts = _accounts(db, 3)
    _serial(db)
    job = _job(db)

    @contextlib.asynccontextmanager
    async def crashing_browser(**kwargs):
        page = FakePage()
        yield page.context, page
        raise RuntimeError("browser crashed while closing")

    monkeypatch.setattr(tasks, "browser_page", crashing_browser)
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)
    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            cards=_cards(accounts),
            total_quantity=2,
            qty_per_order=2,
        )
    )

    assert len(calls) == 1  # the units stayed spent; nobody ordered them again
    order = db.query(Order).one()
    assert order.status == OrderStatus.placed
    assert order.order_id == f"OD-{accounts[0].phone}"


# --- the attempt cap ----------------------------------------------------------
def test_total_attempts_caps_how_many_accounts_are_tried(db, monkeypatch, limits):
    """Only the first ``total_attempts`` accounts may be burned.

    The target is deliberately out of reach, so nothing but the cap can stop
    the run.
    """
    accounts = _accounts(db, 6)
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            cards=_cards(accounts),
            total_quantity=100,
            qty_per_order=1,
            total_attempts=2,
        )
    )

    assert [c["phone"] for c in calls] == [accounts[0].phone, accounts[1].phone]
    assert db.query(Order).count() == 2

    items = _items(db, job.id)
    for account in accounts[2:]:
        assert items[account.id].status == JobItemStatus.skipped
        assert items[account.id].message == "attempt limit reached (2 accounts)"

    db.expire_all()
    assert "4 attempt limit" in db.get(Job, job.id).log


# --- platform unit limits -----------------------------------------------------
def test_a_blocked_account_is_skipped_by_name_and_keeps_its_budget(
    db, monkeypatch, limits
):
    """An account with no quota left is passed over, and costs no units.

    If the skip silently ate a claim, a run of 4 units over three accounts
    would stop at 2 — the operator would see a short run with no reason given.
    """
    accounts = _accounts(db, 3)
    _serial(db)
    limits.allowances[accounts[0].id] = 0
    # A real limit row, so the skip message is built the way the operator will
    # read it rather than from a fallback.
    db.add(
        UnitLimit(
            scope=LimitScope.brand, value="vivo", max_units=16, window_days=31
        )
    )
    db.commit()
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            cards=_cards(accounts),
            total_quantity=4,
            qty_per_order=2,
            brand="vivo",
            category="Mobile",
        )
    )

    items = _items(db, job.id)
    blocked = items[accounts[0].id]
    assert blocked.status == JobItemStatus.skipped
    # Which cap bound, in the operator's own words — not just "skipped".
    assert blocked.message == (
        "unit limit reached — 16 units of brand 'vivo' per 31 days, "
        "nothing left in its window"
    )

    # No browser, no order, and the units went to the other two accounts.
    assert [c["phone"] for c in calls] == [accounts[1].phone, accounts[2].phone]
    assert sum(o.qty for o in db.query(Order).all()) == 4
    assert db.query(Order).filter(Order.account_id == accounts[0].id).count() == 0

    # The engine was asked about every account, with this run's facts.
    assert {account_id for account_id, _ in limits.seen} == {a.id for a in accounts}
    assert all(
        facts.brand == "vivo" and facts.category == "Mobile" and facts.model is None
        for _, facts in limits.seen
    )

    db.expire_all()
    assert "1 unit limit" in db.get(Job, job.id).log


def test_a_block_with_no_nameable_limit_still_says_what_was_being_bought(
    db, monkeypatch, limits
):
    """A limit edited away mid-run must not turn the reason into nothing.

    The engine says 0 units but can no longer point at a row, so the message
    falls back to what the run was buying — still a reason, not a bare "skipped".
    """
    account = _accounts(db, 1)[0]
    limits.allowances[account.id] = 0
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [account.id],
            product_link=PRODUCT,
            cards=_cards([account]),
            total_quantity=4,
            qty_per_order=2,
            brand="vivo",
        )
    )

    assert calls == []
    assert _items(db, job.id)[account.id].message == (
        "unit limit reached — brand vivo, nothing left in its window"
    )


def test_an_allowance_below_the_batch_size_orders_only_the_allowance(
    db, monkeypatch, limits
):
    """3 units left on the account, 4 per order => an order of 3.

    Ordering the full batch is what trips the platform's cap and flags the
    account, which is the whole reason the check happens before the order.
    """
    account = _accounts(db, 1)[0]
    limits.allowances[account.id] = 3
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [account.id],
            product_link=PRODUCT,
            cards=_cards([account]),
            total_quantity=10,
            qty_per_order=4,
            brand="vivo",
        )
    )

    assert [c["qty"] for c in calls] == [3]
    order = db.query(Order).one()
    assert order.qty == 3
    assert order.status == OrderStatus.placed

    db.expire_all()
    assert "placed 3 of 10 unit(s) requested" in db.get(Job, job.id).log


def test_brand_and_category_land_on_every_order_row(db, monkeypatch, limits):
    """Recorded placed or not, so a later limit check is a SUM over this table."""
    accounts = _accounts(db, 2)
    _serial(db)
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, _calls = _order_stub(over_phones={accounts[1].phone})
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            target=1000.0,
            cards=_cards(accounts),
            total_quantity=4,
            qty_per_order=2,
            brand="vivo",
            category="Mobile / Handset",
        )
    )

    orders = {o.account_id: o for o in db.query(Order).all()}
    assert len(orders) == 2
    assert all(o.brand == "vivo" for o in orders.values())
    assert all(o.category == "Mobile / Handset" for o in orders.values())
    # Including the one that bought nothing: an attempt that was blocked by the
    # price target still has to be classified if it is ever to be counted.
    assert orders[accounts[1].id].status == OrderStatus.over_target


def test_an_unclassified_quantity_run_says_so_on_the_job(db, monkeypatch, limits):
    """No brand and no category means only wildcard limits can match.

    The run still goes ahead — a wildcard limit does cap it — but it must not
    look limit-checked while a "vivo 16 / 31 days" cap sits there matching
    nothing, so the transcript spells out what did and did not apply.
    """
    account = _accounts(db, 1)[0]
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [account.id],
            product_link=PRODUCT,
            cards=_cards([account]),
            total_quantity=2,
            qty_per_order=2,
        )
    )

    assert [c["qty"] for c in calls] == [2]
    assert [facts for _, facts in limits.seen] == [ProductFacts()]

    db.expire_all()
    assert "no brand or category given" in db.get(Job, job.id).log


def test_the_real_limit_engine_is_wired_in():
    """``tasks`` must hold the engine's own entry points, not a look-alike.

    The one test that fails if the import drifts away from the module the
    ``limits`` fixture stands in for.
    """
    from app.core import unit_limits

    assert tasks.unit_allowance is unit_limits.allowance
    assert tasks.ProductFacts is unit_limits.ProductFacts
    assert tasks.blocking_limit is unit_limits.blocking_limit
    assert tasks.describe_limit is unit_limits.describe


# --- back-compat: no total_quantity, no change --------------------------------
def test_without_a_total_quantity_it_is_one_order_per_account(db, monkeypatch, limits):
    """Every account orders ``qty`` units when no limit stands in the way.

    No budget and no cap. Since the audit of 19 Nov 2026 the limit engine IS
    consulted on such a run (it used to be skipped, which let a run without a
    Total Quantity buy past an account's limits); with no limit it changes nothing.
    """
    accounts = _accounts(db, 3)
    job = _job(db)

    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            qty=2,
            cards=_cards(accounts),
        )
    )

    assert len(calls) == 3
    assert [c["qty"] for c in calls] == [2, 2, 2]
    assert len(limits.seen) == 3  # every account's limits asked once

    orders = db.query(Order).all()
    assert len(orders) == 3
    assert all(o.qty == 2 for o in orders)
    assert all(o.brand is None and o.category is None for o in orders)

    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == JobStatus.completed
    assert "unit(s) requested" not in (job.log or "")  # no quantity summary


# --- POST /jobs/order ---------------------------------------------------------
@pytest.fixture
def api(db):
    """The real jobs router on a local app, with the engine task recorded.

    ``app/main.py`` is not ours to change, so the router is mounted here — the
    same pattern the other jobs-API tests use.
    """
    from app.api import jobs as jobs_api

    application = FastAPI()
    application.include_router(jobs_api.router)
    client = TestClient(
        application,
        headers={"Authorization": f"Bearer {create_access_token('tester', role='admin')}"},
    )
    return client


@pytest.fixture
def started(monkeypatch):
    """Records what ``POST /jobs/order`` handed the engine, without running it."""
    from app.api import jobs as jobs_api

    calls: list[dict] = []

    async def _noop() -> None:
        return None

    def recorder(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return _noop()

    monkeypatch.setattr(jobs_api, "run_order_job", recorder)
    return calls


def _form(**extra) -> dict:
    """A minimal COD order form — COD keeps the card upload out of these. A
    run states its price cap (audit F-23), so the form carries one."""
    return {"product_link": PRODUCT, "payment_mode": "cod", "target": "18250", **extra}


@pytest.mark.parametrize(
    "field", ["qty_per_order", "total_quantity", "total_attempts"]
)
def test_a_count_below_one_is_a_422_and_starts_nothing(db, api, started, field):
    """Zero units, zero per order or zero attempts would run and do nothing."""
    _accounts(db, 2)

    response = api.post("/jobs/order", data=_form(**{field: 0}))

    assert response.status_code == 422, response.text
    assert field in response.json()["detail"]
    assert started == []
    assert db.query(Job).count() == 0  # not even a queued row


@pytest.mark.parametrize(
    "field", ["qty_per_order", "total_quantity", "total_attempts"]
)
def test_a_negative_count_is_a_422_too(db, api, started, field):
    _accounts(db, 2)

    response = api.post("/jobs/order", data=_form(**{field: -3}))

    assert response.status_code == 422, response.text
    assert started == []
    assert db.query(Job).count() == 0


def test_the_quantity_fields_reach_the_engine_and_the_job_payload(db, api, started):
    _accounts(db, 2)

    response = api.post(
        "/jobs/order",
        data=_form(
            total_quantity=10,
            qty_per_order=4,
            total_attempts=6,
            cart_amount_limit=25000.5,
            max_delivery_days=3,
            brand=" vivo ",
            category="Mobile",
        ),
    )

    assert response.status_code == 201, response.text
    kwargs = started[0]["kwargs"]
    assert kwargs["total_quantity"] == 10
    assert kwargs["qty_per_order"] == 4
    assert kwargs["total_attempts"] == 6
    assert kwargs["cart_amount_limit"] == 25000.5
    assert kwargs["max_delivery_days"] == 3
    assert kwargs["brand"] == "vivo"  # trimmed
    assert kwargs["category"] == "Mobile"

    payload = response.json()["payload"]
    assert payload["total_quantity"] == 10
    assert payload["qty_per_order"] == 4
    assert payload["total_attempts"] == 6
    assert payload["cart_amount_limit"] == 25000.5
    assert payload["max_delivery_days"] == 3
    assert payload["brand"] == "vivo"
    assert payload["category"] == "Mobile"
    assert db.get(Job, response.json()["id"]).payload == payload


def test_a_request_without_the_new_fields_records_none_of_them(db, api, started):
    """Back-compat: an unchanged request's payload is unchanged.

    A form always sends its fields, so a blank one must read as "not given"
    rather than as a deliberate choice a historical run never made.
    """
    _accounts(db, 2)

    response = api.post(
        "/jobs/order", data=_form(brand="", category="   ", qty_per_order="")
    )

    assert response.status_code == 201, response.text
    payload = response.json()["payload"]
    assert set(payload) == {
        "product_link",
        "qty",
        "target",
        "payment_mode",
        "account_filter",
        "account_count",
        "started_by",
        "agent",
    }

    kwargs = started[0]["kwargs"]
    assert kwargs["total_quantity"] is None  # => one order per account
    assert kwargs["qty_per_order"] == 1
    assert kwargs["brand"] is None
    assert kwargs["category"] is None


# --- model-scoped caps must be checkable BEFORE the order (gap found in review)
def test_model_reaches_the_limit_engine_and_the_payload(db, api, started) -> None:
    """The tightest cap the operator relies on is per model ("T5x 5G 5/30 days").

    Order.model is only known after the product page is scraped, so unless the
    caller states the model up front, a model-scoped limit can never fire
    before the order is placed — which is the only time it matters.
    """
    _accounts(db, 2)

    response = api.post(
        "/jobs/order",
        data=_form(
            total_quantity=4, qty_per_order=2,
            brand="vivo", category="Mobile", model="T5x 5G",
        ),
    )

    assert response.status_code == 201, response.text
    assert response.json()["payload"]["model"] == "T5x 5G"
    assert started[0]["kwargs"]["model"] == "T5x 5G"
