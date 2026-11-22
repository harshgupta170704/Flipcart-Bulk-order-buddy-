"""The two invoice checks across orders (26 Oct 2026): "Invoice amount
differs" and "Product invoice missing".

Each is held to the engine's contract like the rules before it — it fires on
the condition, stays quiet otherwise, is one row per problem however often the
rules run, and closes itself when the condition clears — and to what makes it
worth a place in the queue: Flipkart's fee and transport invoices are not a
difference, and a delivered unit whose page showed no date can still be asked
about.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core import alerts, invoices
from app.db.models import (
    Account,
    AccountStatus,
    Alert,
    AlertSeverity,
    Invoice,
    Order,
    OrderStatus,
    utcnow,
)
from app.io import invoice_store
from app.io.invoice_store import InvoiceStore

#: The owner's first sample order: a product invoice of ₹50,999 and two fee
#: invoices of ₹99 and ₹49, as Flipkart hands them out.
SAMPLES = Path(__file__).parent / "fixtures" / "invoices" / "OD332627468086718100"


# --- helpers ----------------------------------------------------------------------
@pytest.fixture
def account(db) -> Account:
    row = Account(email="invoices@test.com", status=AccountStatus.new)
    db.add(row)
    db.commit()
    return row


def _now() -> datetime:
    return utcnow().replace(tzinfo=None)


def _order(db, account: Account, **fields) -> Order:
    """A placed order with a Flipkart id, overridable field by field."""
    count = db.query(Order).count() + 1
    values = {
        "account_id": account.id,
        "status": OrderStatus.placed,
        "model": "vivo T5x 5G",
        "qty": 1,
        "order_id": f"OD{count:018d}",
        "placed_at": _now() - timedelta(days=10),
        **fields,
    }
    order = Order(**values)
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _invoice(
    db,
    order: Order,
    *,
    number: str | None,
    total: float | None,
    kind: str = "product",
    tag: str | None = None,
) -> Invoice:
    """An invoice row as the reader leaves it, straight on the ledger: the
    rules read rows, not files. ``tag`` makes a second copy of the same
    invoice (other bytes, same number)."""
    seed = f"{order.id}-{number}-{tag}-{kind}-{total}"
    row = Invoice(
        order_pk=order.id,
        order_id=order.order_id,
        kind=kind,
        invoice_type={"product": "Product", "fee": "Secure Packaging Fee", "credit_note": "Credit Note"}.get(kind),
        invoice_number=number,
        grand_total=total,
        path=f"{seed}.pdf",
        size=1,
        sha256=hashlib.sha256(seed.encode()).hexdigest(),
        source="upload",
    )
    db.add(row)
    db.commit()
    return row


def _deliver(db, order: Order, *, days_ago: float | None, position: int = 1, status: str = "Delivered") -> None:
    """Mark one unit as the read-back would: its wording, and the date beside it."""
    db.refresh(order)
    unit = next(u for u in order.units if u.position == position)
    unit.platform_status = status
    unit.delivery_date = None if days_ago is None else _now() - timedelta(days=days_ago)
    unit.synced_at = _now()
    db.commit()


def _keys(findings) -> set[str]:
    return {finding.key for finding in findings}


def _alert(db, key: str) -> Alert | None:
    row = db.scalar(select(Alert).where(Alert.key == key))
    if row is not None:
        db.refresh(row)
    return row


def test_both_invoice_checks_are_registered_rules() -> None:
    kinds = {rule.kind for rule in alerts.RULES}
    assert {"invoice_amount_mismatch", "product_invoice_missing"} <= kinds


def test_evaluate_counts_the_invoice_checks_with_the_rest(db) -> None:
    counts = alerts.evaluate(db)
    assert counts["invoice_amount_mismatch"] == 0
    assert counts["product_invoice_missing"] == 0


# --- invoice amount differs ---------------------------------------------------------
def test_a_product_invoice_that_differs_from_the_amount_fires_as_a_warning(db, account) -> None:
    order = _order(db, account, amount=18207.0)
    _invoice(db, order, number="FAXYZ1", total=17999.0)

    findings = alerts.find_invoice_amount_mismatches(db)

    assert _keys(findings) == {f"invoice_amount_mismatch:order:{order.id}"}
    finding = findings[0]
    assert finding.severity == AlertSeverity.warning
    assert (finding.account_id, finding.order_id) == (account.id, order.id)
    assert order.order_id in finding.message
    assert "₹17,999.00" in finding.message and "₹18,207.00" in finding.message
    assert account.email in finding.message
    assert finding.details == {
        "amount": 18207.0,
        "product_invoice_total": 17999.0,
        "with_fee_invoices": None,
        "invoice_numbers": "FAXYZ1",
    }


@pytest.mark.parametrize(
    ("invoice_total", "fires"),
    [(18207.0, False), (18206.5, False), (18206.0, False), (18208.0, False), (18205.9, True), (18208.5, True)],
)
def test_within_a_rupee_is_rounding_not_a_difference(db, account, invoice_total, fires) -> None:
    order = _order(db, account, amount=18207.0)
    _invoice(db, order, number="FA1", total=invoice_total)
    assert bool(alerts.find_invoice_amount_mismatches(db)) is fires


def test_the_tolerance_is_a_parameter(db, account) -> None:
    order = _order(db, account, amount=18207.0)
    _invoice(db, order, number="FA1", total=18200.0)
    assert len(alerts.find_invoice_amount_mismatches(db)) == 1
    assert alerts.find_invoice_amount_mismatches(db, tolerance=10) == []


def test_fee_invoices_that_make_up_the_amount_are_no_difference(db, account) -> None:
    """Flipkart bills its fees and the delivery on invoices of their own; the
    checkout's amount is all of it. An alert per fee would be noise."""
    order = _order(db, account, amount=18207.0)
    _invoice(db, order, number="FA1", total=17999.0)
    _invoice(db, order, number="BF1", total=129.0, kind="fee")
    _invoice(db, order, number="BF2", total=79.0, kind="fee")
    assert alerts.find_invoice_amount_mismatches(db) == []


def test_the_owners_sample_invoices_add_up_to_what_was_charged(db, account, tmp_path, monkeypatch) -> None:
    """The real PDFs of one order — ₹50,999 for the phone, ₹99 and ₹49 in fees
    — read by the real reader: ₹51,147 charged is no difference, nor is
    ₹50,999 (an amount recorded without the fees); ₹52,000 is."""
    monkeypatch.setattr(invoice_store, "store_root", lambda: tmp_path / "invoices")
    store = InvoiceStore.from_settings()
    # The id the PDFs print: attach refuses a PDF that names another order.
    order = _order(db, account, amount=51147.0, order_id="OD332627468086718100")
    for name in ("file_1.pdf", "file_2.pdf", "file_3.pdf"):
        assert invoices.attach(db, store, order, (SAMPLES / name).read_bytes(), source="upload") is not None
    db.commit()

    assert alerts.find_invoice_amount_mismatches(db) == []
    order.amount = 50999.0
    db.commit()
    assert alerts.find_invoice_amount_mismatches(db) == []

    order.amount = 52000.0
    db.commit()
    [finding] = alerts.find_invoice_amount_mismatches(db)

    assert finding.order_id == order.id
    assert "₹50,999.00 (₹51,147.00 with its 2 fee invoices)" in finding.message and "₹52,000.00" in finding.message
    assert finding.details["with_fee_invoices"] == 51147.0
    assert finding.details["invoice_numbers"] == "FASUUT2500113513"


def test_fees_that_do_not_make_up_the_amount_are_named_in_the_message(db, account) -> None:
    order = _order(db, account, amount=18500.0)
    _invoice(db, order, number="FA1", total=17999.0)
    _invoice(db, order, number="BF1", total=129.0, kind="fee")

    [finding] = alerts.find_invoice_amount_mismatches(db)

    assert "₹17,999.00 (₹18,128.00 with its 1 fee invoice)" in finding.message
    assert "not the ₹18,500.00 the order was recorded at" in finding.message
    assert finding.details["with_fee_invoices"] == 18128.0


def test_an_order_invoiced_in_parts_is_compared_by_the_parts_sum(db, account) -> None:
    whole = _order(db, account, amount=30000.0, qty=2)
    _invoice(db, whole, number="FA1", total=15000.0)
    _invoice(db, whole, number="FA2", total=15000.0)
    short = _order(db, account, amount=30000.0, qty=2)
    _invoice(db, short, number="FB1", total=15000.0)
    _invoice(db, short, number="FB2", total=14000.0)

    [finding] = alerts.find_invoice_amount_mismatches(db)

    assert finding.order_id == short.id
    assert "2 product invoices come to ₹29,000.00" in finding.message
    assert finding.details["invoice_numbers"] == "FB1, FB2"


def test_the_same_invoice_on_file_twice_is_counted_once(db, account) -> None:
    """Downloaded again, the PDF's bytes differ and the store keeps both; by
    its number it is one invoice, and must not double the total."""
    order = _order(db, account, amount=17999.0)
    _invoice(db, order, number="FA1", total=17999.0, tag="first download")
    _invoice(db, order, number="fa1 ", total=17999.0, tag="second download")
    assert alerts.find_invoice_amount_mismatches(db) == []

    # And named once when it does differ.
    order.amount = 18500.0
    db.commit()
    [finding] = alerts.find_invoice_amount_mismatches(db)
    assert finding.details["invoice_numbers"] == "FA1"
    assert "'s product invoice comes to ₹17,999.00" in finding.message


def test_a_product_invoice_with_no_total_read_raises_nothing(db, account) -> None:
    """No part of a sum is guessed: one unreadable total makes the sum unknown."""
    order = _order(db, account, amount=30000.0)
    _invoice(db, order, number="FA1", total=15000.0)
    _invoice(db, order, number="FA2", total=None)
    assert alerts.find_invoice_amount_mismatches(db) == []


def test_a_fee_total_not_read_or_a_pdf_not_placed_leaves_the_sum_unknown(db, account) -> None:
    """Only known values are compared, like the GST check: the ₹208 missing
    here may be on the fee invoice whose total was not read, or on the PDF
    the reader could not place at all."""
    fee_unread = _order(db, account, amount=18207.0)
    _invoice(db, fee_unread, number="FA1", total=17999.0)
    _invoice(db, fee_unread, number="BF1", total=None, kind="fee")
    unplaced = _order(db, account, amount=18207.0)
    _invoice(db, unplaced, number="FA2", total=17999.0)
    _invoice(db, unplaced, number=None, total=None, kind="unknown")
    assert alerts.find_invoice_amount_mismatches(db) == []

    # A credit note is no part of what was charged, and changes nothing.
    credited = _order(db, account, amount=18207.0)
    _invoice(db, credited, number="FA3", total=17999.0)
    _invoice(db, credited, number="CN3", total=17999.0, kind="credit_note")
    assert _keys(alerts.find_invoice_amount_mismatches(db)) == {f"invoice_amount_mismatch:order:{credited.id}"}


@pytest.mark.parametrize(
    "status", [OrderStatus.failed, OrderStatus.unconfirmed, OrderStatus.over_target, OrderStatus.pending]
)
def test_only_placed_orders_are_compared(db, account, status) -> None:
    order = _order(db, account, amount=18207.0, status=status)
    _invoice(db, order, number="FA1", total=1.0)
    assert alerts.find_invoice_amount_mismatches(db) == []


def test_nothing_to_compare_raises_nothing(db, account) -> None:
    no_amount = _order(db, account, amount=None)
    _invoice(db, no_amount, number="FA1", total=17999.0)
    fee_only = _order(db, account, amount=18207.0)
    _invoice(db, fee_only, number="BF1", total=99.0, kind="fee")
    credit_only = _order(db, account, amount=18207.0)
    _invoice(db, credit_only, number="CN1", total=17999.0, kind="credit_note")
    unreadable = _order(db, account, amount=18207.0)
    _invoice(db, unreadable, number=None, total=None, kind="unknown")
    _order(db, account, amount=18207.0)  # nothing on file at all

    assert alerts.find_invoice_amount_mismatches(db) == []


def test_the_amount_check_looks_back_a_quarter_like_the_other_order_rules(db, account) -> None:
    old = _order(
        db,
        account,
        amount=18207.0,
        created_at=_now() - timedelta(days=alerts.ORDER_RULE_WINDOW_DAYS + 5),
    )
    _invoice(db, old, number="FA1", total=1.0)
    assert alerts.find_invoice_amount_mismatches(db) == []


def test_evaluate_raises_it_once_and_closes_it_when_the_fees_arrive(db, account) -> None:
    order = _order(db, account, amount=18207.0)
    _invoice(db, order, number="FA1", total=17999.0)
    key = f"invoice_amount_mismatch:order:{order.id}"

    assert alerts.evaluate(db)["invoice_amount_mismatch"] == 1
    assert alerts.evaluate(db)["invoice_amount_mismatch"] == 1
    assert db.query(Alert).filter(Alert.kind == "invoice_amount_mismatch").count() == 1
    first = _alert(db, key)
    assert first is not None and first.resolved is False and first.severity == AlertSeverity.warning

    # The fee invoice is put on file: the invoices now make up the amount.
    _invoice(db, order, number="BF1", total=208.0, kind="fee")
    assert alerts.evaluate(db)["invoice_amount_mismatch"] == 0

    closed = _alert(db, key)
    assert closed.id == first.id
    assert closed.resolved is True and closed.resolved_by == alerts.RESOLVED_BY_RULE


# --- product invoice missing ----------------------------------------------------------
def test_a_delivered_order_with_no_product_invoice_fires_as_a_warning(db, account) -> None:
    order = _order(db, account, amount=18207.0)
    _deliver(db, order, days_ago=5)

    findings = alerts.find_missing_product_invoices(db)

    assert _keys(findings) == {f"product_invoice_missing:order:{order.id}"}
    finding = findings[0]
    assert finding.severity == AlertSeverity.warning
    assert (finding.account_id, finding.order_id) == (account.id, order.id)
    assert f"Order {order.order_id} was delivered 5 days ago and has no product invoice on file" in finding.message
    assert account.email in finding.message
    db.refresh(order)
    assert finding.details == {
        "days": 5,
        "delivered_on": order.units[0].delivery_date.date().isoformat(),
        "other_invoices_on_file": 0,
        "unreadable_invoices": 0,
        "grace_days": alerts.MISSING_INVOICE_DAYS,
    }


def test_inside_the_grace_period_it_does_not_fire(db, account) -> None:
    order = _order(db, account)
    _deliver(db, order, days_ago=2)
    assert alerts.find_missing_product_invoices(db) == []
    # The grace period is a parameter.
    assert _keys(alerts.find_missing_product_invoices(db, grace_days=1)) == {
        f"product_invoice_missing:order:{order.id}"
    }


@pytest.mark.parametrize("wording", ["Out for Delivery", "Shipped", "Cancelled", None])
def test_a_unit_not_delivered_raises_nothing(db, account, wording) -> None:
    order = _order(db, account)
    _deliver(db, order, days_ago=8, status=wording)
    assert alerts.find_missing_product_invoices(db) == []


@pytest.mark.parametrize("wording", ["Return Started", "Refund Completed"])
def test_a_unit_read_as_returned_or_refunded_raises_nothing(db, account, wording) -> None:
    """"Refund Completed" also describes a prepaid order cancelled before it
    shipped, which never had a product invoice, and the date beside a return
    or a refund is that step's, not the delivery's: only a delivered unit
    asks for its invoice."""
    order = _order(db, account)
    _deliver(db, order, days_ago=6, status=wording)
    assert alerts.find_missing_product_invoices(db) == []


def test_one_delivered_unit_is_enough_while_the_order_row_reads_in_transit(db, account) -> None:
    """The order row mirrors one representative unit — the one still on its
    way — so the check reads the units themselves."""
    order = _order(db, account, qty=2, platform_status="Out for Delivery")
    _deliver(db, order, days_ago=1, position=1, status="Out for Delivery")
    _deliver(db, order, days_ago=5, position=2)
    db.refresh(order)
    assert order.delivery_state == "in_transit"

    assert _keys(alerts.find_missing_product_invoices(db)) == {f"product_invoice_missing:order:{order.id}"}


def test_a_delivered_unit_with_no_date_counts_from_the_order_date(db, account) -> None:
    """Flipkart's order page can list its steps without dates: such a unit must
    not be the one delivered order that is never asked about."""
    old = _order(db, account, placed_at=_now() - timedelta(days=6))
    _deliver(db, old, days_ago=None)
    recent = _order(db, account, placed_at=_now() - timedelta(days=1))
    _deliver(db, recent, days_ago=None)

    [finding] = alerts.find_missing_product_invoices(db)

    assert finding.order_id == old.id
    assert "showed no delivery date" in finding.message and "6 days old" in finding.message
    assert finding.details["delivered_on"] is None
    assert finding.details["days"] == 6


def test_a_product_invoice_clears_it_and_the_other_invoices_do_not(db, account) -> None:
    order = _order(db, account)
    _deliver(db, order, days_ago=5)
    _invoice(db, order, number="BF1", total=99.0, kind="fee")
    _invoice(db, order, number=None, total=None, kind="unknown")
    key = f"product_invoice_missing:order:{order.id}"

    [finding] = alerts.find_missing_product_invoices(db)
    assert finding.details["other_invoices_on_file"] == 2
    assert finding.details["unreadable_invoices"] == 1
    # A PDF is on file, so the message must not read as if nothing were.
    assert "has no readable product invoice on file (one of its PDFs could not be read)" in finding.message

    assert alerts.evaluate(db)["product_invoice_missing"] == 1
    assert alerts.evaluate(db)["product_invoice_missing"] == 1  # one row, however often it runs
    assert db.query(Alert).filter(Alert.kind == "product_invoice_missing").count() == 1
    opened = _alert(db, key)
    assert opened is not None and opened.resolved is False

    _invoice(db, order, number="FA1", total=17999.0)
    assert alerts.evaluate(db)["product_invoice_missing"] == 0

    closed = _alert(db, key)
    assert closed.id == opened.id
    assert closed.resolved is True and closed.resolved_by == alerts.RESOLVED_BY_RULE


@pytest.mark.parametrize("status", [OrderStatus.failed, OrderStatus.unconfirmed])
def test_only_placed_orders_are_asked_for_their_invoice(db, account, status) -> None:
    order = _order(db, account, status=status)
    _deliver(db, order, days_ago=8)
    assert alerts.find_missing_product_invoices(db) == []


def test_the_missing_invoice_check_looks_back_a_quarter(db, account) -> None:
    old = _order(db, account, created_at=_now() - timedelta(days=alerts.ORDER_RULE_WINDOW_DAYS + 5))
    _deliver(db, old, days_ago=60)
    assert alerts.find_missing_product_invoices(db) == []


def test_a_missing_invoice_does_not_move_up_the_queue_as_it_ages(db, account, monkeypatch) -> None:
    """The age is a detail that moves by itself (VOLATILE_DETAIL_KEYS): a pass
    a day later that finds the same invoice still missing leaves updated_at —
    the queue's order — alone, and refreshes the age."""
    order = _order(db, account)
    _deliver(db, order, days_ago=5)
    alerts.evaluate(db)
    key = f"product_invoice_missing:order:{order.id}"
    before = _alert(db, key)
    updated_at, delivered_on = before.updated_at, before.details["delivered_on"]

    tomorrow = utcnow() + timedelta(days=1)
    monkeypatch.setattr(alerts, "utcnow", lambda: tomorrow)
    alerts.evaluate(db)

    after = _alert(db, key)
    assert after.updated_at == updated_at
    assert after.details["days"] == 6
    assert after.details["delivered_on"] == delivered_on
    assert "delivered 6 days ago" in after.message
