"""Tests for the price-target gate and ordering pure helpers (PLAN.md §6a)."""

import asyncio

from app.automation import ordering as order_flow
from app.automation.ordering import AddressInfo, CardInfo, GstInfo, parse_amount, within_target
from app.core import selectors as S
from app.db.models import OrderStatus
from tests.fakes import FakePage

ADDR = AddressInfo(name="N", phone="", pin="122016", area="Somewhere")
GST = GstInfo(gstin="", business_name="")  # blank => GST step is skipped
CARD = CardInfo(number="4111111111111111", month="12", year="28", cvv="123")


# --- pure helpers ------------------------------------------------------------
def test_parse_amount():
    assert parse_amount("₹999") == 999.0
    assert parse_amount("₹1,000.00") == 1000.0
    assert parse_amount("₹1,000.01") == 1000.01
    assert parse_amount(None) == 0.0
    assert parse_amount("") == 0.0


def test_within_target_strict():
    assert within_target(999.99, 1000.0) is True
    assert within_target(1000.00, 1000.0) is True   # equal is allowed
    assert within_target(1000.01, 1000.0) is False  # not even a paisa over
    assert within_target(5000.0, None) is True       # no target => allowed


# --- the gate, end to end over a fake page -----------------------------------
def _run(page, target):
    return asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://flipkart.com/x",
            address=ADDR,
            gst=GST,
            card=CARD,
            qty=1,
            target=target,
        )
    )


def test_order_over_target_is_not_charged():
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹1,000.01"})
    result = _run(page, target=1000.0)

    assert result["status"] == OrderStatus.over_target
    assert result["amount"] == 1000.01
    # The irreversible confirm click must NOT have happened.
    assert not page.submitted


def test_order_within_target_is_placed():
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹999.99"})
    result = _run(page, target=1000.0)

    assert result["status"] == OrderStatus.placed
    # From Flipkart's confirmation page, never from the account's order list.
    assert result["order_id"] == "OD428123456789100"
    assert page.submitted


def test_order_equal_to_target_is_placed():
    page = FakePage(
        inner_text_map={
            S.ORDER_TOTAL: "₹1000.00",
            S.ORDER_MODEL: "M",
            S.ORDER_COLOR: "C",
            S.ORDER_AMOUNT: "₹1000.00",
        }
    )
    result = _run(page, target=1000.0)
    assert result["status"] == OrderStatus.placed
    assert page.submitted


# --- fail-closed behaviour (found live 2026-10-23) ----------------------------
def test_unreadable_total_is_never_confirmed():
    """The bug this guards: a dead ORDER_TOTAL selector made the cap inert.

    read_final_amount used to return 0.0 when it could not find the total, and
    0.0 <= target is True, so EVERY order sailed through the gate and was
    charged. An amount we cannot read must block the order instead.
    """
    page = FakePage()  # ORDER_TOTAL has no text -> inner_text raises
    result = _run(page, target=1000.0)

    assert result["status"] == OrderStatus.failed
    assert "could not read" in result["error"].lower()
    assert not page.submitted  # nothing was charged


def test_zero_total_is_treated_as_unreadable():
    """A ₹0 checkout total means we read the wrong element, not a free order."""
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹0"})
    result = _run(page, target=1000.0)

    assert result["status"] == OrderStatus.failed
    assert not page.submitted


# --- the total must have STOPPED MOVING (found live 2026-10-24) --------------
def test_gate_waits_for_a_re_pricing_total_to_settle():
    """The bug this guards: the gate read the total mid-re-price, and low.

    Selecting Cash on Delivery re-prices the order. A live probe read ₹17,628
    on a checkout that settled at ₹17,707 — the ₹79 handling fee arrived a beat
    later. Reading low is the dangerous direction: at target ₹17,650 the gate
    would have approved an order that charges ₹17,707. It must read the settled
    figure and refuse.
    """
    page = FakePage(inner_text_map={S.ORDER_TOTAL: ["₹17,628", "₹17,707", "₹17,707"]})
    result = _run(page, target=17_650.0)

    assert result["amount"] == 17_707.0, "gated on the mid-update figure"
    assert result["status"] == OrderStatus.over_target
    assert not page.submitted


def test_a_fee_that_lands_a_few_reads_later_is_still_caught():
    """Two agreeing reads were enough only while each read happened to wait
    out a 4s timeout. Once that wait went, a COD handling fee landing ~1.5s
    after the method was chosen slipped past the gate. The figure must hold
    for several consecutive reads before it counts as settled."""
    page = FakePage(inner_text_map={S.ORDER_TOTAL: ["₹17,628", "₹17,628", "₹17,628", "₹17,707"]})
    result = _run(page, target=17_650.0)

    assert result["amount"] == 17_707.0, "gated on the pre-fee figure"
    assert result["status"] == OrderStatus.over_target
    assert not page.submitted


def test_gate_caps_on_the_highest_figure_when_the_page_disagrees():
    """Summary total and pay-button amount are separate, sometimes-disagreeing
    elements. Capping on the larger can refuse too much but never charge too
    much; capping on the smaller charges over target."""
    page = FakePage(
        inner_text_map={
            S.ORDER_TOTAL: "₹1,000.00",
            S.PAY_CTA_AMOUNT: "Pay ₹1,200.00",
        }
    )
    result = _run(page, target=1_100.0)

    assert result["amount"] == 1_200.0
    assert result["status"] == OrderStatus.over_target
    assert not page.submitted


def test_a_total_that_never_settles_is_refused():
    """A figure still changing when time runs out is not a figure we can
    promise anything about, so it fails closed rather than being charged."""
    page = FakePage(
        inner_text_map={S.ORDER_TOTAL: ["₹1", "₹2", "₹3", "₹4", "₹5", "₹6", "₹7", "₹8",
                                        "₹9", "₹10", "₹11", "₹12", "₹13", "₹14", "₹15"]}
    )
    result = _run(page, target=100_000.0)  # every reading is under target

    assert result["status"] == OrderStatus.failed
    assert not page.submitted


def test_cod_button_without_an_amount_still_reads_the_total():
    """A COD CTA says "Place Order" and carries no amount. Its absence is
    normal and must not look like a read failure."""
    page = FakePage(
        inner_text_map={
            S.ORDER_TOTAL: "₹999.00",
            S.ORDER_MODEL: "M",
            S.ORDER_COLOR: "C",
            S.ORDER_AMOUNT: "₹999.00",
        }
    )  # PAY_CTA_AMOUNT absent -> inner_text raises
    result = _run(page, target=1_000.0)

    assert result["amount"] == 999.0
    assert result["status"] == OrderStatus.placed


def test_within_target_fails_closed_on_unknown_amount():
    assert within_target(None, 1000.0) is False
    assert within_target(None, None) is False  # no cap still needs a real amount


def test_total_includes_fees_not_just_the_item_price():
    """Live checkout showed item ₹27,999 but Total Amount ₹28,128 (+₹129 fee).

    Capping on the item price would approve an order that charges more than
    the target, so the gate must read the total.
    """
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹28,128"})
    result = _run(page, target=28000.0)

    assert result["status"] == OrderStatus.over_target
    assert result["amount"] == 28128.0
    assert not page.submitted
