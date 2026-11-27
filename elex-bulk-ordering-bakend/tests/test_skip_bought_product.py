"""Skip accounts that already bought this product (order form → API → run)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.automation.ordering import PAYMENT_COD
from app.core.account_picking import already_bought, product_key, same_product
from app.db.models import Job, JobItem, JobItemStatus, JobStatus, JobType, Order, OrderStatus, naive_utcnow
from app.jobs import tasks
from tests.test_x1_mock_mode import _Recorder, _post, api, no_browser, recorder  # noqa: F401 — fixtures
from tests.test_x3_account_selection import _account, address_book  # noqa: F401 — fixtures

#: The owner's link of 24 Nov 2026: the Wave Blue variant, with search tracking.
BLUE = (
    "https://www.flipkart.com/vivo-t5-lite-44w-5g-wave-blue-64-gb/p/itm5420c578ec2df"
    "?pid=MOBHQSFFCMZN6GBC&lid=LSTMOBHQSFFCMZN6GBCPWZJQM&marketplace=FLIPKART&q=t5+lite+4+64"
    "&srno=s_1_1&ppt=loginpage&ov_redirect=true"
)
#: The same phone as a run on 23 Nov recorded it: other tracking, same pid.
BLUE_PLAIN = "https://www.flipkart.com/vivo-t5-lite-44w-5g-wave-blue-64-gb/p/itm5420c578ec2df?pid=MOBHQSFFCMZN6GBC"
#: The same product page, another colour.
SHADOW = "https://www.flipkart.com/vivo-t5-lite-44w-5g-twilight-shadow-64-gb/p/itm5420c578ec2df?pid=MOBHQSFFAAAAAAAA"
OTHER_PHONE = "https://www.flipkart.com/some-phone/p/itm0000000000001?pid=MOBXXXXXXXXXXXXX"


def _bought(db, account, link, status=OrderStatus.placed, *, days_ago=1, order_id="OD1"):
    db.add(
        Order(
            account_id=account.id,
            status=status,
            product_link=link,
            order_id=order_id,
            created_at=naive_utcnow() - timedelta(days=days_ago),
        )
    )
    db.commit()


# --- reading the product off a link -----------------------------------------------
def test_the_key_is_the_variant_and_the_page_whatever_the_tracking():
    key = product_key(BLUE)
    assert (key.pid, key.item) == ("MOBHQSFFCMZN6GBC", "itm5420c578ec2df")
    assert same_product(key, BLUE_PLAIN)
    assert same_product(key, BLUE_PLAIN.replace("MOBHQSFFCMZN6GBC", "mobhqsffcmzn6gbc"))


def test_another_colour_of_the_same_page_is_another_product():
    assert not same_product(product_key(BLUE), SHADOW)
    assert not same_product(product_key(BLUE), OTHER_PHONE)


def test_without_a_pid_on_one_side_the_page_decides():
    page_only = "https://www.flipkart.com/vivo-t5-lite/p/itm5420c578ec2df"
    assert same_product(product_key(BLUE), page_only)
    assert same_product(product_key(page_only), BLUE_PLAIN)
    assert not same_product(product_key(page_only), OTHER_PHONE)


def test_a_short_link_matches_only_itself():
    short = "https://dl.flipkart.com/s/AbCdEfuuuN"
    assert not product_key(short).readable
    assert same_product(product_key(short), short)
    assert not same_product(product_key(short), "https://dl.flipkart.com/s/ZZZZZZuuuN")
    assert not same_product(product_key(short), BLUE)


def test_an_id_that_only_contains_this_one_is_not_this_product():
    longer = BLUE_PLAIN.replace("MOBHQSFFCMZN6GBC", "MOBHQSFFCMZN6GBCX")
    assert not same_product(product_key(BLUE), longer)


# --- which accounts bought it ----------------------------------------------------
def test_only_placed_or_unconfirmed_orders_of_this_product_count_at_any_age(db):
    placed = _account(db, "placed@catbar.net")
    unconfirmed = _account(db, "unconf@catbar.net")
    long_ago = _account(db, "old@catbar.net")
    failed = _account(db, "failed@catbar.net")
    over = _account(db, "over@catbar.net")
    colour = _account(db, "colour@catbar.net")
    fresh = _account(db, "fresh@catbar.net")
    _bought(db, placed, BLUE_PLAIN, order_id="OD439231950004021100")
    _bought(db, unconfirmed, BLUE_PLAIN, OrderStatus.unconfirmed)
    _bought(db, long_ago, BLUE_PLAIN, days_ago=400)
    _bought(db, failed, BLUE_PLAIN, OrderStatus.failed)
    _bought(db, over, BLUE_PLAIN, OrderStatus.over_target)
    _bought(db, colour, SHADOW)
    _bought(db, fresh, OTHER_PHONE)

    ids = [a.id for a in (placed, unconfirmed, long_ago, failed, over, colour, fresh)]
    found = already_bought(db, ids, BLUE)
    assert set(found) == {placed.id, unconfirmed.id, long_ago.id}
    assert found[placed.id].order_id == "OD439231950004021100"
    assert found[placed.id].describe().startswith("already bought this product (OD439231950004021100 on ")
    # Only the run's own accounts are looked at.
    assert already_bought(db, [fresh.id], BLUE) == {}
    assert already_bought(db, [], BLUE) == {}


# --- the run ----------------------------------------------------------------------
def _run(db, account_ids, **kwargs):
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()
    asyncio.run(
        tasks.run_order_job(
            job.id, account_ids, product_link=BLUE, target=20000.0, payment_mode=PAYMENT_COD, mock=True, **kwargs
        )
    )
    db.expire_all()
    items = {item.account_id: item for item in db.query(JobItem).filter(JobItem.job_id == job.id)}
    return db.get(Job, job.id), items


def test_a_run_passes_over_accounts_that_bought_it_and_orders_on_the_rest(db, address_book, no_browser):
    done = _account(db, "done@catbar.net")
    other_colour = _account(db, "colour@catbar.net")
    fresh = _account(db, "fresh@catbar.net")
    _bought(db, done, BLUE_PLAIN, order_id="OD439231950004021100")
    _bought(db, other_colour, SHADOW)

    job, items = _run(db, [done.id, other_colour.id, fresh.id], skip_bought_product=True, total_attempts=2)

    assert items[done.id].status == JobItemStatus.skipped
    assert "already bought this product (OD439231950004021100" in items[done.id].message
    assert items[done.id].started_at is None
    # The passed-over account used none of the two attempts: both others ran.
    assert items[other_colour.id].message.startswith("[mock]")
    assert items[fresh.id].message.startswith("[mock]")
    assert job.total_items == 3
    assert job.skipped_items >= 1
    assert "1 account(s) already bought this product — passed over; 2 left to order on." in (job.log or "")


def test_without_the_switch_nothing_is_passed_over(db, address_book, no_browser):
    done = _account(db, "done@catbar.net")
    _bought(db, done, BLUE_PLAIN)

    _, items = _run(db, [done.id])
    assert items[done.id].message.startswith("[mock]")


def test_a_run_where_every_account_bought_it_ends_cleanly(db, address_book, no_browser):
    a = _account(db, "a@catbar.net")
    b = _account(db, "b@catbar.net")
    _bought(db, a, BLUE_PLAIN)
    _bought(db, b, BLUE_PLAIN)

    job, items = _run(db, [a.id, b.id], skip_bought_product=True)
    assert {item.status for item in items.values()} == {JobItemStatus.skipped}
    assert job.status != JobStatus.running
    assert job.done_items == 2


def test_the_switch_reaches_the_run_and_its_job(api, db, recorder):
    response = _post(api, skip_bought_product="true")
    assert response.status_code == 201, response.text
    assert recorder.calls[0]["skip_bought_product"] is True
    assert response.json()["payload"]["skip_bought_product"] is True


def test_the_switch_is_off_unless_sent(api, db, recorder):
    response = _post(api)
    assert response.status_code == 201, response.text
    assert recorder.calls[0]["skip_bought_product"] is False
    assert "skip_bought_product" not in response.json()["payload"]
