"""The checkout's quantity control, as mapped live on 24 Oct 2026.

The "Qty: 1" box opens a list whose "more" entry is an "Enter Quantity"
dialog with APPLY. Flipkart caps some products at one unit per order and says
so in a message gone within a second. What the run records is what the box
reads back, never what was asked for.
"""

import asyncio

from app.automation import ordering
from app.core import selectors as S
from tests.fakes import FakePage

LIMIT = (
    "You can only purchase 1 units of vivo T5 Lite 44W 5G (Twilight Shadow, 64 GB) "
    "in a single order. If you have a higher requirement, please create a new order."
)


def _page(box_reads, body: str = "Deliver to: Ayush") -> FakePage:
    page = FakePage(inner_text_map={S.QTY_BOX: box_reads, S.PAGE_BODY: body})
    return page


def _set(page: FakePage, qty: int) -> ordering.QuantityOutcome:
    return asyncio.run(ordering.set_quantity(page, qty))


def test_one_unit_touches_nothing():
    page = _page("Qty: 1")
    assert _set(page, 1) == ordering.QuantityOutcome(1)
    assert page.clicks == []


def test_the_dialog_is_used_and_the_box_is_read_back():
    page = _page(["Qty: 1", "Qty: 2"])  # the box updates after APPLY

    out = _set(page, 2)

    assert out == ordering.QuantityOutcome(2)
    assert page.clicks == [S.QTY_BOX, S.QTY_MORE, S.QTY_APPLY]
    assert page.fills[S.QTY_DIALOG_INPUT] == "2"


def test_a_per_order_cap_is_recorded_as_one_unit_with_flipkarts_reason():
    page = _page("Qty: 1", body=f"Deliver to: Ayush {LIMIT} Total Amount ₹18,128")

    out = _set(page, 3)

    assert out.applied == 1
    assert out.note is not None and "only purchase 1 units" in out.note
    assert page.fills[S.QTY_DIALOG_INPUT] == "3"


def test_the_short_list_is_used_when_there_is_no_more_entry():
    page = _page(["Qty: 1", "Qty: 2"])
    page.counts[S.QTY_MORE] = 0

    out = _set(page, 2)

    assert out == ordering.QuantityOutcome(2)
    assert page.clicks == [S.QTY_BOX, S.QTY_OPTION.format(value=2)]


def test_no_control_means_one_unit_and_says_so():
    page = _page("Qty: 1")
    page.counts[S.QTY_BOX] = 0  # never appears

    out = _set(page, 2)

    assert out.applied == 1
    assert "no quantity control" in (out.note or "")
    assert page.clicks == []


def test_set_qty_keeps_its_old_shape():
    assert asyncio.run(ordering.set_qty(_page(["Qty: 1", "Qty: 2"]), 2)) == 2


def test_the_runs_message_carries_flipkarts_reason():
    from app.db.models import OrderStatus
    from app.jobs.tasks import _order_message

    placed = {"order_id": "OD1", "amount": 18207.0, "qty": 1, "qty_note": LIMIT}
    message = _order_message(OrderStatus.placed, placed, 20000.0)
    assert message.startswith("placed: order OD1, amount ₹18,207.00; 1 unit — You can only purchase 1 units")

    plain = {"order_id": "OD2", "amount": 100.0, "qty": 2, "qty_note": None}
    assert _order_message(OrderStatus.placed, plain, None) == "placed: order OD2, amount ₹100.00"


def test_the_runs_messages_write_money_as_every_screen_does():
    """NFR-010: the ₹ sign, Indian grouping and two decimals, as the ledger
    shows the same order — not "129900.00 > 125000.00"."""
    from app.db.models import OrderStatus
    from app.jobs.tasks import _order_message

    over = {"amount": 129900.0, "target": 125000.0}
    assert _order_message(OrderStatus.over_target, over, None) == "over target: paid nothing, ₹1,29,900.00 > ₹1,25,000.00"
    uncapped = {"amount": 129900.0, "target": None}
    assert _order_message(OrderStatus.over_target, uncapped, None) == "over target: paid nothing, ₹1,29,900.00 > no target"
    placed = {"order_id": "OD3", "amount": 129900.5}
    assert _order_message(OrderStatus.placed, placed, None) == "placed: order OD3, amount ₹1,29,900.50"
    unconfirmed = _order_message(OrderStatus.unconfirmed, {"amount": 129900.0}, None)
    assert unconfirmed.startswith("unconfirmed: submitted for ₹1,29,900.00 but Flipkart never confirmed it")
