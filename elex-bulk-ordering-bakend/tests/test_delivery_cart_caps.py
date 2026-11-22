"""Delivery-date and cart-amount caps.

These were recorded but NOT enforced — the form offered a cap that did nothing.
Both fail CLOSED: an unreadable value never counts as permission, which is the
mistake the price gate made when it read "could not find" as ₹0.00.
"""

import asyncio
from datetime import date

from app.automation import ordering as order_flow
from app.automation.ordering import AddressInfo, CardInfo, GstInfo, parse_delivery_days
from app.core import selectors as S
from app.db.models import OrderStatus
from tests.fakes import FakePage

ADDR = AddressInfo(name="N", phone="", pin="122016", area="Somewhere")
GST = GstInfo(gstin="", business_name="")
CARD = CardInfo(number="4111111111111111", month="12", year="28", cvv="123")
TODAY = date(2026, 10, 23)


def _run(page, **kw):
    return asyncio.run(
        order_flow.run_order(
            page,
            product_link="https://flipkart.com/x",
            address=ADDR,
            gst=GST,
            card=CARD,
            qty=1,
            **kw,
        )
    )


# --- date parsing ------------------------------------------------------------
def test_delivery_date_formats():
    assert parse_delivery_days("Delivery by Oct 28, Wed", today=TODAY) == 5
    assert parse_delivery_days("Delivery by 28 Oct", today=TODAY) == 5
    assert parse_delivery_days("Delivery by Mon Oct 25", today=TODAY) == 2


def test_a_date_already_past_is_read_as_next_year_not_negative():
    """Flipkart omits the year, so January read naively would be negative."""
    assert parse_delivery_days("Delivery by Jan 5", today=TODAY) == 74


def test_unreadable_delivery_text():
    assert parse_delivery_days("rubbish", today=TODAY) is None
    assert parse_delivery_days(None, today=TODAY) is None


# --- the gates ---------------------------------------------------------------
def test_slow_delivery_is_refused_before_any_card_is_entered():
    page = FakePage(inner_text_map={S.CHECKOUT_DELIVERY_BY: "Delivery by Dec 20"})
    result = _run(page, max_delivery_days=7)

    assert result["status"] == OrderStatus.failed
    assert "delivery" in result["error"].lower()
    assert not page.submitted
    assert S.CARD_NUMBER not in page.fills  # refused before touching the card


def test_fast_enough_delivery_passes_the_gate():
    from datetime import datetime, timedelta

    # Five days from today, in Flipkart's wording — a fixed date here turned
    # into "364 days away" the night the calendar passed it.
    soon = (datetime.now() + timedelta(days=5)).strftime("%b %d").replace(" 0", " ")
    page = FakePage(
        inner_text_map={
            S.CHECKOUT_DELIVERY_BY: f"Delivery by {soon}",
            S.ORDER_TOTAL: "₹999",
            S.ORDER_AMOUNT: "₹999",
        }
    )
    result = _run(page, max_delivery_days=30, target=1000.0)
    assert result["status"] == OrderStatus.placed


def test_unreadable_delivery_date_fails_closed():
    """No promise read means no promise kept — refuse rather than assume."""
    page = FakePage()  # nothing to read
    result = _run(page, max_delivery_days=7)

    assert result["status"] == OrderStatus.failed
    assert "could not read the delivery date" in result["error"]
    assert not page.submitted


def test_no_delivery_cap_means_the_date_is_not_even_read():
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹999", S.ORDER_AMOUNT: "₹999"})
    result = _run(page, target=1000.0)  # no max_delivery_days
    assert result["status"] == OrderStatus.placed


def test_cart_over_the_cart_limit_is_refused():
    page = FakePage(inner_text_map={S.CHECKOUT_CART_AMOUNT: "₹45,999"})
    result = _run(page, cart_amount_limit=30000.0)

    assert result["status"] == OrderStatus.over_target
    assert result["amount"] == 45999.0
    # NFR-010: money as every screen writes it, Indian grouping and two decimals.
    assert result["error"] == "cart ₹45,999.00 exceeds the cart limit ₹30,000.00"
    assert not page.submitted


def test_the_cart_limit_message_groups_lakhs_the_indian_way():
    page = FakePage(inner_text_map={S.CHECKOUT_CART_AMOUNT: "₹1,29,900"})
    result = _run(page, cart_amount_limit=125000.0)

    assert result["error"] == "cart ₹1,29,900.00 exceeds the cart limit ₹1,25,000.00"


def test_unreadable_cart_amount_fails_closed():
    page = FakePage()
    result = _run(page, cart_amount_limit=30000.0)
    assert result["status"] == OrderStatus.failed
    assert "could not read the cart amount" in result["error"]


def test_cart_limit_and_payable_target_are_different_numbers():
    """Live checkout showed cart ₹45,999 but payable ₹28,128.

    A cart cap must not be satisfied by the payable total, nor vice versa —
    conflating them would let a run through on the wrong figure.
    """
    page = FakePage(
        inner_text_map={
            S.CHECKOUT_CART_AMOUNT: "₹45,999",
            S.ORDER_TOTAL: "₹28,128",
            S.ORDER_AMOUNT: "₹28,128",
        }
    )
    # Cart cap allows it, payable target allows it -> placed.
    assert _run(page, cart_amount_limit=50000.0, target=30000.0)["status"] == OrderStatus.placed

    # Cart cap bites even though the payable total is well under target.
    page2 = FakePage(
        inner_text_map={S.CHECKOUT_CART_AMOUNT: "₹45,999", S.ORDER_TOTAL: "₹28,128"}
    )
    assert _run(page2, cart_amount_limit=40000.0, target=30000.0)["status"] == OrderStatus.over_target


# --- the location gate (found live 2026-10-23) -------------------------------
def test_a_location_gated_checkout_fails_fast_with_the_real_reason():
    """Flipkart withholds checkout until the address has a map pin.

    A gated page renders NO Continue/Pay control at all, so the old flow walked
    through address, qty, GST and payment, timed each one out (10s + 30s + 60s
    + 30s + 10s) and reported a vague failure after ~2 minutes. The gate is now
    detected first and named.
    """
    page = FakePage(
        inner_text_map={
            S.CHECKOUT_LOCATION_GATE: "Please set exact location on map.",
        }
    )
    result = _run(page)

    assert result["status"] == OrderStatus.failed
    assert "exact location" in result["error"].lower()
    assert "Set Location" in result["error"]
    # It stopped immediately — no address, GST, card or confirm work was tried.
    assert S.ADDR_NAME not in page.fills
    assert S.CARD_NUMBER not in page.fills
    assert not page.submitted


def test_an_ungated_checkout_proceeds_normally():
    page = FakePage(inner_text_map={S.ORDER_TOTAL: "₹999", S.ORDER_AMOUNT: "₹999"})
    assert _run(page, target=1000.0)["status"] == OrderStatus.placed
