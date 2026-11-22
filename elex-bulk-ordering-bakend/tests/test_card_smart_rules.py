"""Card payment rules from the order form's Cards Filters: the smart cap,
exclusion by failure code, and the Card Type column."""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from app.api.jobs import _failure_codes, _only_card_type
from app.core.card_pool import (
    CapCounter,
    CardRules,
    card_key,
    failed_recently,
    failure_code,
    plan,
)
from app.db.models import Account, AccountStatus, CardFailure, Order, OrderStatus
from app.io.cards import CardRecord, CardSheet, parse_card_sheet

CARD_A = "4111111111111111"
CARD_B = "5555555555554444"


class _Card:
    def __init__(self, number: str) -> None:
        self.number = number


def test_failure_codes_from_reasons():
    assert failure_code("Payment declined by bank") == "DECLINED"
    assert failure_code("insufficient balance on card") == "INSUFFICIENT_FUNDS"
    assert failure_code("OTP was not entered in time") == "OTP_FAILED"
    assert failure_code("bank page timed out") == "BANK_TIMEOUT"
    assert failure_code("payment could not complete") == "PAYMENT_OTHER"
    assert failure_code("out of stock") is None


def test_parse_failure_codes_refuses_unknown():
    assert _failure_codes(" declined, OTP_FAILED ,declined") == ("DECLINED", "OTP_FAILED")
    assert _failure_codes(None) == ()
    with pytest.raises(HTTPException) as err:
        _failure_codes("DECLINED,NOPE")
    assert err.value.status_code == 422


def test_rules_payload_and_window():
    rules = CardRules(smart_max_uses=5, exclude_failed_codes=("DECLINED",))
    assert rules.any()
    assert rules.failed_window == 30  # codes alone look back 30 days
    assert rules.as_payload() == {"card_smart_max_uses": 5, "card_exclude_failed_codes": ["DECLINED"]}


def _accounts(db, n: int) -> list[Account]:
    rows = [Account(email=f"c{i}@test.com", phone=f"91000000{i}", status=AccountStatus.new) for i in range(n)]
    db.add_all(rows)
    db.commit()
    return rows


def test_failed_recently_only_counts_chosen_codes(db):
    db.add_all(
        [
            CardFailure(card_key=card_key(CARD_A), reason="Payment declined"),
            CardFailure(card_key=card_key(CARD_B), reason="OTP not received"),
        ]
    )
    db.commit()
    assert failed_recently(db, [CARD_A, CARD_B], 30) == {CARD_A, CARD_B}
    assert failed_recently(db, [CARD_A, CARD_B], 30, ["DECLINED"]) == {CARD_A}


def test_smart_cap_blocks_spent_cards_and_caps_the_rest(db):
    a, b = _accounts(db, 2)
    # Card A already paid for 3 orders in the window, card B for 1.
    db.add_all(
        [Order(account_id=a.id, status=OrderStatus.placed, card_number=CARD_A) for _ in range(3)]
        + [Order(account_id=b.id, status=OrderStatus.placed, card_number=CARD_B)]
    )
    db.commit()

    result = plan(db, [a.id, b.id], {a.id: _Card(CARD_A), b.id: _Card(CARD_B)}, CardRules(smart_max_uses=3))
    assert a.id in result.blocked and "smart cap" in result.blocked[a.id]
    assert b.id not in result.blocked
    assert result.caps[CARD_B] == 2


def test_cap_counter_takes_the_lower_of_fixed_and_smart():
    cap = CapCounter(max_uses=5, per_card={CARD_B: 2})
    assert cap.limit(CARD_B) == 2
    assert cap.limit(CARD_A) == 5
    assert cap.claim(CARD_B) and cap.claim(CARD_B)
    assert not cap.claim(CARD_B)
    cap.release(CARD_B)
    assert cap.claim(CARD_B)
    assert CapCounter(None).claim(CARD_A)


def _sheet_bytes() -> bytes:
    wb = Workbook()
    sheet = wb.active
    sheet.append(["Email", "Card Number", "Exp Month", "Exp Year", "CVV", "Card Type"])
    sheet.append(["c0@test.com", CARD_A, "12", "2030", "123", "HDFC Virtual"])
    sheet.append(["c1@test.com", CARD_B, "12", "2030", "456", "AXIS Corporate"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def test_sheet_reads_card_type_and_run_keeps_only_that_type():
    sheet = parse_card_sheet(io.BytesIO(_sheet_bytes()))
    assert [r.card_type for r in sheet.records] == ["HDFC Virtual", "AXIS Corporate"]

    only = _only_card_type(sheet, "hdfc virtual")
    assert [r.number for r in only.records] == [CARD_A]
    assert _only_card_type(sheet, "") is sheet
    with pytest.raises(HTTPException):
        _only_card_type(sheet, "AMEX Corporate")


def test_sheet_without_type_column_is_used_whole():
    sheet = CardSheet(records=[CardRecord(number=CARD_A, month="1", year="2030", cvv="1")], rejected=[])
    assert _only_card_type(sheet, "HDFC Virtual") is sheet
