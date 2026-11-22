"""An account's own GST and billing phone in an order run (package D, 26 Oct 2026).

A run picks one address and one GST for every account in it. A run that mixed
the accounts of two entities billed every order under that one GST, and the
input credit landed on the wrong registration. So an account may now carry a
GST of its own, which wins over the run's, and a billing phone, which is the
phone its orders are placed with. These tests are about exactly that: which GST
and which phone reach checkout, what the order row records, what the job says,
and that the checkout's own GST check holds the page to the account's GST.

No browser and no Flipkart: ``app.jobs.tasks.browser_page`` and ``run_order``
are stood in for (``tests.fakes.FakePage`` where the real ``run_order`` runs).
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from app import cli
from app.automation import ordering as order_flow
from app.core import selectors as S
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Gst,
    Job,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
)
from app.jobs import tasks
from tests.fakes import FakeContext, FakePage

PRODUCT = "https://www.flipkart.com/some-item/p/itm123"
#: GSTINs of a real GSTIN's shape, so the checkout's reader recognises them.
RUN_GSTIN = "29AABCB1234C1Z5"
ACCOUNT_GSTIN = "27AADCD5678E1Z3"
OTHER_GSTIN = "33AAECS4321F1ZK"


# --- helpers -------------------------------------------------------------------
def _gst(db, label: str, gstin: str, name: str, *, default: bool = False) -> Gst:
    row = Gst(label=label, gstin=gstin, business_name=name, is_default=default)
    db.add(row)
    db.commit()
    return row


def _address(db, *, phone: str | None = None, gst_id: int | None = None) -> Address:
    row = Address(
        label="hq",
        name="Blazecom Warehouse",
        phone=phone,
        line1="12 Dock Road",
        city="Bengaluru",
        state="Karnataka",
        pincode="560099",
        gst_id=gst_id,
        is_default=True,
    )
    db.add(row)
    db.commit()
    return row


def _account(
    db,
    email: str,
    *,
    phone: str | None = None,
    billing_phone: str | None = None,
    gst_id: int | None = None,
    with_session: bool = True,
) -> Account:
    account = Account(
        email=email, phone=phone, billing_phone=billing_phone, gst_id=gst_id, status=AccountStatus.session_valid
    )
    db.add(account)
    db.commit()
    if with_session:
        db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": email}], valid=True))
        db.commit()
    return account


def _job(db) -> Job:
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()
    return job


def _fake_browser(page_factory=FakePage):
    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = page_factory()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    return fake_browser_page


@pytest.fixture
def ordering(monkeypatch) -> list[dict]:
    """run_order stood in for; every call's account, address and GST recorded."""
    calls: list[dict] = []

    async def fake_run_order(page, *, product_link, address, gst, qty=1, target=None, **_extra):
        calls.append({"address": address, "gst": gst, "cookies": page.context._cookies})
        return {"status": OrderStatus.placed, "amount": 500.0, "target": target, "order_id": "OD1", "qty": qty}

    monkeypatch.setattr(tasks, "browser_page", _fake_browser())
    monkeypatch.setattr(tasks, "run_order", fake_run_order)
    return calls


def _run(job: Job, account_ids: list[int], **kwargs) -> None:
    """An order run to the end, Cash on Delivery so no card is involved."""
    asyncio.run(
        tasks.run_order_job(job.id, account_ids, product_link=PRODUCT, payment_mode=tasks.PAYMENT_COD, **kwargs)
    )


def _sent_for(calls: list[dict], email: str) -> dict:
    """The run_order call made for the account whose session is ``email``'s."""
    return next(call for call in calls if call["cookies"][0]["value"] == email)


def _order_of(db, account: Account) -> Order:
    db.expire_all()
    return db.query(Order).filter(Order.account_id == account.id).one()


# --- which GST: the account's own wins ----------------------------------------
def test_an_accounts_own_gst_wins_over_the_gst_the_run_chose(db, ordering):
    run_gst = _gst(db, "BLAZECOM", RUN_GSTIN, "Blazecom")
    own = _gst(db, "DIGIDRIVE", ACCOUNT_GSTIN, "Digidrive LLP")
    _address(db)
    account = _account(db, "own@test.com", gst_id=own.id)
    job = _job(db)

    _run(job, [account.id], gst_id=run_gst.id)

    sent = ordering[0]["gst"]
    assert (sent.gstin, sent.business_name) == (ACCOUNT_GSTIN, "Digidrive LLP")
    order = _order_of(db, account)
    assert (order.gstin, order.gst_name) == (ACCOUNT_GSTIN, "Digidrive LLP")


def test_it_wins_over_the_addresss_linked_gst_and_the_default_too(db, ordering):
    """However the run's GST was reached — chosen, linked, or the default."""
    linked = _gst(db, "BLAZECOM", RUN_GSTIN, "Blazecom")
    _gst(db, "GLOBE", OTHER_GSTIN, "Globe Commerce", default=True)
    own = _gst(db, "DIGIDRIVE", ACCOUNT_GSTIN, "Digidrive LLP")
    _address(db, gst_id=linked.id)
    account = _account(db, "own@test.com", gst_id=own.id)

    _run(_job(db), [account.id])

    assert ordering[0]["gst"].gstin == ACCOUNT_GSTIN


def test_an_account_without_a_gst_of_its_own_uses_the_runs_as_before(db, ordering):
    run_gst = _gst(db, "BLAZECOM", RUN_GSTIN, "Blazecom")
    _address(db)
    account = _account(db, "plain@test.com")
    job = _job(db)

    _run(job, [account.id], gst_id=run_gst.id)

    assert (ordering[0]["gst"].gstin, ordering[0]["gst"].business_name) == (RUN_GSTIN, "Blazecom")
    assert _order_of(db, account).gstin == RUN_GSTIN
    db.expire_all()
    assert "own GST" not in (db.get(Job, job.id).log or "")  # nothing to say


def test_an_account_with_its_own_gst_bills_under_it_when_the_run_has_none(db, ordering):
    own = _gst(db, "DIGIDRIVE", ACCOUNT_GSTIN, "Digidrive LLP")
    _address(db)  # no GST chosen, linked or default: the run itself has none
    with_gst = _account(db, "own@test.com", gst_id=own.id)
    without = _account(db, "plain@test.com")
    job = _job(db)

    _run(job, [with_gst.id, without.id])

    assert _sent_for(ordering, "own@test.com")["gst"].gstin == ACCOUNT_GSTIN
    assert _sent_for(ordering, "plain@test.com")["gst"].gstin == ""
    assert _order_of(db, without).gstin is None
    db.expire_all()
    assert f"1 order(s) used their account's own GST: {ACCOUNT_GSTIN} (1). The run's GST: none." in db.get(Job, job.id).log


def test_a_mixed_run_bills_each_order_to_its_own_entity_and_says_how_many(db, ordering):
    """The case the package exists for: one run over two entities' accounts."""
    run_gst = _gst(db, "BLAZECOM", RUN_GSTIN, "Blazecom")
    digidrive = _gst(db, "DIGIDRIVE", ACCOUNT_GSTIN, "Digidrive LLP")
    sonic = _gst(db, "SONIC", OTHER_GSTIN, "Sonic Kommerce")
    _address(db)
    a = _account(db, "a@test.com", gst_id=digidrive.id)
    b = _account(db, "b@test.com")
    c = _account(db, "c@test.com", gst_id=sonic.id)
    d = _account(db, "d@test.com", gst_id=digidrive.id)
    # Passed over (no session): never ordered, so never counted.
    skipped = _account(db, "e@test.com", gst_id=sonic.id, with_session=False)
    job = _job(db)

    _run(job, [a.id, b.id, c.id, d.id, skipped.id], gst_id=run_gst.id)

    assert {email: _sent_for(ordering, email)["gst"].gstin for email in ("a@test.com", "b@test.com", "c@test.com", "d@test.com")} == {
        "a@test.com": ACCOUNT_GSTIN,
        "b@test.com": RUN_GSTIN,
        "c@test.com": OTHER_GSTIN,
        "d@test.com": ACCOUNT_GSTIN,
    }
    assert {_order_of(db, row).gstin for row in (a, d)} == {ACCOUNT_GSTIN}
    assert _order_of(db, c).gstin == OTHER_GSTIN
    assert _order_of(db, b).gstin == RUN_GSTIN
    db.expire_all()
    log = db.get(Job, job.id).log
    assert (
        f"3 order(s) used their account's own GST: {ACCOUNT_GSTIN} (2), {OTHER_GSTIN} (1). "
        f"The run's GST: {RUN_GSTIN}."
    ) in log


def test_a_failed_attempt_still_records_the_gst_it_used(db, monkeypatch):
    """The row of an order that broke before Flipkart answered names the GST
    and phone it was attempted with, like any other."""
    _gst(db, "BLAZECOM", RUN_GSTIN, "Blazecom", default=True)
    own = _gst(db, "DIGIDRIVE", ACCOUNT_GSTIN, "Digidrive LLP")
    _address(db)
    account = _account(db, "own@test.com", billing_phone="9811111111", gst_id=own.id)

    async def broken(page, **_):
        raise RuntimeError("the page went away")

    monkeypatch.setattr(tasks, "browser_page", _fake_browser())
    monkeypatch.setattr(tasks, "run_order", broken)
    _run(_job(db), [account.id])

    order = _order_of(db, account)
    assert order.status == OrderStatus.failed
    assert (order.gstin, order.billing_phone) == (ACCOUNT_GSTIN, "9811111111")


# --- which phone: the billing phone, else the account's, else the address's ----
@pytest.mark.parametrize(
    ("billing_phone", "phone", "address_phone", "used"),
    [
        pytest.param("8000000002", "9000000001", "7000000003", "8000000002", id="billing-phone-first"),
        pytest.param(None, "9000000001", "7000000003", "9000000001", id="else-the-accounts-phone"),
        pytest.param(None, None, "7000000003", "7000000003", id="else-the-addresss-phone"),
        pytest.param(None, None, None, "", id="none-at-all"),
    ],
)
def test_the_phone_at_checkout_and_on_the_order(db, ordering, billing_phone, phone, address_phone, used):
    _address(db, phone=address_phone)
    account = _account(db, "phone@test.com", phone=phone, billing_phone=billing_phone)

    _run(_job(db), [account.id])

    assert ordering[0]["address"].phone == used
    assert _order_of(db, account).billing_phone == (used or None)


def test_a_phone_wider_than_the_ledgers_column_is_cut_to_fit_not_refused(db, ordering):
    """An account's own phone may be 32 characters; the order row keeps 20.
    The order is still placed with the whole number."""
    _address(db)
    wide = "9" * 32
    account = _account(db, "wide@test.com", phone=wide)

    _run(_job(db), [account.id])

    assert ordering[0]["address"].phone == wide
    order = _order_of(db, account)
    assert order.status == OrderStatus.placed
    assert order.billing_phone == "9" * 20


# --- the checkout's own GST check is held to the account's GST -----------------
def _checkout_showing(gstin: str):
    """A page whose checkout names ``gstin`` beside "GST Invoice", then
    Flipkart's confirmation page, for the real run_order to walk through."""

    def factory() -> FakePage:
        page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹999.00"})
        checkout = f"Deliver to: Blazecom Warehouse. GST Invoice {gstin} Business name"
        # The checkout is read twice before payment: by the GST step, which
        # leaves a page already billing the run's GSTIN alone (27 Oct 2026),
        # and by the check that holds the page to it.
        page.inner_text_map[S.PAGE_BODY] = [
            checkout,
            checkout,
            "Order placed successfully. Order ID OD428123456789100",
        ]
        return page

    return factory


@pytest.fixture
def real_flow(monkeypatch, tmp_path):
    """The real run_order on a fake page. The step that adds the delivery
    address to the account is left out: it is not what these tests are about."""
    real_run_order = tasks.run_order

    async def without_the_address_step(page, **kwargs):
        kwargs["ensure_address"] = False
        return await real_run_order(page, **kwargs)

    monkeypatch.setattr(tasks, "run_order", without_the_address_step)
    monkeypatch.setattr(order_flow, "DIAGNOSTICS_DIR", tmp_path)  # a refused order's screenshot
    return lambda gstin: monkeypatch.setattr(tasks, "browser_page", _fake_browser(_checkout_showing(gstin)))


def test_a_checkout_billing_the_accounts_own_gst_is_ordered(db, real_flow):
    run_gst = _gst(db, "BLAZECOM", RUN_GSTIN, "Blazecom")
    own = _gst(db, "DIGIDRIVE", ACCOUNT_GSTIN, "Digidrive LLP")
    _address(db)
    account = _account(db, "own@test.com", gst_id=own.id)
    real_flow(ACCOUNT_GSTIN)

    _run(_job(db), [account.id], gst_id=run_gst.id, target=1000.0)

    order = _order_of(db, account)
    assert order.status == OrderStatus.placed
    assert order.gstin == ACCOUNT_GSTIN


def test_a_checkout_billing_the_runs_gst_is_refused_for_an_account_with_its_own(db, real_flow):
    """gstin_on_checkout is compared with the GST the account resolved to, so
    a page still billing the run's GST stops before any payment step."""
    run_gst = _gst(db, "BLAZECOM", RUN_GSTIN, "Blazecom")
    own = _gst(db, "DIGIDRIVE", ACCOUNT_GSTIN, "Digidrive LLP")
    _address(db)
    account = _account(db, "own@test.com", gst_id=own.id)
    real_flow(RUN_GSTIN)
    job = _job(db)

    _run(job, [account.id], gst_id=run_gst.id, target=1000.0)

    order = _order_of(db, account)
    assert order.status == OrderStatus.failed
    db.expire_all()
    message = db.get(Job, job.id).items[0].message
    assert f"the checkout bills under GST {RUN_GSTIN}, the run chose {ACCOUNT_GSTIN}" in message
    assert "nothing was charged" in message


def test_without_a_gst_of_its_own_the_check_holds_the_page_to_the_runs_gst(db, real_flow):
    run_gst = _gst(db, "BLAZECOM", RUN_GSTIN, "Blazecom")
    _address(db)
    account = _account(db, "plain@test.com")
    real_flow(ACCOUNT_GSTIN)

    _run(_job(db), [account.id], gst_id=run_gst.id, target=1000.0)

    assert _order_of(db, account).status == OrderStatus.failed  # the page billed someone else


# --- the command line orders the same way ---------------------------------------
def test_the_cli_uses_the_accounts_own_gst_and_billing_phone(db, monkeypatch):
    _gst(db, "BLAZECOM", RUN_GSTIN, "Blazecom", default=True)
    own = _gst(db, "DIGIDRIVE", ACCOUNT_GSTIN, "Digidrive LLP")
    _address(db, phone="7000000003")
    account = _account(db, "cli@test.com", phone="9000000001", billing_phone="8000000002", gst_id=own.id)
    calls: list[dict] = []

    async def fake_run_order(page, **kwargs):
        calls.append(kwargs)
        return {"status": OrderStatus.placed, "order_id": "OD1", "amount": 500.0, "payment_mode": "cod", "qty": 1}

    @contextlib.asynccontextmanager
    async def fake_browser_page(**_):
        page = FakePage()
        yield page.context, page

    monkeypatch.setattr(order_flow, "run_order", fake_run_order)
    monkeypatch.setattr(cli, "browser_page", fake_browser_page)
    args = SimpleNamespace(
        product=PRODUCT, email="cli@test.com", address=None, address_id=None, gst_id=None, target=18000.0, qty=1,
        payment_mode=order_flow.PAYMENT_COD, card=None, month=None, year=None, cvv=None,
    )

    asyncio.run(cli.cmd_order(args))

    assert calls[0]["gst"].gstin == ACCOUNT_GSTIN
    assert calls[0]["address"].phone == "8000000002"
    order = _order_of(db, account)
    assert (order.gstin, order.billing_phone) == (ACCOUNT_GSTIN, "8000000002")
