"""A read-back a few minutes after an order (26 Oct 2026), not at the next
hourly pass: a new order's status, and an unconfirmed one's fate, show within
minutes of the run ending."""

from datetime import timedelta

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
    naive_utcnow,
)
from app.jobs import schedule


def _account(db):
    account = Account(email="ordered@test.com", status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    return account


def _order(db, account, *, minutes_ago, status=OrderStatus.placed, by_a_run=True):
    run = None
    if by_a_run:
        run = Job(type=JobType.order, status=JobStatus.completed)
        db.add(run)
        db.commit()
    order = Order(account_id=account.id, product_link="https://www.flipkart.com/p", qty=1, status=status, job_id=run.id if run else None)
    db.add(order)
    db.commit()
    order.created_at = naive_utcnow() - timedelta(minutes=minutes_ago)
    db.commit()
    return order


def _read_back(db, account, *, minutes_ago):
    job = Job(type=JobType.sync, status=JobStatus.completed, payload={"tracking": True})
    db.add(job)
    db.commit()
    db.add(JobItem(job_id=job.id, account_id=account.id, status=JobItemStatus.done, finished_at=naive_utcnow() - timedelta(minutes=minutes_ago)))
    db.commit()


def test_ordered_on_since_the_read_back_is_due_after_a_few_minutes(db):
    account = _account(db)
    _order(db, account, minutes_ago=40)
    _read_back(db, account, minutes_ago=20)  # within the hour: not due by the clock
    assert schedule.tracking_due(db)[0] == []

    _order(db, account, minutes_ago=5)  # ordered on since

    accounts, why = schedule.tracking_due(db)
    assert accounts == [account.id]
    assert "1 ordered on since" in why


def test_not_before_the_order_has_had_its_few_minutes(db):
    account = _account(db)
    _read_back(db, account, minutes_ago=20)
    _order(db, account, minutes_ago=1)

    assert schedule.tracking_due(db)[0] == []
    later = naive_utcnow() + timedelta(minutes=schedule.AFTER_ORDER_MINUTES)
    assert schedule.tracking_due(db, now=later)[0] == [account.id]


def test_an_order_from_before_the_read_back_waits_for_the_clock(db):
    account = _account(db)
    _order(db, account, minutes_ago=30)
    _read_back(db, account, minutes_ago=20)

    assert schedule.tracking_due(db)[0] == []


def test_an_unconfirmed_order_counts_too(db):
    account = _account(db)
    _order(db, account, minutes_ago=40)
    _read_back(db, account, minutes_ago=20)
    _order(db, account, minutes_ago=5, status=OrderStatus.unconfirmed)

    assert schedule.tracking_due(db)[0] == [account.id]


def test_orders_filed_with_no_run_are_left_to_the_clock(db):
    """An order report or id upload files many orders at once: no read-back
    of every account in it a few minutes later."""
    account = _account(db)
    _order(db, account, minutes_ago=40)
    _read_back(db, account, minutes_ago=20)
    _order(db, account, minutes_ago=5, by_a_run=False)

    assert schedule.tracking_due(db)[0] == []


def test_nothing_starts_while_the_order_run_is_still_going(db):
    account = _account(db)
    _read_back(db, account, minutes_ago=20)
    _order(db, account, minutes_ago=5)
    running = Job(type=JobType.order, status=JobStatus.running)
    db.add(running)
    db.commit()

    assert schedule.tracking_due(db) == ([], f"run #{running.id} is still going")
