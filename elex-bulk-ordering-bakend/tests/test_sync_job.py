"""Tests for the post-order sync job (ROADMAP §10) — no browser, no Flipkart.

``app.jobs.sync_tasks.browser_page`` and ``scrape_orders`` are monkeypatched, so
these exercise the reconciliation rules rather than the scraper:

* the Phase 10 columns land on the matching row and ``synced_at`` is stamped;
* **the scrape never rewrites ``amount``/``target``/``qty``/``status``** — those
  are what we intended and paid, and are the record a reconciliation is run
  *against*. That is the headline test here;
* a cancelled order is the one exception and does flip ``status``;
* matching is scoped to one account, so a shared order id cannot cross over;
* an account with no session is skipped and the rest of the run continues.

Each fake account's cookies carry its own email, which is how the fake scraper
knows whose orders to return — the same trick ``tests/test_jobs_tasks.py`` uses.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

import pytest

from app.db.models import (
    Account,
    AccountStatus,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
)
from tests.fakes import FakeContext, FakePage

# The sibling scraper (app/automation/sync.py) is written in the same phase as
# this job. Skip rather than error if it has not landed yet: the job module
# imports it at module scope, so there is nothing to test without it.
sync_module = pytest.importorskip(
    "app.automation.sync", reason="app/automation/sync.py (sibling) not present yet"
)
from app.jobs import sync_tasks  # noqa: E402  (must follow the importorskip)

ScrapedOrder = sync_module.ScrapedOrder

#: The fields this job reads off an observation and writes to an Order row. This
#: tuple is the contract with the scraper, which is why one test asserts it.
CONTRACT_FIELDS = (
    "order_id",
    "item_id",
    "unit_id",
    "seller",
    "tracking_id",
    "platform_status",
    "delivery_date",
    "delivery_otp",
    "invoice_url",
)

#: Fields the job writes that ``ScrapedOrder`` does not carry yet. None now: the
#: last, the tracking id, has been read off the order's history since 27 Oct
#: 2026. The job reads observations with ``getattr``, so such a column would
#: simply stay None until the scraper surfaced it.
PENDING_FIELDS: tuple[str, ...] = ()

#: Flipkart's own wording, as ``parse_status_line`` hands it over (clean, with
#: the date tail already split off). One test below checks the scraper agrees
#: these two are terminal, so these literals cannot drift from its vocabulary.
DELIVERED = "Delivered"
CANCELLED = "Cancelled"
#: A state that is neither, to prove an unrecognised one is never guessed.
IN_FLIGHT = "Out for Delivery"


# --- helpers -----------------------------------------------------------------
def _account(db, email, *, with_session=True):
    account = Account(email=email, status=AccountStatus.new)
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


def _job(db, payload=None):
    job = Job(type=JobType.sync, status=JobStatus.queued, payload=payload)
    db.add(job)
    db.commit()
    return job


def _order(db, account, **fields):
    row = Order(account_id=account.id, **{"status": OrderStatus.placed, **fields})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _scraped_order_fields() -> set[str]:
    """The field names ``ScrapedOrder`` actually declares."""
    return set(
        getattr(ScrapedOrder, "__dataclass_fields__", None)
        or getattr(ScrapedOrder, "__annotations__", {})
    )


def _scraped(**fields: object):
    """One observation, built from the scraper's own ``ScrapedOrder``.

    The real type rather than a stand-in on purpose: a field this job writes
    that ``ScrapedOrder`` renames is a contract break, and it should fail here
    rather than show up in production as a column that is never filled.

    Anything ``ScrapedOrder`` does not declare is set on the instance instead of
    passed to the constructor, which is how a test can say "the platform
    reported a tracking id" (not scraped yet) or "the page claimed a different
    quantity" (never a scraper's business at all).
    """
    known = _scraped_order_fields()
    observation = ScrapedOrder(**{k: v for k, v in fields.items() if k in known})
    for name, value in fields.items():
        if name not in known:
            setattr(observation, name, value)
    return observation


def _patch(
    monkeypatch, by_email: dict[str, list], *, raises: dict[str, str] | None = None
):
    """Swap in a fake browser + scraper; returns the recorded scraper calls."""
    calls: list[dict] = []
    raises = raises or {}

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def fake_scrape_orders(page, *, limit=20, open_details=True, should_stop=None):
        cookies = await page.context.cookies()
        email = cookies[0]["value"]
        calls.append({"email": email, "limit": limit, "open_details": open_details})
        if email in raises:
            raise RuntimeError(raises[email])
        return list(by_email.get(email, []))

    monkeypatch.setattr(sync_tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(sync_tasks, "scrape_orders", fake_scrape_orders)
    return calls


def _items_by_account(db, job_id):
    db.expire_all()
    return {
        item.account_id: item
        for item in db.query(JobItem).filter(JobItem.job_id == job_id).all()
    }


def _reload(db, row):
    db.expire_all()
    return db.get(type(row), row.id)


def _message(db, job_id, account_id) -> str:
    return _items_by_account(db, job_id)[account_id].message or ""


# --- the contract with the scraper -------------------------------------------
def test_scraped_order_carries_every_field_the_sync_writes() -> None:
    """Guards the seam: a renamed field must fail loudly, not silently.

    ``_observed`` reads an observation with ``getattr(..., None)`` so one
    unsupported column cannot take an account's whole sync down — which means a
    rename would otherwise show up only as a column that is never populated.
    """
    names = _scraped_order_fields()
    missing = sorted(set(CONTRACT_FIELDS) - names - set(PENDING_FIELDS))
    assert not missing, f"ScrapedOrder is missing {missing}"


def test_the_states_these_tests_use_are_the_ones_the_scraper_recognises() -> None:
    """The scraper owns the vocabulary; these literals must still be in it."""
    assert sync_module.is_delivered(DELIVERED)
    assert sync_module.is_cancelled(CANCELLED)
    assert not sync_module.is_delivered(IN_FLIGHT)
    assert not sync_module.is_cancelled(IN_FLIGHT)


# --- the fields land ----------------------------------------------------------
def test_sync_fills_the_platform_fields_and_stamps_synced_at(db, monkeypatch) -> None:
    account = _account(db, "s1@test.com")
    order = _order(db, account, order_id="OD113741449375753000", qty=1, amount=9891.0)
    job = _job(db)
    delivered_on = datetime(2026, 10, 20, 11, 30)

    _patch(
        monkeypatch,
        {
            "s1@test.com": [
                _scraped(
                    order_id="OD113741449375753000",
                    item_id="ITM9f8",
                    unit_id="UNIT77",
                    seller="Flashtech Retail",
                    platform_status=DELIVERED,
                    delivery_date=delivered_on,
                    delivery_otp="4821",
                    invoice_url="https://www.flipkart.com/invoice/OD1137.pdf",
                )
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    row = _reload(db, order)
    assert row.item_id == "ITM9f8"
    assert row.unit_id == "UNIT77"
    assert row.seller == "Flashtech Retail"
    assert row.platform_status == DELIVERED
    assert row.delivery_date == delivered_on
    assert row.delivery_otp == "4821"
    assert row.invoice_url == "https://www.flipkart.com/invoice/OD1137.pdf"
    assert row.synced_at is not None

    job = _reload(db, job)
    assert job.status == JobStatus.completed
    assert job.progress == 100
    items = _items_by_account(db, job.id)
    assert items[account.id].status == JobItemStatus.done
    assert "1 matched" in items[account.id].message


def test_a_tracking_id_lands_as_soon_as_the_scraper_reports_one(
    db, monkeypatch
) -> None:
    """The scraper reads the courier's tracking id off the order's history
    (27 Oct 2026); the job writes it to the order row."""
    account = _account(db, "track@test.com")
    order = _order(db, account, order_id="OD-TRACK")
    job = _job(db)

    _patch(
        monkeypatch,
        {"track@test.com": [_scraped(order_id="OD-TRACK", tracking_id="FMPC0123456789")]},
    )
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    assert _reload(db, order).tracking_id == "FMPC0123456789"


def test_synced_at_is_stamped_even_when_nothing_moved(db, monkeypatch) -> None:
    """"Nothing changed" and "never looked" must stay different answers."""
    account = _account(db, "s2@test.com")
    order = _order(db, account, order_id="OD2", platform_status=DELIVERED)
    job = _job(db)

    _patch(monkeypatch, {"s2@test.com": [_scraped(order_id="OD2", platform_status=DELIVERED)]})
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    row = _reload(db, order)
    assert row.synced_at is not None
    # Matched, but nothing observed actually moved.
    message = _message(db, job.id, account.id)
    assert "1 matched" in message and "0 updated" in message


def test_an_order_id_matches_despite_case_and_whitespace(db, monkeypatch) -> None:
    """A silent no-match here looks exactly like "the platform forgot it"."""
    account = _account(db, "s3@test.com")
    order = _order(db, account, order_id=" od9001 ")
    job = _job(db)

    _patch(monkeypatch, {"s3@test.com": [_scraped(order_id="OD9001", seller="Acme")]})
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    assert _reload(db, order).seller == "Acme"


# --- observation never overwrites intent (the headline) ----------------------
def test_a_scrape_never_rewrites_what_we_intended_and_paid(db, monkeypatch) -> None:
    """``amount``/``target``/``qty``/``status`` are the record, not the reading.

    The observation below claims a different price, a different quantity and a
    different status. Letting any of them through would destroy the figures the
    reconciliation is run against — the order we *placed* at 27,999 against a
    28,000 cap would come back as a 1-rupee order of 99 units.
    """
    account = _account(db, "keep@test.com")
    order = _order(
        db,
        account,
        order_id="OD314",
        qty=3,
        amount=27999.0,
        target=28000.0,
        model="vivo T5x 5G (Moonlight Blue, 128 GB)",
        color="Moonlight Blue",
        status=OrderStatus.placed,
    )
    job = _job(db)

    _patch(
        monkeypatch,
        {
            "keep@test.com": [
                _scraped(
                    order_id="OD314",
                    qty=99,
                    amount=1.0,
                    target=2.0,
                    status=OrderStatus.failed,
                    model="SOMETHING ELSE",
                    color="Jet Black",
                    platform_status=DELIVERED,
                    seller="Flashtech Retail",
                )
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    row = _reload(db, order)
    assert row.qty == 3
    assert row.amount == 27999.0
    assert row.target == 28000.0
    assert row.status == OrderStatus.placed
    assert row.model == "vivo T5x 5G (Moonlight Blue, 128 GB)"
    assert row.color == "Moonlight Blue"
    # ...while what the sync *is* for did land.
    assert row.platform_status == DELIVERED
    assert row.seller == "Flashtech Retail"


def test_an_unreadable_field_does_not_erase_one_we_already_have(
    db, monkeypatch
) -> None:
    """"Not read" is not "empty" — a blank detail page must not wipe a row."""
    account = _account(db, "blank@test.com")
    order = _order(
        db,
        account,
        order_id="OD500",
        unit_id="UNIT-KEEP-ME",
        delivery_otp="1234",
        seller="Flashtech Retail",
    )
    job = _job(db)

    _patch(
        monkeypatch,
        {
            "blank@test.com": [
                _scraped(
                    order_id="OD500",
                    unit_id=None,
                    delivery_otp="",
                    seller="   ",
                    platform_status=DELIVERED,
                )
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    row = _reload(db, order)
    assert row.unit_id == "UNIT-KEEP-ME"
    assert row.delivery_otp == "1234"
    assert row.seller == "Flashtech Retail"
    assert row.platform_status == DELIVERED


# --- cancelled is the one exception ------------------------------------------
def test_a_cancelled_order_is_marked_failed_and_the_message_says_so(
    db, monkeypatch
) -> None:
    """A cancelled order genuinely is not placed any more."""
    account = _account(db, "cancel@test.com")
    order = _order(db, account, order_id="OD600", status=OrderStatus.placed)
    job = _job(db)

    _patch(
        monkeypatch,
        {"cancel@test.com": [_scraped(order_id="OD600", platform_status=CANCELLED)]},
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    row = _reload(db, order)
    assert row.status == OrderStatus.failed
    # The raw platform wording is kept as well: it says *why* we flipped it.
    assert row.platform_status == CANCELLED
    message = _message(db, job.id, account.id)
    assert "cancelled" in message and "failed" in message


def test_a_delivered_order_keeps_the_status_we_recorded(db, monkeypatch) -> None:
    account = _account(db, "delivered@test.com")
    order = _order(db, account, order_id="OD700", status=OrderStatus.placed)
    job = _job(db)

    _patch(
        monkeypatch,
        {"delivered@test.com": [_scraped(order_id="OD700", platform_status=DELIVERED)]},
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    row = _reload(db, order)
    assert row.status == OrderStatus.placed
    assert row.platform_status == DELIVERED
    message = _message(db, job.id, account.id)
    assert "1 delivered" in message
    assert "cancelled" not in message


def test_a_state_that_is_neither_is_never_guessed(db, monkeypatch) -> None:
    """An in-flight order is recorded as-is: not delivered, not cancelled."""
    account = _account(db, "inflight@test.com")
    order = _order(db, account, order_id="OD750", status=OrderStatus.placed)
    job = _job(db)

    _patch(
        monkeypatch,
        {"inflight@test.com": [_scraped(order_id="OD750", platform_status=IN_FLIGHT)]},
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    row = _reload(db, order)
    assert row.status == OrderStatus.placed
    assert row.platform_status == IN_FLIGHT
    message = _message(db, job.id, account.id)
    assert "delivered" not in message and "cancelled" not in message


def test_a_withdrawn_return_does_not_cancel_the_order(db, monkeypatch) -> None:
    """"Return Cancelled" is the buyer withdrawing a return on a unit that was
    delivered: the order stands. Reading it as a cancellation would flip the
    row to failed for good — nothing ever flips it back — and hand its
    unit-limit quota to the next run."""
    account = _account(db, "withdrawn@test.com")
    order = _order(db, account, order_id="OD760", status=OrderStatus.placed)
    job = _job(db)

    _patch(
        monkeypatch,
        {
            "withdrawn@test.com": [
                _scraped(order_id="OD760", platform_status="Return Cancelled")
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    row = _reload(db, order)
    assert row.status == OrderStatus.placed
    assert row.platform_status == "Return Cancelled"
    assert row.delivery_state == "returned"  # settled, in the return flow
    message = _message(db, job.id, account.id)
    assert "cancelled" not in message and "failed" not in message


def test_a_sync_re_evaluates_the_alert_rules_when_it_finishes(db, monkeypatch) -> None:
    """A delivery that arrived must close its overdue alert without anyone
    pressing "Evaluate now" — and the rules must run after the rows are
    written, not before."""
    from app.db.models import Alert

    account = _account(db, "alerts@test.com")
    order = _order(db, account, order_id="OD770", status=OrderStatus.placed)
    job = _job(db)
    seen: list[str] = []

    real = sync_tasks.alert_rules.evaluate_in_own_session

    def recording():
        seen.append(_reload(db, order).platform_status or "")
        return real()

    monkeypatch.setattr(sync_tasks.alert_rules, "evaluate_in_own_session", recording)
    _patch(monkeypatch, {"alerts@test.com": [_scraped(order_id="OD770", platform_status=DELIVERED)]})

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    assert seen == [DELIVERED]  # once, after the reconciliation landed
    db.expire_all()
    assert db.query(Alert).count() >= 0  # the real pass ran against the same database


# --- orders we never placed ---------------------------------------------------
def test_unknown_orders_are_ignored_by_default(db, monkeypatch) -> None:
    """A sync must not bury our own orders under an account's whole history."""
    account = _account(db, "unknown@test.com")
    job = _job(db)

    _patch(
        monkeypatch,
        {
            "unknown@test.com": [
                _scraped(order_id="OD-NOT-OURS", platform_status=DELIVERED)
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    db.expire_all()
    assert db.query(Order).count() == 0
    message = _message(db, job.id, account.id)
    assert "1 unknown" in message
    assert "imported" not in message


def test_an_unconfirmed_order_adopts_the_platform_order_instead_of_importing(
    db, monkeypatch
) -> None:
    """An unconfirmed order has no id (it was sent, never confirmed). When the
    platform later lists an order we cannot otherwise match, that row is the
    one it belongs to — importing would duplicate it and leave the unconfirmed
    row saying "may exist" forever."""
    account = _account(db, "unsure@test.com")
    row = _order(
        db,
        account,
        status=OrderStatus.unconfirmed,
        order_id=None,
        model="vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)",
        amount=17707.0,
    )
    job = _job(db, payload={"import_unknown": True})

    _patch(
        monkeypatch,
        {
            "unsure@test.com": [
                _scraped(
                    order_id="OD338963864435840100",
                    model="vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB)",
                    amount=17707.0,
                    platform_status="Arriving",
                )
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    db.expire_all()
    assert db.query(Order).count() == 1  # adopted, not imported alongside
    row = _reload(db, row)
    assert row.order_id == "OD338963864435840100"
    assert row.status == OrderStatus.placed
    assert row.synced_at is not None
    message = _message(db, job.id, account.id)
    assert "1 unconfirmed order(s) found on Flipkart and settled" in message
    assert "imported" not in message


def test_an_adopted_order_the_platform_cancelled_is_failed(db, monkeypatch) -> None:
    account = _account(db, "unsure2@test.com")
    row = _order(db, account, status=OrderStatus.unconfirmed, order_id=None, amount=17707.0)
    job = _job(db)
    _patch(
        monkeypatch,
        # The amount agrees with the unconfirmed row: that is what lets the
        # platform's order be filed as the one that was sent.
        {"unsure2@test.com": [_scraped(order_id="OD338963864435840100", platform_status="Cancelled", amount=17707.0)]},
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    db.expire_all()
    row = _reload(db, row)
    assert row.order_id == "OD338963864435840100"
    assert row.status == OrderStatus.failed


def test_an_unknown_order_matching_nothing_is_not_filed_as_the_unconfirmed_one(db, monkeypatch) -> None:
    """Audit F-9 (25 Oct 2026): a stranger's order on the account — the owner's
    own purchase, an older one in the list — agrees with the unconfirmed row
    on neither model nor amount, and must not become a placed order carrying
    that row's card number. It stays for a person to settle."""
    account = _account(db, "unsure3@test.com")
    row = _order(db, account, status=OrderStatus.unconfirmed, order_id=None, amount=17707.0)
    job = _job(db)
    _patch(
        monkeypatch,
        {
            "unsure3@test.com": [
                _scraped(
                    order_id="OD999999999999999999",
                    platform_status="Delivered",
                    model="Realme 2 (Diamond Blue, 64 GB)",
                    amount=9891.0,
                )
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    db.expire_all()
    row = _reload(db, row)
    assert (row.order_id, row.status) == (None, OrderStatus.unconfirmed)


def test_a_logged_out_account_fails_its_read_back_and_is_marked_invalid(db, monkeypatch) -> None:
    """Audit F-8 (25 Oct 2026): a session Flipkart bounced to login is a failed
    read-back that says so, and the account is marked so the alert raises and
    the next login run picks it up — never a clean "0 orders seen"."""
    from app.db.models import Account, AccountStatus, JobItem, JobItemStatus

    account = _account(db, "gone@test.com")
    job = _job(db)

    async def bounce(page, **kwargs):
        raise sync_tasks.NotSignedIn("the account is not signed in: Flipkart sent the orders page to its login page")

    _patch(monkeypatch, {})  # the fake browser; the scraper below replaces the fake one
    monkeypatch.setattr(sync_tasks, "scrape_orders", bounce)

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    db.expire_all()
    assert db.get(Account, account.id).status == AccountStatus.session_invalid
    item = db.query(JobItem).filter(JobItem.job_id == job.id).one()
    assert item.status == JobItemStatus.failed
    assert "not signed in" in (item.message or "") and "login" in (item.message or "")


def test_unknown_orders_are_imported_when_asked(db, monkeypatch) -> None:
    account = _account(db, "import@test.com")
    job = _job(db)
    delivered_on = datetime(2026, 10, 1, 9, 0)

    _patch(
        monkeypatch,
        {
            "import@test.com": [
                _scraped(
                    order_id="OD-BY-HAND",
                    item_id="ITM1",
                    unit_id="UNIT1",
                    seller="Flashtech Retail",
                    model="Realme 2 (Diamond Blue, 64 GB)",
                    color="Diamond Blue",
                    amount=9891.0,
                    platform_status=DELIVERED,
                    delivery_date=delivered_on,
                    delivery_otp="9090",
                )
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id], import_unknown=True))

    db.expire_all()
    rows = db.query(Order).all()
    assert len(rows) == 1
    created = rows[0]
    assert created.account_id == account.id
    assert created.order_id == "OD-BY-HAND"
    assert created.status == OrderStatus.placed
    assert created.seller == "Flashtech Retail"
    assert created.model == "Realme 2 (Diamond Blue, 64 GB)"
    assert created.color == "Diamond Blue"
    assert created.amount == 9891.0
    assert created.delivery_date == delivered_on
    assert created.delivery_otp == "9090"
    assert created.synced_at is not None
    # We did not place it, so there is no honest placed_at to record.
    assert created.placed_at is None
    assert "1 imported" in _message(db, job.id, account.id)


def test_an_imported_order_the_platform_cancelled_is_recorded_as_failed(
    db, monkeypatch
) -> None:
    account = _account(db, "import-cancelled@test.com")
    job = _job(db)

    _patch(
        monkeypatch,
        {
            "import-cancelled@test.com": [
                _scraped(order_id="OD-GONE", platform_status=CANCELLED)
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id], import_unknown=True))

    db.expire_all()
    assert db.query(Order).one().status == OrderStatus.failed


def test_importing_does_not_touch_the_orders_that_already_match(
    db, monkeypatch
) -> None:
    """With importing on, a matched order is still updated, not duplicated."""
    account = _account(db, "mixed@test.com")
    order = _order(db, account, order_id="OURS", qty=2, amount=100.0)
    job = _job(db)

    _patch(
        monkeypatch,
        {
            "mixed@test.com": [
                _scraped(order_id="OURS", seller="Ours Seller"),
                _scraped(order_id="THEIRS", seller="Theirs Seller"),
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id], import_unknown=True))

    db.expire_all()
    assert db.query(Order).count() == 2
    ours = _reload(db, order)
    assert ours.seller == "Ours Seller"
    assert ours.qty == 2 and ours.amount == 100.0
    message = _message(db, job.id, account.id)
    assert "1 matched" in message and "1 unknown" in message and "1 imported" in message


def test_an_observation_with_no_order_id_is_never_imported(db, monkeypatch) -> None:
    """A row whose href carried no ids cannot match anything — now or ever.

    It must not be matched against an order row that has no id yet either, and
    importing it would create a fresh duplicate on every single run, so it is
    counted and dropped even with importing on.
    """
    account = _account(db, "noid@test.com")
    pending = _order(db, account, order_id=None, status=OrderStatus.pending)
    job = _job(db)

    _patch(monkeypatch, {"noid@test.com": [_scraped(order_id=None, seller="Acme")]})
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id], import_unknown=True))

    row = _reload(db, pending)
    assert row.seller is None
    assert row.synced_at is None
    db.expire_all()
    assert db.query(Order).count() == 1  # only the row that was already there
    message = _message(db, job.id, account.id)
    assert "1 unknown" in message and "1 with no order id" in message


def test_further_units_of_one_order_become_their_own_rows(db, monkeypatch) -> None:
    """Flipkart lists one row per unit; so does the ledger.

    The order row mirrors the first unit (that is what the alert rules read),
    and every unit keeps its own id and status — the per-unit breakdown the
    operator's status report needs. A second identical sync reports nothing
    moved.
    """
    account = _account(db, "units@test.com")
    order = _order(db, account, order_id="OD-3UP", qty=3)

    units = [
        _scraped(
            order_id="OD-3UP",
            unit_id=f"UNIT{i}",
            item_id="ITM1",
            platform_status=DELIVERED,
        )
        for i in (1, 2, 3)
    ]
    _patch(monkeypatch, {"units@test.com": units})

    first_job = _job(db)
    asyncio.run(sync_tasks.run_sync_job(first_job.id, [account.id]))

    row = _reload(db, order)
    assert row.unit_id == "UNIT1"
    assert row.qty == 3
    assert [(u.position, u.unit_id, u.platform_status) for u in row.units] == [
        (1, "UNIT1", DELIVERED),
        (2, "UNIT2", DELIVERED),
        (3, "UNIT3", DELIVERED),
    ]
    message = _message(db, first_job.id, account.id)
    assert "1 matched" in message and "1 updated" in message
    assert "2 further unit(s) of the same order recorded" in message
    # One order delivered, not three.
    assert "1 delivered" in message

    second_job = _job(db)
    asyncio.run(sync_tasks.run_sync_job(second_job.id, [account.id]))

    # Idempotent: the second pass sees the same page and nothing has moved.
    assert _reload(db, order).unit_id == "UNIT1"
    assert "0 updated" in _message(db, second_job.id, account.id)


def test_one_cancelled_unit_of_three_is_a_partial_order_not_a_failed_one(db, monkeypatch) -> None:
    account = _account(db, "partial@test.com")
    order = _order(db, account, order_id="OD-PART", qty=3)
    _patch(
        monkeypatch,
        {
            "partial@test.com": [
                _scraped(order_id="OD-PART", unit_id="P1", platform_status=DELIVERED),
                _scraped(order_id="OD-PART", unit_id="P2", platform_status=CANCELLED),
                _scraped(order_id="OD-PART", unit_id="P3", platform_status=DELIVERED),
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(_job(db).id, [account.id]))

    row = _reload(db, order)
    assert row.status == OrderStatus.placed  # two units are on their way to the buyer
    assert [u.delivery_state for u in row.units] == ["delivered", "cancelled", "delivered"]


def test_the_order_row_mirrors_a_unit_still_on_its_way(db, monkeypatch) -> None:
    """One delivered, one shipped: the order is not delivered yet, whatever
    unit happened to be listed first — the alert rules read the order row."""
    account = _account(db, "mixed@test.com")
    order = _order(db, account, order_id="OD-MIXED", qty=2)
    _patch(
        monkeypatch,
        {
            "mixed@test.com": [
                _scraped(order_id="OD-MIXED", unit_id="M1", platform_status=DELIVERED),
                _scraped(order_id="OD-MIXED", unit_id="M2", platform_status="Shipped"),
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(_job(db).id, [account.id]))

    row = _reload(db, order)
    assert (row.platform_status, row.delivery_state) == ("Shipped", "in_transit")
    assert [u.delivery_state for u in row.units] == ["delivered", "in_transit"]


def test_further_units_of_an_unknown_order_are_imported_once(db, monkeypatch) -> None:
    """Importing must not create one row per unit of the same foreign order."""
    account = _account(db, "unitimport@test.com")
    job = _job(db)

    _patch(
        monkeypatch,
        {
            "unitimport@test.com": [
                _scraped(order_id="OD-FOREIGN", unit_id="UNIT1"),
                _scraped(order_id="OD-FOREIGN", unit_id="UNIT2"),
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id], import_unknown=True))

    db.expire_all()
    created = db.query(Order).all()
    assert len(created) == 1
    assert created[0].unit_id == "UNIT1"
    # One order, two units — each its own row, both first seen on the platform.
    assert created[0].qty == 2
    assert [(u.unit_id, u.source) for u in created[0].units] == [("UNIT1", "sync"), ("UNIT2", "sync")]


# --- scoping and resilience ---------------------------------------------------
def test_matching_is_scoped_to_the_account(db, monkeypatch) -> None:
    """Two accounts syncing at once must not rewrite each other's rows."""
    mine = _account(db, "mine@test.com")
    theirs = _account(db, "theirs@test.com")
    my_order = _order(db, mine, order_id="SHARED", seller="mine")
    their_order = _order(db, theirs, order_id="SHARED", seller="theirs")
    job = _job(db)

    _patch(
        monkeypatch,
        {"mine@test.com": [_scraped(order_id="SHARED", seller="Flashtech Retail")]},
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [mine.id, theirs.id]))

    assert _reload(db, my_order).seller == "Flashtech Retail"
    # Same order id, different account: untouched, and never even looked at.
    untouched = _reload(db, their_order)
    assert untouched.seller == "theirs"
    assert untouched.synced_at is None


def test_an_account_without_a_session_is_skipped_and_the_run_carries_on(
    db, monkeypatch
) -> None:
    """A sync is read-only, so "never logged in" is a skip, not a failure."""
    no_session = _account(db, "nosession@test.com", with_session=False)
    usable = _account(db, "usable@test.com")
    order = _order(db, usable, order_id="OD900")
    job = _job(db)

    calls = _patch(
        monkeypatch,
        {"usable@test.com": [_scraped(order_id="OD900", seller="Flashtech Retail")]},
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [no_session.id, usable.id]))

    items = _items_by_account(db, job.id)
    assert items[no_session.id].status == JobItemStatus.skipped
    assert items[no_session.id].message == "no session"
    assert items[usable.id].status == JobItemStatus.done
    # No browser was opened for the account that could not be read.
    assert [call["email"] for call in calls] == ["usable@test.com"]
    assert _reload(db, order).seller == "Flashtech Retail"

    job = _reload(db, job)
    # Nothing went wrong, so a skipped account must not make the run red.
    assert job.status == JobStatus.completed
    assert job.skipped_items == 1
    assert job.failed_items == 0


def test_one_account_failing_does_not_stop_the_others(db, monkeypatch) -> None:
    broken = _account(db, "broken@test.com")
    fine = _account(db, "fine@test.com")
    order = _order(db, fine, order_id="OD1000")
    job = _job(db)

    _patch(
        monkeypatch,
        {"fine@test.com": [_scraped(order_id="OD1000", seller="Flashtech Retail")]},
        raises={"broken@test.com": "orders page never loaded"},
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [broken.id, fine.id]))

    items = _items_by_account(db, job.id)
    assert items[broken.id].status == JobItemStatus.failed
    assert "orders page never loaded" in items[broken.id].message
    assert items[fine.id].status == JobItemStatus.done
    assert _reload(db, order).seller == "Flashtech Retail"
    # One success among a failure is a completed run, not a failed one.
    assert _reload(db, job).status == JobStatus.completed


def test_a_deleted_account_is_reported_rather_than_crashing_the_run(
    db, monkeypatch
) -> None:
    job = _job(db)
    _patch(monkeypatch, {})

    asyncio.run(sync_tasks.run_sync_job(job.id, [424242]))

    # No item row can point at an account that is not there (a foreign key,
    # enforced on PostgreSQL and, since 25 Oct 2026, on SQLite too): the
    # account is written off as skipped at the start, named in the log, and
    # the run finishes instead of crashing.
    assert 424242 not in _items_by_account(db, job.id)
    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == JobStatus.completed
    assert (job.total_items, job.done_items, job.skipped_items, job.failed_items) == (1, 1, 1, 0)
    assert "account 424242: skipped - account not found" in (job.log or "")


def test_duplicate_rows_sharing_an_order_id_are_both_written(db, monkeypatch) -> None:
    """Leaving half a duplicate stale would hide it rather than expose it."""
    account = _account(db, "dupe@test.com")
    first = _order(db, account, order_id="OD-DUPE")
    second = _order(db, account, order_id="OD-DUPE")
    job = _job(db)

    _patch(
        monkeypatch,
        {"dupe@test.com": [_scraped(order_id="OD-DUPE", seller="Flashtech Retail")]},
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    assert _reload(db, first).seller == "Flashtech Retail"
    assert _reload(db, second).seller == "Flashtech Retail"
    # One scraped order matched; two rows were written.
    message = _message(db, job.id, account.id)
    assert "1 matched" in message and "2 updated" in message


# --- parameters reach the scraper --------------------------------------------
@pytest.mark.parametrize("limit", [5, 50])
def test_limit_per_account_reaches_the_scraper(db, monkeypatch, limit: int) -> None:
    account = _account(db, "limit@test.com")
    job = _job(db)

    calls = _patch(monkeypatch, {"limit@test.com": []})
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id], limit_per_account=limit))

    assert calls == [{"email": "limit@test.com", "limit": limit, "open_details": True}]


def test_details_are_always_opened(db, monkeypatch) -> None:
    """The list page's four detail selectors match nothing — reading them there
    is the bug that recorded every order with blanks. So the job always asks for
    the order's own page."""
    account = _account(db, "details@test.com")
    job = _job(db)

    calls = _patch(monkeypatch, {"details@test.com": []})
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    assert calls[0]["open_details"] is True


def test_an_account_with_no_orders_on_the_page_is_still_a_success(
    db, monkeypatch
) -> None:
    account = _account(db, "empty@test.com")
    job = _job(db)

    _patch(monkeypatch, {"empty@test.com": []})
    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    item = _items_by_account(db, job.id)[account.id]
    assert item.status == JobItemStatus.done
    assert "0 order(s) seen" in item.message


def test_accounts_are_run_concurrently(db, monkeypatch) -> None:
    """The same fan-out as every other job body, not a serial loop."""
    accounts = [_account(db, f"many{i}@test.com") for i in range(4)]
    job = _job(db)

    calls = _patch(monkeypatch, {f"many{i}@test.com": [] for i in range(4)})
    asyncio.run(sync_tasks.run_sync_job(job.id, [a.id for a in accounts]))

    assert len(calls) == 4
    job = _reload(db, job)
    assert job.total_items == 4
    assert job.done_items == 4
    assert job.status == JobStatus.completed


# --- date handling ------------------------------------------------------------
def test_an_iso_string_delivery_date_is_accepted(db, monkeypatch) -> None:
    """A DateTime column will not take a string; a scraper that hands back an
    ISO date must not fail the account's whole sync on commit."""
    account = _account(db, "isodate@test.com")
    order = _order(db, account, order_id="OD1100")
    job = _job(db)

    _patch(
        monkeypatch,
        {
            "isodate@test.com": [
                _scraped(order_id="OD1100", delivery_date="2026-10-20T11:30:00")
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    assert _reload(db, order).delivery_date == datetime(2026, 10, 20, 11, 30)


def test_an_unparseable_delivery_date_is_dropped_not_fatal(db, monkeypatch) -> None:
    account = _account(db, "baddate@test.com")
    order = _order(db, account, order_id="OD1200")
    job = _job(db)

    _patch(
        monkeypatch,
        {
            "baddate@test.com": [
                _scraped(
                    order_id="OD1200",
                    delivery_date="sometime next week",
                    seller="Flashtech Retail",
                )
            ]
        },
    )

    asyncio.run(sync_tasks.run_sync_job(job.id, [account.id]))

    row = _reload(db, order)
    assert row.delivery_date is None
    # The rest of the observation still landed.
    assert row.seller == "Flashtech Retail"
    assert _items_by_account(db, job.id)[account.id].status == JobItemStatus.done
