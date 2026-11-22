"""Fixes from the BulkOrderBuddy comparison audit of 19 Nov 2026 ("Fix first").

Each test names the audit line it guards.
"""

from __future__ import annotations

import asyncio

from app.db.models import Job, Order, OrderStatus
from app.jobs import tasks
from tests.test_quantity_run import (  # the quantity-run helpers and fixtures
    PRODUCT,
    _accounts,
    _address_book,  # noqa: F401 — autouse fixture, re-exported for this module
    _cards,
    _fake_browser,
    _job,
    _order_stub,
    _serial,
    limits,  # noqa: F401 — fixture
)


def test_coupon_codes_do_not_break_the_runs_unit_counting(db, monkeypatch, limits):
    """Audit: "Coupon codes together with a Total Quantity break the run's unit
    counting". The coupon loop reused the variable holding the order's budget
    claim, so a failed order kept its units and the run bought less than asked.
    """
    accounts = _accounts(db, 5)
    _serial(db)
    job = _job(db)

    async def fake_apply_coupon(page, code, *, result_wait=5):
        return "applied"

    monkeypatch.setattr(tasks, "apply_coupon", fake_apply_coupon)
    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub(over_phones={accounts[0].phone})
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
            coupon_codes=["SAVE10"],
        )
    )

    placed = db.query(Order).filter(Order.status == OrderStatus.placed).all()
    assert sum(o.qty for o in placed) == 4  # the over-target order gave its 2 back
    assert all(o.order_id for o in placed)
    db.expire_all()
    assert "placed 4 of 4 unit(s) requested" in db.get(Job, job.id).log


# --- purchase limits on every run --------------------------------------------
import pytest  # noqa: E402

from app.db.models import JobItem, JobItemStatus, LimitScope, UnitLimit  # noqa: E402
from app.core import unit_limits  # noqa: E402


def _items(db, job_id):
    db.expire_all()
    return {i.account_id: i for i in db.query(JobItem).filter(JobItem.job_id == job_id).all()}


@pytest.mark.parametrize("qty_mode, expect_units", [("upto", [1]), ("minimum", [])])
def test_limits_apply_without_a_total_quantity(db, monkeypatch, limits, qty_mode, expect_units):
    """Audit: "Purchase limits are skipped completely when Total Quantity is
    left blank". A capped account is passed over, a tight one orders only what
    its limit leaves (Upto) or is passed over (Minimum)."""
    accounts = _accounts(db, 2)
    _serial(db)
    job = _job(db)
    limits.allowances[accounts[0].id] = 0
    limits.allowances[accounts[1].id] = 1
    monkeypatch.setattr(tasks, "browser_page", _fake_browser([]))
    stub, calls = _order_stub()
    monkeypatch.setattr(tasks, "run_order", stub)

    asyncio.run(
        tasks.run_order_job(
            job.id,
            [a.id for a in accounts],
            product_link=PRODUCT,
            qty=2,
            qty_mode=qty_mode,
            cards=_cards(accounts),
            brand="vivo",
        )
    )

    assert [c["qty"] for c in calls] == expect_units
    items = _items(db, job.id)
    assert items[accounts[0].id].status == JobItemStatus.skipped
    assert "unit limit" in items[accounts[0].id].message


def test_a_model_limit_matches_the_product_title(db):
    """Audit: "model limits never match". The run carries the product title the
    preview read; a model limit naming the model applies, and orders whose
    title contains it count against it."""
    db.add(UnitLimit(scope=LimitScope.model, value="T5x 5G", max_units=1, window_days=30, enabled=True))
    db.add(UnitLimit(scope=LimitScope.model, value="Note 14", max_units=1, window_days=30, enabled=True))
    db.commit()
    (account,) = _accounts(db, 1)
    facts = unit_limits.ProductFacts(model="vivo T5x 5G (Blue, 128 GB)")

    applicable = unit_limits.applicable(db, facts)
    assert [limit.value for limit in applicable] == ["T5x 5G"]
    assert unit_limits.allowance(db, account.id, facts) == 1

    db.add(Order(account_id=account.id, status=OrderStatus.placed, qty=1, model="vivo T5x 5G (Green, 128 GB)"))
    db.commit()
    assert unit_limits.allowance(db, account.id, facts) == 0
    # A different model is not counted, and "_" in a value is not a wildcard.
    assert unit_limits.model_matches("T5x_5G", "vivo T5x 5G") is False


# --- the price cap: per unit, on the cost before SuperCoins / wallet ---------
from app.automation import ordering as order_flow  # noqa: E402
from app.core import selectors as S  # noqa: E402
from tests.fakes import FakePage  # noqa: E402
from tests.test_ordering_gate import ADDR, CARD, GST  # noqa: E402


def test_per_unit_rounds_and_never_loosens():
    assert order_flow.per_unit(1000.0, 2) == 500.0
    assert order_flow.per_unit(1000.01, 1) == 1000.01
    assert order_flow.per_unit(900.0, 0) == 900.0  # never divided into a looser cap
    assert order_flow.per_unit(None, 2) is None


def _with_balances(monkeypatch, totals: list[float | None], toggle: str):
    reads = iter(totals)

    async def fake_total(page):
        return next(reads)

    async def fake_toggle(page, row_xpath, **kwargs):
        return toggle

    monkeypatch.setattr(order_flow, "read_final_amount", fake_total)
    monkeypatch.setattr(order_flow, "use_balance_toggle", fake_toggle)


def _order(page, target):
    return asyncio.run(
        order_flow.run_order(
            page, product_link="https://flipkart.com/x", address=ADDR, gst=GST,
            card=CARD, qty=1, target=target, use_supercoins=True,
        )
    )


def test_supercoins_do_not_hide_an_over_cap_price(monkeypatch):
    """Audit: the cap was checked "only on what is left to pay after SuperCoins
    and wallet money". ₹1,050 with ₹100 of SuperCoins leaves ₹950 to pay, but
    the item costs ₹1,050 — over a ₹1,000 cap, so not ordered."""
    _with_balances(monkeypatch, [1050.0, 950.0, 950.0], order_flow.TOGGLE_APPLIED)
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹950.00"})
    result = _order(page, 1000.0)
    assert result["status"] == OrderStatus.over_target
    assert result["unit_amount"] == 1050.0
    assert not page.submitted


def test_the_rupees_supercoins_took_off_are_kept(monkeypatch):
    _with_balances(monkeypatch, [1000.0, 900.0, 900.0], order_flow.TOGGLE_APPLIED)
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹900.00"})
    result = _order(page, 1000.0)
    assert result["status"] == OrderStatus.placed
    assert result["supercoins_amount"] == 100.0


def test_an_unreadable_total_before_supercoins_refuses(monkeypatch):
    _with_balances(monkeypatch, [None, 950.0], order_flow.TOGGLE_APPLIED)
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹950.00"})
    result = _order(page, 1000.0)
    assert result["status"] == OrderStatus.failed
    assert not page.submitted


def test_supercoins_not_offered_leaves_the_cap_on_the_total(monkeypatch):
    _with_balances(monkeypatch, [990.0, 990.0], order_flow.TOGGLE_ABSENT)
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹990.00"})
    result = _order(page, 1000.0)
    assert result["status"] == OrderStatus.placed
    assert page.submitted


# --- card bookkeeping ---------------------------------------------------------
@pytest.mark.parametrize("mode", ["card", "gv_card", "cod_card"])
def test_every_card_paid_mode_keeps_the_card_number(mode):
    """Audit: "Gift voucher plus card orders do not keep the card number"."""
    columns = tasks._order_columns(
        1,
        {"status": OrderStatus.placed, "payment_mode": mode, "order_id": "OD1"},
        product_link=PRODUCT, qty=1, target=None, address=ADDR, gst=GST,
        card_number="4111111111111111",
    )
    assert columns["card_number"] == "4111111111111111"


def test_cod_and_gift_voucher_only_keep_no_card_number():
    for mode in ("cod", "gv"):
        columns = tasks._order_columns(
            1,
            {"status": OrderStatus.placed, "payment_mode": mode, "order_id": "OD1"},
            product_link=PRODUCT, qty=1, target=None, address=ADDR, gst=GST,
            card_number="4111111111111111",
        )
        assert columns["card_number"] is None


# --- sign-in refusals ----------------------------------------------------------
import contextlib  # noqa: E402

from app.automation.login import LoginRefused, refusal_in_text  # noqa: E402
from app.db.models import Account, AccountStatus, JobStatus, JobType  # noqa: E402


def test_refusal_wording_maps_to_an_account_status():
    assert refusal_in_text("You have exceeded the maximum number of attempts").status == "multiple_attempts"
    assert refusal_in_text("Your account has been blocked. Contact us.").status == "blocked"
    assert refusal_in_text("Please enter the OTP sent to you") is None
    assert refusal_in_text(None) is None


@pytest.mark.parametrize("status", ["blocked", "multiple_attempts"])
def test_a_refused_sign_in_marks_the_account(db, monkeypatch, status):
    """Audit: "The dashboard's Blocked and Multiple attempts columns can never
    be above 0" — every refusal read as "session invalid"."""
    account = Account(email="r@t.in", status=AccountStatus.new)
    db.add(account)
    job = Job(type=JobType.login, status=JobStatus.queued)
    db.add(job)
    db.commit()

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_kwargs):
        yield None, None

    async def fake_run_login(page, email, *_args, **_kwargs):
        raise LoginRefused(status, "Flipkart says so")

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_login", fake_run_login)
    asyncio.run(tasks.run_login_job(job.id, [account.id]))

    db.expire_all()
    assert db.get(Account, account.id).status == AccountStatus(status)


# --- the dashboard's SuperCoins figure is rupees -------------------------------
def test_supercoins_applied_is_summed_in_rupees(db):
    """Audit: "'SuperCoins applied' counts orders, not rupees"."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import stats
    from app.core.security import create_access_token

    (account,) = _accounts(db, 1)
    for amount, state in ((120.5, "delivered"), (79.5, "delivered"), (500.0, "in_transit")):
        db.add(Order(account_id=account.id, status=OrderStatus.placed, used_supercoins=True,
                     supercoins_amount=amount, delivery_state=state))
    db.commit()
    app = FastAPI()
    app.include_router(stats.router)
    client = TestClient(app, headers={"Authorization": f"Bearer {create_access_token('t', role='admin')}"})
    body = client.get("/stats/balances").json()
    assert body["supercoins_orders_delivered"] == 2
    assert body["supercoins_applied_amount"] == 200.0
