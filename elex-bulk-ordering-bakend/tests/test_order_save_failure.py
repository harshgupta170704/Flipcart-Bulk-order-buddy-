"""An order that ends but cannot be written down (review of 27 Oct 2026).

When saving an order's outcome failed after the browser had closed (SQLite's
"database is locked", a dropped PostgreSQL connection), the worker raised and
the run reported the account as a plain failure with the database's error: a
placed order's number was lost, and a failure invites the retry that buys
twice. The outcome is now told all the same: an order that was sent reads
unconfirmed, with what Flipkart said and a warning not to order again; one
that was not sent says why, beside the failed save.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy.exc import OperationalError

from app.automation.ordering import CardInfo
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Job,
    JobItem,
    JobItemStatus,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
)
from app.jobs import tasks
from tests.fakes import FakeContext, FakePage

PRODUCT = "https://www.flipkart.com/pigeon-special-3-l-outer-lid-pressure-cooker/p/itm0000000000001"
LOCKED = OperationalError("UPDATE orders SET status=?", {}, Exception("database is locked"))


@pytest.fixture
def account(db) -> int:
    row = Account(email="save@test.com", phone="9000000003", status=AccountStatus.session_valid)
    db.add(row)
    db.commit()
    db.add(SessionModel(account_id=row.id, cookies=[{"name": "SN", "value": "1"}], valid=True))
    db.add(Address(label="hq", name="HQ", line1="1 Main Road", pincode="560001", is_default=True))
    db.commit()
    return row.id


def _run(db, monkeypatch, account_id: int, outcome: dict, *, sent: bool = True, error: Exception = LOCKED):
    """One card order whose flow ends with ``outcome``, and whose save fails
    with ``error``. ``sent`` reserves the row first, as the real flow does
    before it sends an order."""
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()

    @contextlib.asynccontextmanager
    async def fake_browser_page(**kwargs):
        page = FakePage()
        page.context = FakeContext(kwargs.get("cookies"))
        yield page.context, page

    async def fake_run_order(page, *, product_link, address, gst, reserve=None, **kwargs):
        row = {"amount": 638.0, "payment_mode": tasks.PAYMENT_CARD, "qty": 1}
        result = {**row, **outcome}
        if sent:
            result["reservation"] = reserve(row)
        return result

    def failing_save(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(tasks, "browser_page", fake_browser_page)
    monkeypatch.setattr(tasks, "run_order", fake_run_order)
    monkeypatch.setattr(tasks, "record_order_result", failing_save)

    cards = {account_id: CardInfo(number="4111111111111111", month="12", year="28", cvv="321")}
    asyncio.run(tasks.run_order_job(job.id, [account_id], product_link=PRODUCT, target=700.0, cards=cards))
    db.expire_all()
    return db.get(Job, job.id), db.query(JobItem).filter_by(job_id=job.id).one()


def test_a_placed_order_that_cannot_be_saved_reads_unconfirmed_with_its_number(db, monkeypatch, account):
    job, item = _run(db, monkeypatch, account, {"status": OrderStatus.placed, "order_id": "OD338996689296590100"})

    assert item.status == JobItemStatus.unconfirmed
    assert item.message.startswith(
        "unconfirmed: placed on Flipkart as order OD338996689296590100 for ₹638.00, but saving it here failed ("
    )
    assert "database is locked" in item.message
    assert "do NOT order again for this account; the next read-back records it" in item.message
    # The row written before the order was sent still says it may exist.
    order = db.query(Order).one()
    assert (order.status, order.order_id) == (OrderStatus.unconfirmed, None)
    assert (job.unconfirmed_items, job.failed_items) == (1, 0)
    # Counted with the unconfirmed in the run's own record too.
    assert "1 order(s) UNCONFIRMED" in job.log


def test_an_unconfirmed_order_that_cannot_be_saved_still_reads_unconfirmed(db, monkeypatch, account):
    job, item = _run(
        db, monkeypatch, account, {"status": OrderStatus.unconfirmed, "why_unconfirmed": "no confirmation page"}
    )

    assert item.status == JobItemStatus.unconfirmed
    assert item.message.startswith(
        "unconfirmed: submitted for ₹638.00 but Flipkart never confirmed it, so it MAY exist — check this "
        "account's orders before retrying (no confirmation page) (saving it here failed: "
    )
    assert (job.unconfirmed_items, job.failed_items) == (1, 0)
    assert "1 order(s) UNCONFIRMED" in job.log


def test_an_order_not_sent_still_says_why_beside_the_failed_save(db, monkeypatch, account):
    job, item = _run(
        db, monkeypatch, account, {"status": OrderStatus.failed, "error": "Place Order did not respond"}, sent=False
    )

    assert item.status == JobItemStatus.failed
    assert item.message.startswith("not placed (failed): Place Order did not respond (saving it here failed: ")
    assert "database is locked" in item.message
    assert (job.unconfirmed_items, job.failed_items) == (0, 1)


def test_a_card_number_in_the_save_error_is_kept_out_of_the_message(db, monkeypatch, account):
    error = OperationalError("UPDATE orders SET card_number=?", {}, Exception("value 4111111111111111 refused"))

    _job, item = _run(
        db, monkeypatch, account, {"status": OrderStatus.placed, "order_id": "OD338996689296590100"}, error=error
    )

    assert "4111111111111111" not in item.message
    assert "[redacted]" in item.message
