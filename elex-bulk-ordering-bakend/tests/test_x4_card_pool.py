"""Step C: card pool rules, read from our own order history.

Cooldown, least used first, a hard cap per card in one run, and skipping
cards that failed at the payment step lately (kept by fingerprint only).
"""

from __future__ import annotations

import asyncio
import io
from datetime import timedelta

import pytest
from openpyxl import Workbook

from app.automation.ordering import PAYMENT_CARD, CardInfo
from app.core.card_pool import (
    CapCounter,
    CardRules,
    card_key,
    failed_recently,
    is_payment_failure,
    plan,
    record_failure,
)
from app.db.models import (
    Account,
    AccountStatus,
    Address,
    CardFailure,
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
from tests.test_x1_mock_mode import _Recorder, api, no_browser, recorder  # noqa: F401 — fixtures

PRODUCT = "https://www.flipkart.com/some-phone/p/itm0000000000001"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
A, B, C = "4111111111111111", "5555555555554444", "4000056655665556"


def _card(number):
    return CardInfo(number=number, month="12", year="30", cvv="123")


def _account(db, email):
    account = Account(email=email, status=AccountStatus.session_valid)
    db.add(account)
    db.commit()
    db.add(SessionModel(account_id=account.id, cookies=[{"name": "SN", "value": email}], valid=True))
    db.commit()
    return account


def _paid(db, account, number, days_ago, status=OrderStatus.placed):
    db.add(Order(account_id=account.id, status=status, card_number=number, created_at=naive_utcnow() - timedelta(days=days_ago)))
    db.commit()


# --- the rules on their own ------------------------------------------------------
def test_the_fingerprint_is_stable_and_hides_the_number():
    assert card_key("4111 1111 1111 1111") == card_key(A)
    assert A not in card_key(A) and len(card_key(A)) == 64


def test_only_payment_step_failures_count_against_a_card():
    assert is_payment_failure("the bank declined the card")
    # A page problem is not the card's fault (audit of 19 Nov 2026).
    assert not is_payment_failure("Payment page did not load")
    assert not is_payment_failure("could not reach the payment page — nothing was charged")
    assert not is_payment_failure("Flipkart refused the gift card — nothing was charged")
    assert not is_payment_failure("card could not be entered (no card number box) — nothing was charged")
    assert is_payment_failure("Payment declined by bank")
    assert is_payment_failure("bank OTP was not accepted")
    assert not is_payment_failure("product out of stock")
    assert not is_payment_failure(None)


def test_cooldown_blocks_a_card_used_inside_the_window(db):
    x, y = _account(db, "x@t.in"), _account(db, "y@t.in")
    _paid(db, x, A, 2)
    _paid(db, y, B, 30)
    decided = plan(db, [x.id, y.id], {x.id: _card(A), y.id: _card(B)}, CardRules(cooldown_days=7))
    assert set(decided.blocked) == {x.id}
    assert "cooldown" in decided.blocked[x.id]


def test_an_unconfirmed_order_counts_as_a_use_and_a_failed_one_does_not(db):
    x, y = _account(db, "x@t.in"), _account(db, "y@t.in")
    _paid(db, x, A, 1, OrderStatus.unconfirmed)
    _paid(db, y, B, 1, OrderStatus.failed)
    decided = plan(db, [x.id, y.id], {x.id: _card(A), y.id: _card(B)}, CardRules(cooldown_days=7))
    assert set(decided.blocked) == {x.id}


def test_least_used_cards_go_first(db):
    x, y, z = _account(db, "x@t.in"), _account(db, "y@t.in"), _account(db, "z@t.in")
    for _ in range(3):
        _paid(db, x, A, 1)
    _paid(db, y, B, 1)
    decided = plan(db, [x.id, y.id, z.id], {x.id: _card(A), y.id: _card(B), z.id: _card(C)}, CardRules(least_used_days=30))
    assert decided.order == [z.id, y.id, x.id]
    assert decided.blocked == {}


def test_a_recent_failure_is_found_by_fingerprint(db):
    record_failure(db, A, "the bank declined the card", job_id=None)
    assert db.query(CardFailure).one().card_key == card_key(A)
    assert failed_recently(db, [A, B], 7) == {A}
    x = _account(db, "x@t.in")
    decided = plan(db, [x.id], {x.id: _card(A)}, CardRules(exclude_failed_days=7))
    assert "failed an order" in decided.blocked[x.id]


def test_no_card_number_is_ever_stored_in_the_failure_table(db):
    record_failure(db, A, "card declined", job_id=None)
    row = db.query(CardFailure).one()
    assert A not in " ".join(str(v) for v in (row.card_key, row.reason, row.job_id))


def test_the_cap_holds_and_gives_back_unused_claims():
    cap = CapCounter(2)
    assert cap.claim(A) and cap.claim(A)
    assert not cap.claim(A)
    cap.release(A)
    assert cap.claim(A)
    assert CapCounter(None).claim(A)


# --- in a run -------------------------------------------------------------------
@pytest.fixture
def address_book(db):
    gst = Gst(label="ACME", gstin="29ABCDE1234F1Z5", business_name="Acme", is_default=True)
    db.add(gst)
    db.commit()
    db.add(Address(label="w", name="Acme", line1="1 Road", pincode="560001", gst_id=gst.id, is_default=True))
    db.commit()


def _run(db, ids, cards, rules):
    job = Job(type=JobType.order, status=JobStatus.queued)
    db.add(job)
    db.commit()
    asyncio.run(
        tasks.run_order_job(
            job.id, ids, product_link=PRODUCT, target=1000.0, payment_mode=PAYMENT_CARD,
            cards=cards, mock=True, card_rules=rules,
        )
    )
    db.expire_all()
    return {item.account_id: item for item in db.query(JobItem).filter(JobItem.job_id == job.id)}


def test_a_card_on_cooldown_skips_its_account_in_a_run(db, address_book, no_browser):
    x, y = _account(db, "x@t.in"), _account(db, "y@t.in")
    _paid(db, x, A, 1)
    items = _run(db, [x.id, y.id], {x.id: _card(A), y.id: _card(B)}, CardRules(cooldown_days=7))
    assert "card cooldown" in items[x.id].message
    assert items[y.id].message.startswith("[mock]")


def test_the_hard_cap_stops_a_shared_card_after_n_orders(db, address_book, no_browser, monkeypatch):
    # Every mock order placed, so the cap is what stops the third.
    from app.automation import mock_ordering

    monkeypatch.setattr(mock_ordering, "FAIL_SHARE", 0.0)
    monkeypatch.setattr(mock_ordering, "PRICE_RANGE", (0.5, 0.9))
    accounts = [_account(db, f"s{n}@t.in") for n in range(3)]
    items = _run(db, [a.id for a in accounts], {a.id: _card(A) for a in accounts}, CardRules(max_uses=2))
    capped = [item for item in items.values() if "already pays for 2" in (item.message or "")]
    assert len(capped) == 1


# --- the API --------------------------------------------------------------------
def _sheet(email):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV"])
    sheet.append([email, A, "12", "30", "321"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_card_rules_reach_a_card_run_and_its_job(api, db, recorder):
    response = api.post(
        "/jobs/order",
        data={
            "product_link": PRODUCT, "payment_mode": "card", "target": "900",
            "card_cooldown_days": "3", "card_max_uses": "2", "card_least_used_days": "30", "card_exclude_failed_days": "7",
        },
        files={"cards_file": ("cards.xlsx", _sheet("api@test.com"), XLSX)},
    )
    assert response.status_code == 201, response.text
    rules = recorder.calls[0]["card_rules"]
    assert (rules.cooldown_days, rules.max_uses, rules.least_used_days, rules.exclude_failed_days) == (3, 2, 30, 7)
    payload = response.json()["payload"]
    assert payload["card_cooldown_days"] == 3 and payload["card_max_uses"] == 2


def test_card_rules_on_a_cash_on_delivery_run_are_refused(api, db, recorder):
    response = api.post("/jobs/order", data={"product_link": PRODUCT, "payment_mode": "cod", "target": "900", "card_cooldown_days": "3"})
    assert response.status_code == 422
    assert recorder.calls == []
