"""Step B: email-domain filter, exclude recently ordered, smart selection."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.automation.ordering import PAYMENT_COD
from app.core import account_filter as account_query
from app.core.account_filter import AccountFilter, email_domain
from app.core.account_picking import least_recently_ordered_first, recently_ordered
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    Gst,
    Job,
    JobItem,
    JobStatus,
    JobType,
    Order,
    OrderStatus,
    Session as SessionModel,
    naive_utcnow,
)
from app.jobs import tasks
from app.schemas.job import AccountFilterIn
from tests.test_x1_mock_mode import _Recorder, _post, api, no_browser, recorder  # noqa: F401 — fixtures

PRODUCT = "https://www.flipkart.com/some-phone/p/itm0000000000001"


def _account(db, email, *, session=True):
    account = Account(email=email, status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    if session:
        db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": email}], valid=True))
        db.commit()
    return account


def _order(db, account, status, days_ago):
    db.add(Order(account_id=account.id, status=status, created_at=naive_utcnow() - timedelta(days=days_ago)))
    db.commit()


# --- domains --------------------------------------------------------------------
def test_email_domain_reads_the_part_after_the_last_at():
    assert email_domain("A.B@SonicLip.in") == "soniclip.in"
    assert email_domain("no-at-sign") is None


def test_a_domain_filter_matches_the_end_of_the_address_in_any_case(db):
    a = _account(db, "one@soniclip.in")
    b = _account(db, "two@SWYPAY.in")
    _account(db, "three@soniclip.in.evil.com")
    _account(db, "x_y@other.com")

    assert set(account_query.ids(db, AccountFilter(domains=("soniclip.in",)))) == {a.id}
    assert set(account_query.ids(db, AccountFilter(domains=("soniclip.in", "swypay.in")))) == {a.id, b.id}


def test_a_domain_with_like_wildcards_matches_only_itself(db):
    _account(db, "a@xyz.com")
    assert account_query.ids(db, AccountFilter(domains=("_yz.com",))) == []


def test_the_run_filter_carries_domains_lower_cased():
    flt = AccountFilterIn(domains=[" SonicLip.in ", ""]).to_filter()
    assert flt.domains == ("soniclip.in",)


def test_filter_options_list_domains_most_accounts_first(db, client, auth_headers):
    for email in ("a@b.in", "c@b.in", "d@z.in"):
        _account(db, email, session=False)
    body = client.get("/accounts/filter-options", headers=auth_headers).json()
    assert body["domains"] == [{"value": "b.in", "count": 2}, {"value": "z.in", "count": 1}]


# --- exclude recent / smart order -------------------------------------------------
def test_only_bought_orders_inside_the_window_count_as_recent(db):
    fresh = _account(db, "fresh@t.in")
    old = _account(db, "old@t.in")
    failed = _account(db, "failed@t.in")
    unconfirmed = _account(db, "unconf@t.in")
    _order(db, fresh, OrderStatus.placed, 2)
    _order(db, old, OrderStatus.placed, 40)
    _order(db, failed, OrderStatus.failed, 1)
    _order(db, unconfirmed, OrderStatus.unconfirmed, 1)

    ids = [fresh.id, old.id, failed.id, unconfirmed.id]
    assert recently_ordered(db, ids, 7) == {fresh.id, unconfirmed.id}
    assert recently_ordered(db, ids, 0) == set()


def test_smart_order_puts_never_ordered_first_then_the_oldest(db):
    recent = _account(db, "r@t.in")
    never_a = _account(db, "n1@t.in")
    oldest = _account(db, "o@t.in")
    never_b = _account(db, "n2@t.in")
    _order(db, recent, OrderStatus.placed, 1)
    _order(db, oldest, OrderStatus.placed, 90)

    ordered = least_recently_ordered_first(db, [recent.id, never_a.id, oldest.id, never_b.id])
    assert ordered == [never_a.id, never_b.id, oldest.id, recent.id]


@pytest.fixture
def address_book(db):
    gst = Gst(label="ACME", gstin="29ABCDE1234F1Z5", business_name="Acme", is_default=True)
    db.add(gst)
    db.commit()
    db.add(Address(label="w", name="Acme", line1="1 Road", pincode="560001", gst_id=gst.id, is_default=True))
    db.commit()


def test_a_run_passes_over_recently_ordered_accounts(db, address_book, no_browser):
    busy = _account(db, "busy@t.in")
    free = _account(db, "free@t.in")
    _order(db, busy, OrderStatus.placed, 1)
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()

    asyncio.run(
        tasks.run_order_job(
            job.id, [busy.id, free.id], product_link=PRODUCT, target=1000.0,
            payment_mode=PAYMENT_COD, mock=True, exclude_ordered_days=7,
        )
    )
    db.expire_all()
    items = {item.account_id: item for item in db.query(JobItem).filter(JobItem.job_id == job.id)}
    assert "ordered in the last 7 day(s)" in items[busy.id].message
    assert items[free.id].message.startswith("[mock]")


def test_the_new_fields_reach_the_run_and_its_job(api, db, recorder):
    response = _post(api, exclude_ordered_days="14", smart_selection="true")
    assert response.status_code == 201, response.text
    assert recorder.calls[0]["exclude_ordered_days"] == 14
    assert recorder.calls[0]["smart_selection"] is True
    assert response.json()["payload"]["exclude_ordered_days"] == 14


@pytest.mark.parametrize("days", ["0", "-1", "400"])
def test_an_exclusion_window_out_of_range_starts_nothing(api, db, recorder, days):
    assert _post(api, exclude_ordered_days=days).status_code == 422
    assert recorder.calls == []
