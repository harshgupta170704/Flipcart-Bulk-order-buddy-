"""Rows an order report refuses, and how it counts the rest (review of 27 Oct
2026): a row for a unit the ledger holds under another account than the one
it names; and a row Flipkart's page contradicts that fills nothing, which was
counted both as "kept as Flipkart says" and as "already as the report says".
"""

from __future__ import annotations

from datetime import datetime

from app.db.models import Account, AccountStatus
from tests.w7_report_rows import UNITS, api, buyer, line, order_of, report, units_of, upload  # noqa: F401 — fixtures


def test_a_held_unit_named_under_another_account_is_refused(api, db, buyer):
    db.add(Account(email="other@example.com", status=AccountStatus.session_valid))
    db.commit()
    upload(api, report(line(unit_id=UNITS[0])))

    out = upload(
        api, report(line(unit_id=UNITS[0], platform_email="other@example.com", billing_phone="8888888888"))
    ).json()

    assert out["rejected"] == [
        {"row": 2, "reason": "OD338967210239333101 is in the ledger under another account than other@example.com"}
    ]
    assert (out["created"], out["updated"], out["unchanged"]) == (0, 0, 0)
    order = order_of(db)
    assert order.account_id == buyer.id and order.billing_phone is None


def test_a_held_unit_named_under_an_email_no_account_has_is_refused(api, db, buyer):
    upload(api, report(line(unit_id=UNITS[0])))

    out = upload(api, report(line(unit_id=UNITS[0], platform_email="nobody@example.com"))).json()

    assert [r["row"] for r in out["rejected"]] == [2]


def test_a_contradicted_row_that_fills_nothing_is_counted_as_kept_only(api, db, buyer):
    upload(api, report(line(unit_id=UNITS[0], status="Shipped", seller_name="Retailer A")))
    (unit,) = units_of(db)
    unit.synced_at = datetime(2026, 10, 26, 6, 0)
    unit.platform_status = "Delivered"
    db.commit()

    out = upload(api, report(line(unit_id=UNITS[0], status="Shipped", seller_name="Retailer A"))).json()

    assert (out["kept_platform"], out["updated"], out["unchanged"]) == (1, 0, 0)
    (unit,) = units_of(db)
    assert unit.platform_status == "Delivered"
